#!/usr/bin/env python3
"""Generate a source helper that forces .sdata2 float/double ordering.

The reference object already knows the desired order: MWCC emits float and
double constants as OBJECT symbols in .sdata2. This tool reads those symbols
from build/GALE01/obj/<unit>.o, emits a conventional unused helper with
`(void) <constant>;` statements in that order, writes it into the requested
source file, then compiles the TU and reports whether the non-gap .sdata2
constants now match.
"""

from __future__ import annotations

import argparse
import math
import re
import shutil
import struct
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from melee_root import resolve_root
from ninja_compile import direct_compile


ROOT = resolve_root()
SRC_ROOT = ROOT / "src"


@dataclass(frozen=True)
class Symbol:
    offset: int
    size: int
    typ: str
    bind: str
    vis: str
    ndx: str
    name: str


@dataclass(frozen=True)
class Sdata2Entry:
    offset: int
    size: int
    data: bytes
    name: str

    @property
    def c_type(self) -> str:
        return "f32" if self.size == 4 else "f64"


ORDER_FUNC_RE = re.compile(
    r"(?m)^[ \t]*static\s+(?:inline\s+)?"
    r"[A-Za-z_][A-Za-z0-9_\s\*]*?\b"
    r"(?P<name>sdata2_order(?:ing)?|order_sdata2(?:_\d+)?)"
    r"\s*\([^;{}]*\)\s*\{"
)


def find_tool(name: str) -> str:
    local = ROOT / "build/binutils" / name
    if local.is_file():
        return str(local)
    found = shutil.which(name)
    if found is not None:
        return found
    raise RuntimeError(f"could not find {name}")


def section_indices(readelf: str, obj: Path) -> dict[int, str]:
    out = subprocess.check_output(
        [readelf, "-S", "--wide", str(obj)], cwd=ROOT, text=True,
        errors="replace")
    sections: dict[int, str] = {}
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("["):
            continue
        parts = line.replace("[", " [ ").replace("]", " ] ").split()
        if len(parts) >= 5 and parts[0] == "[" and parts[2] == "]":
            try:
                sections[int(parts[1])] = parts[3]
            except ValueError:
                pass
    return sections


def extract_section(objcopy: str, obj: Path, section: str) -> bytes:
    import tempfile

    with tempfile.NamedTemporaryFile() as tmp:
        result = subprocess.run(
            [
                objcopy,
                "-O",
                "binary",
                f"--only-section={section}",
                str(obj),
                tmp.name,
            ],
            cwd=ROOT,
            capture_output=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                result.stderr.decode(errors="replace").strip()
                or f"could not extract {section} from {obj}")
        return Path(tmp.name).read_bytes()


def read_symbols(readelf: str, obj: Path) -> list[Symbol]:
    out = subprocess.check_output(
        [readelf, "-s", "--wide", str(obj)], cwd=ROOT, text=True,
        errors="replace")
    syms: list[Symbol] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 8 or not parts[0].endswith(":"):
            continue
        try:
            offset = int(parts[1], 16)
            size = int(parts[2], 10)
        except ValueError:
            continue
        syms.append(Symbol(
            offset=offset,
            size=size,
            typ=parts[3],
            bind=parts[4],
            vis=parts[5],
            ndx=parts[6],
            name=parts[7],
        ))
    return syms


def is_gap_symbol(sym: Symbol) -> bool:
    return sym.name.startswith("gap_") or sym.typ == "SECTION"


def literal_symbols(
    symbols: list[Symbol],
    section_idx: int,
    *,
    prefer_local: bool,
) -> list[Symbol]:
    base = [
        sym for sym in symbols
        if sym.ndx == str(section_idx)
        and sym.typ == "OBJECT"
        and sym.size in (4, 8)
        and not is_gap_symbol(sym)
    ]
    if prefer_local:
        local_literals = [
            sym for sym in base
            if sym.bind == "LOCAL" and sym.name.startswith("@")
        ]
        if local_literals:
            return local_literals
    return base


def sdata2_entries(
    obj: Path,
    *,
    prefer_local: bool = True,
) -> tuple[bytes, list[Sdata2Entry]]:
    readelf = find_tool("powerpc-eabi-readelf")
    objcopy = find_tool("powerpc-eabi-objcopy")
    sections = section_indices(readelf, obj)
    by_name = {name: idx for idx, name in sections.items()}
    section_idx = by_name.get(".sdata2")
    if section_idx is None:
        return b"", []

    data = extract_section(objcopy, obj, ".sdata2")
    entries: list[Sdata2Entry] = []
    seen: set[tuple[int, int]] = set()
    for sym in sorted(
        literal_symbols(
            read_symbols(readelf, obj),
            section_idx,
            prefer_local=prefer_local,
        ),
        key=lambda s: (s.offset, s.size, s.name),
    ):
        key = (sym.offset, sym.size)
        if key in seen:
            continue
        seen.add(key)
        if sym.offset < 0 or sym.offset + sym.size > len(data):
            continue
        entries.append(Sdata2Entry(
            offset=sym.offset,
            size=sym.size,
            data=data[sym.offset:sym.offset + sym.size],
            name=sym.name,
        ))
    return data, entries


def entry_signature(entries: list[Sdata2Entry]) -> list[tuple[int, bytes]]:
    return [(e.size, e.data) for e in entries]


def used_size(entries: list[Sdata2Entry]) -> int:
    return max((e.offset + e.size for e in entries), default=0)


def sdata2_matches(
    target_data: bytes,
    target_entries: list[Sdata2Entry],
    current_data: bytes,
    current_entries: list[Sdata2Entry],
) -> bool:
    target_used = used_size(target_entries)
    current_used = used_size(current_entries)
    return (
        entry_signature(target_entries) == entry_signature(current_entries)
        and target_used == current_used
        and target_data[:target_used] == current_data[:current_used]
    )


def normalize_float_literal(s: str) -> str:
    if "e" not in s.lower() and "." not in s:
        s += ".0"
    return s


def entry_literal(entry: Sdata2Entry) -> str:
    if entry.size == 4:
        bits = int.from_bytes(entry.data, "big")
        if bits == 0x80000000:
            return "-0.0f"
        val = struct.unpack(">f", entry.data)[0]
        if not math.isfinite(val):
            raise ValueError(f"cannot emit non-finite f32 literal for {entry.name}")
        return normalize_float_literal(format(val, ".9g")) + "f"
    if entry.size == 8:
        bits = int.from_bytes(entry.data, "big")
        if bits == 0x8000000000000000:
            return "-0.0"
        val = struct.unpack(">d", entry.data)[0]
        if not math.isfinite(val):
            raise ValueError(f"cannot emit non-finite f64 literal for {entry.name}")
        return normalize_float_literal(format(val, ".17g"))
    raise ValueError(f"unsupported .sdata2 entry size {entry.size}")


def render_helper(entries: list[Sdata2Entry], name: str) -> str:
    lines = [f"static void {name}(void)", "{"]
    for entry in entries:
        lines.append(f"    (void) {entry_literal(entry)};")
    lines.append("}")
    return "\n".join(lines) + "\n"


def find_matching_brace(text: str, open_brace: int) -> Optional[int]:
    depth = 0
    state = "code"
    quote = ""
    escaped = False
    i = open_brace
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""

        if state == "line_comment":
            if ch == "\n":
                state = "code"
        elif state == "block_comment":
            if ch == "*" and nxt == "/":
                state = "code"
                i += 1
        elif state == "string":
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                state = "code"
        else:
            if ch == "/" and nxt == "/":
                state = "line_comment"
                i += 1
            elif ch == "/" and nxt == "*":
                state = "block_comment"
                i += 1
            elif ch in ("'", '"'):
                state = "string"
                quote = ch
                escaped = False
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    return None


def expand_removal_end(text: str, end: int) -> int:
    while end < len(text) and text[end] in " \t":
        end += 1
    if end < len(text) and text[end] == "\n":
        end += 1
    while end < len(text):
        line_end = text.find("\n", end)
        if line_end < 0:
            line_end = len(text)
        if text[end:line_end].strip():
            break
        end = line_end + (1 if line_end < len(text) else 0)
    return end


def order_function_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for match in ORDER_FUNC_RE.finditer(text):
        open_brace = match.end() - 1
        close_brace = find_matching_brace(text, open_brace)
        if close_brace is None:
            continue
        spans.append((match.start(), expand_removal_end(text, close_brace + 1)))
    return spans


def insertion_point(text: str) -> int:
    includes = list(re.finditer(r"(?m)^#include[^\n]*(?:\n|$)", text))
    if includes:
        pos = includes[-1].end()
        while pos < len(text) and text[pos] == "\n":
            pos += 1
        return pos
    return 0


def install_helper(text: str, helper: str) -> tuple[str, bool]:
    block = helper + "\n"
    spans = order_function_spans(text)
    if not spans:
        pos = insertion_point(text)
        return text[:pos] + block + text[pos:], False

    out = text
    first_start = spans[0][0]
    for i, (start, end) in enumerate(reversed(spans)):
        if i == len(spans) - 1:
            out = out[:start] + block + out[end:]
        else:
            out = out[:start] + out[end:]
    return out, True


def resolve_source(arg: str) -> Path:
    path = Path(arg)
    candidates: list[Path] = []
    if path.suffix == ".c":
        candidates.append(path if path.is_absolute() else (Path.cwd() / path))
        candidates.append(ROOT / path)
    else:
        candidates.append(SRC_ROOT / f"{arg}.c")
        candidates.append(ROOT / f"{arg}.c")
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError(f"could not find source file for {arg}")


def obj_path_for_source(source: Path) -> str:
    try:
        return source.relative_to(SRC_ROOT).with_suffix("").as_posix()
    except ValueError as e:
        raise ValueError(f"{source} is not under {SRC_ROOT}") from e


def describe_entry(entry: Sdata2Entry) -> str:
    return f"{entry.c_type} {entry_literal(entry)}"


def first_mismatch(
    target_data: bytes,
    target: list[Sdata2Entry],
    current_data: bytes,
    current: list[Sdata2Entry],
) -> str:
    limit = min(len(target), len(current))
    for i in range(limit):
        if target[i].size != current[i].size or target[i].data != current[i].data:
            return (
                f"constant {i}: target {describe_entry(target[i])}, "
                f"current {describe_entry(current[i])}")
    if len(target) != len(current):
        return f"constant count: target {len(target)}, current {len(current)}"
    target_used = used_size(target)
    current_used = used_size(current)
    if target_used != current_used:
        return f"used .sdata2 size: target 0x{target_used:x}, current 0x{current_used:x}"
    for i in range(min(target_used, current_used)):
        if target_data[i] != current_data[i]:
            return f"byte 0x{i:x}: target 0x{target_data[i]:02x}, current 0x{current_data[i]:02x}"
    return "unknown mismatch"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="source file or unit path to update")
    parser.add_argument(
        "--name", default="sdata2_order",
        help="generated helper name (default: sdata2_order)")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="print the generated helper without modifying the source")
    args = parser.parse_args()

    try:
        source = resolve_source(args.source)
        obj_path = obj_path_for_source(source)
        ref_obj = ROOT / "build/GALE01/obj" / f"{obj_path}.o"
        if not ref_obj.is_file():
            raise FileNotFoundError(f"reference object not found: {ref_obj}")

        target_data, target_all_entries = sdata2_entries(
            ref_obj, prefer_local=False)
        _, helper_entries = sdata2_entries(ref_obj, prefer_local=True)
        helper = render_helper(helper_entries, args.name)
        text = source.read_text(encoding="utf-8", errors="surrogateescape")
        updated, replaced = install_helper(text, helper)

        if args.dry_run:
            print(helper, end="")
            return 0

        source.write_text(updated, encoding="utf-8", errors="surrogateescape")
        action = "replaced existing" if replaced else "inserted"
        print(
            f"{action} {args.name} in {source.relative_to(ROOT)} "
            f"with {len(helper_entries)} .sdata2 constants")

        compiled = direct_compile(obj_path, quiet=False)
        if compiled is None:
            print("sdata2 still differs: compile failed; other work remains")
            return 1

        current_data, current_entries = sdata2_entries(
            compiled.obj, prefer_local=False)
        if sdata2_matches(
            target_data,
            target_all_entries,
            current_data,
            current_entries,
        ):
            print(
                f"sdata2 fixed: {len(target_all_entries)} float/double constants "
                "match target order")
            return 0

        print("sdata2 still differs: other work remains")
        print(first_mismatch(
            target_data,
            target_all_entries,
            current_data,
            current_entries,
        ))
        return 2
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
