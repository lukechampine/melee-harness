#!/usr/bin/env python3
"""Dump the mwcc_debug compiler's IR/backend listing for one function.

Resolves the function's TU (via build/GALE01/report.json, like checkdiff.py),
compiles that TU with the instrumented MWCC, then truncates pcdump.txt to
just that function's section so the output concerns only that function.

Usage: tools/mwcc_dump.py it_802E70BC
       tools/mwcc_dump.py --runner wibo it_802E70BC
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

# Melee checkout root: explicit override, then Claude Code's project dir,
# then assume this script lives at <melee>/tools/.
ROOT = Path(
    os.environ.get("MELEE_ROOT")
    or os.environ.get("CLAUDE_PROJECT_DIR")
    or Path(__file__).resolve().parents[1]
)
REPORT_PATH = ROOT / "build/GALE01/report.json"


def find_unit_for_function(func_name: str) -> Optional[str]:
    """Return the repo-relative source path for the TU defining `func_name`,
    or None if no unit in report.json declares it (same lookup as
    checkdiff.py)."""
    if not REPORT_PATH.exists():
        raise SystemExit(
            f"missing {REPORT_PATH} — run a normal build first so objdiff "
            "writes the report (function->TU lookup needs it)"
        )
    with REPORT_PATH.open("r") as f:
        for unit in json.load(f).get("units", []):
            for function in unit.get("functions", []):
                if function.get("name") == func_name:
                    obj = unit.get("name", "").removeprefix("main/")
                    return f"src/{obj}.c"
    return None


def find_build_block(src: str) -> tuple[str, str]:
    """Return (cflags, mw_version) for the object built from `src`."""
    text = (ROOT / "build.ninja").read_text()
    # Unfold ninja line continuations.
    text = text.replace("$\n", " ")
    obj = f"build/GALE01/{src[:-2]}.o"
    blocks = re.split(r"^build ", text, flags=re.M)
    for b in blocks:
        if b.startswith(f"{obj}:") or b.startswith(f"{obj} :"):
            cflags = re.search(r"\bcflags = (.*)", b).group(1).strip()
            mw = re.search(r"\bmw_version = (\S+)", b).group(1).strip()
            return cflags, mw
    raise SystemExit(f"no build block for {obj}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dump the mwcc_debug compiler's listing for one function."
    )
    parser.add_argument("function", help="function name (its TU is resolved automatically)")
    parser.add_argument(
        "--runner",
        choices=("auto", "wibo", "wine"),
        default="auto",
        help="execution backend (default auto = patched wibo with Wine "
        "fallback on SIGBUS; wibo = patched wibo only; wine = Wine only)",
    )
    return parser.parse_args()


def extract_function(body: str, func: str) -> Optional[str]:
    """Return only the `Starting function <func>` section of a pcdump body
    (up to the next `Starting function` line or EOF), or None if absent."""
    lines = body.splitlines(keepends=True)
    marker = "Starting function "
    start = None
    for i, line in enumerate(lines):
        if not line.startswith(marker):
            continue
        if start is None:
            if line[len(marker):].strip() == func:
                start = i
        else:
            return "".join(lines[start:i])
    if start is not None:
        return "".join(lines[start:])
    return None


def format_functions(funcs: list[str]) -> str:
    if len(funcs) <= 20:
        return ", ".join(funcs)
    return f"{len(funcs)} functions, first={funcs[0]}, last={funcs[-1]}"


def format_pass_counts(passes: list[str]) -> str:
    counts = Counter(passes)
    ordered = []
    for name in passes:
        if name not in ordered:
            ordered.append(name)
    return ", ".join(f"{name}={counts[name]}" for name in ordered)


def split_passes(section: str) -> list[tuple[str, list[str]]]:
    """Return dump passes as (pass name, lines) pairs."""
    passes = []
    name = None
    lines: list[str] = []
    for line in section.splitlines():
        if re.match(r"^(?:BEFORE|AFTER|FINAL) .+", line):
            if name is not None:
                passes.append((name, lines))
            name = line
            lines = [line]
        elif name is not None:
            lines.append(line)
    if name is not None:
        passes.append((name, lines))
    return passes


def choose_analysis_pass(passes: list[tuple[str, list[str]]]) -> tuple[str, list[str]]:
    for preferred in (
        "FINAL CODE AFTER INSTRUCTION SCHEDULING",
        "AFTER REGISTER COLORING",
    ):
        for name, lines in reversed(passes):
            if name == preferred:
                return name, lines
    return passes[-1] if passes else ("<none>", [])


def clean_inst(line: str) -> str:
    return line.split(";", 1)[0].strip()


def parse_inst(line: str) -> Optional[tuple[str, list[str], str]]:
    match = re.match(r"^\s+([a-z][a-z0-9.]*)\s+([^;]+)", line)
    if match is None:
        return None
    op = match.group(1)
    operands = [x.strip() for x in match.group(2).split(",")]
    return op, operands, clean_inst(line)


def reg_from_offset_operand(operand: str) -> Optional[tuple[int, str]]:
    match = re.match(r"(-?\d+)\((r\d+)\)", operand)
    if match is None:
        return None
    return int(match.group(1)), match.group(2)


def is_indexed_mem_op(op: str) -> bool:
    return op in {
        "lbzx", "lhax", "lhzx", "lwzx", "lfsx",
        "stbx", "sthx", "stwx", "stfsx",
    }


def is_offset_mem_op(op: str) -> bool:
    return op in {
        "lbz", "lha", "lhz", "lwz", "lfs",
        "stb", "sth", "stw", "stfs",
    }


def find_address_forms(lines: list[str]) -> list[str]:
    insts = [parsed for line in lines if (parsed := parse_inst(line))]
    counts: Counter[str] = Counter()
    folded_samples = []
    byte_index_samples = []

    for i, (op, operands, text) in enumerate(insts):
        if is_indexed_mem_op(op):
            counts[f"{op} indexed"] += 1
        elif is_offset_mem_op(op):
            for operand in operands[1:]:
                parsed = reg_from_offset_operand(operand)
                if parsed is not None:
                    offset, _ = parsed
                    if offset == 0:
                        counts[f"{op} offset0"] += 1
                    else:
                        counts[f"{op} offset"] += 1

        if op == "add" and len(operands) == 3:
            dest = operands[0]
            for next_op, next_operands, next_text in insts[i + 1:i + 4]:
                if not is_offset_mem_op(next_op):
                    continue
                for operand in next_operands[1:]:
                    parsed = reg_from_offset_operand(operand)
                    if parsed is not None and parsed[0] != 0 and parsed[1] == dest:
                        folded_samples.append(f"{text}; {next_text}")
                        break
                if len(folded_samples) >= 3:
                    break

        if op == "addi" and len(operands) == 3 and operands[2] != "0":
            dest = operands[0]
            for next_op, next_operands, next_text in insts[i + 1:i + 4]:
                if not is_indexed_mem_op(next_op):
                    continue
                if any(f"({dest}" in operand for operand in next_operands):
                    byte_index_samples.append(f"{text}; {next_text}")
                    break

    if not counts and not folded_samples and not byte_index_samples:
        return []

    summary = ", ".join(f"{name}={counts[name]}" for name in sorted(counts))
    out = [f"  address forms: {summary}" if summary else "  address forms:"]
    for sample in folded_samples[:3]:
        out.append(f"    folded offset: {sample}")
    for sample in byte_index_samples[:3]:
        out.append(f"    byte-offset indexed: {sample}")
    if folded_samples and byte_index_samples:
        out.append(
            "    hint: mixed next-element forms; try byte-offset pointer casts "
            "when a target wants addi base,+N plus indexed load"
        )
    return out


def find_branch_shapes(lines: list[str]) -> list[str]:
    insts = [parsed for line in lines if (parsed := parse_inst(line))]
    branch_counts: Counter[str] = Counter()
    samples = []
    exit_blocks = set(re.findall(r"^(B\d+): Succ=\{\}", "\n".join(lines), re.M))
    exit_branches = 0

    for op, operands, text in insts:
        if op not in {"b", "bt", "bf"}:
            continue
        if op == "b":
            branch_counts["b"] += 1
            target = operands[0] if operands else ""
        else:
            cond = operands[1] if len(operands) > 1 else "?"
            branch_counts[f"{op} {cond}"] += 1
            target = operands[2] if len(operands) > 2 else ""
        if target in exit_blocks:
            exit_branches += 1
        if op in {"bt", "bf"} and len(samples) < 4:
            samples.append(text)

    if not branch_counts:
        return []

    summary = ", ".join(f"{name}={branch_counts[name]}" for name in sorted(branch_counts))
    out = [f"  branch shapes: {summary}; branches-to-exit={exit_branches}"]
    for sample in samples:
        out.append(f"    conditional: {sample}")
    return out


def find_copy_shapes(section: str, lines: list[str]) -> list[str]:
    log = section.split("BEFORE GLOBAL OPTIMIZATION", 1)[0]
    log_counts = {
        "propagatable assignments": len(re.findall(r"Found propagatable assignment", log)),
        "expression propagations": len(re.findall(r"Found expression propagation", log)),
        "common-sub replacements": len(re.findall(r"Replacing common sub", log)),
        "dead assignments": len(re.findall(r"Removing dead assignment", log)),
    }
    insts = [parsed for line in lines if (parsed := parse_inst(line))]
    zero_copies = []
    for op, operands, text in insts:
        is_mr = op == "mr" and len(operands) == 2
        is_addi_zero = op == "addi" and len(operands) == 3 and operands[2] == "0"
        if is_mr or is_addi_zero:
            zero_copies.append(text)

    if not any(log_counts.values()) and not zero_copies:
        return []

    out = [
        "  copy/prop clues: "
        + ", ".join(f"{name}={count}" for name, count in log_counts.items())
        + f"; zero-copy ops={len(zero_copies)}"
    ]
    for sample in zero_copies[:5]:
        out.append(f"    copy: {sample}")
    return out


def print_shape_summary(func: str, pcdump: Path, section: str) -> None:
    passes = split_passes(section)
    pass_names = [name for name, _ in passes]
    pass_name, pass_lines = choose_analysis_pass(passes)
    section_lines = section.splitlines()

    print(
        f"[mwcc_dump] {func}: {len(section_lines)} lines; "
        f"full function dump available at: {pcdump}"
    )
    print(f"[mwcc_dump] passes: {format_pass_counts(pass_names)}")
    print(f"[mwcc_dump] shape analysis from: {pass_name}")

    details = (
        find_address_forms(pass_lines)
        + find_branch_shapes(pass_lines)
        + find_copy_shapes(section, pass_lines)
    )
    if details:
        for line in details:
            print(f"[mwcc_dump]{line}")
    else:
        print("[mwcc_dump]  no address/branch/copy patterns recognized")


def finalize_dump(func: str) -> int:
    """Truncate pcdump.txt to just `func`'s section and print a one-line
    summary. Returns a process-style exit code (0 = section found)."""
    pcdump = ROOT / "pcdump.txt"
    if not pcdump.exists():
        print("[mwcc_dump] no pcdump.txt produced", file=sys.stderr)
        return 1

    body = pcdump.read_text(errors="replace")
    section = extract_function(body, func)

    if section is None:
        # Leave the full dump in place so the user can inspect it; the most
        # useful thing we can offer is the list of names that *are* present
        # (the function was likely inlined, or the name is wrong).
        present = re.findall(r"^Starting function (\S+)", body, re.M)
        print(f"[mwcc_dump] {func!r} not found (inlined or wrong name); "
              f"present: {format_functions(present)}", file=sys.stderr)
        return 1

    pcdump.write_text(section)
    print_shape_summary(func, pcdump, section)
    return 0


def wibo_path() -> Path:
    """Resolve the patched wibo built in the harness. Order:

      1. $MWCC_WIBO                          explicit override
      2. <harness>/bin/wibo                  what ./setup.sh installs
      3. <harness>/wibo/build/release/wibo   raw cmake output
      4. <melee>/build/tools/wibo            stock fallback (still crashes on
         @NNN temps / sjiswrap; the Wine fallback exists for this)

    <harness> is tried both as the melee sibling (the normal case: this
    script runs as the melee tools/ overlay copy) and relative to the
    script itself (run in place from / symlinked out of the harness).
    """
    override = os.environ.get("MWCC_WIBO")
    if override:
        return Path(override)
    harness_roots = (
        ROOT.parent / "melee-harness",
        Path(__file__).resolve().parents[1],
    )
    for sub in (("bin", "wibo"), ("wibo", "build", "release", "wibo")):
        for h in harness_roots:
            cand = h.joinpath(*sub)
            if cand.is_file():
                return cand
    return ROOT / "build/tools/wibo"


def build_command(runner: str, cc: Path, cflags: str, src: str) -> list[str]:
    args = [str(cc), *shlex.split(cflags), "-c", src, "-o", "/tmp/mwcc_dump.o"]
    if runner == "wibo":
        return [str(wibo_path()), *args]
    if runner == "wine":
        wine = os.environ.get("WINE", "wine")
        if shutil.which(wine) is None and not Path(wine).exists():
            raise SystemExit(f"missing Wine runner: {wine}")
        return [wine, *args]
    raise AssertionError(runner)


def run_compiler(runner: str, cc: Path, cflags: str, src: str) -> subprocess.CompletedProcess[str]:
    pcdump = ROOT / "pcdump.txt"
    if pcdump.exists():
        pcdump.unlink()

    env = os.environ.copy()
    if runner == "wine":
        env.setdefault("WINEDEBUG", "-all")

    proc = subprocess.run(
        build_command(runner, cc, cflags, src),
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
    )
    if proc.stdout:
        print(proc.stdout, end="")
    if proc.stderr:
        for line in proc.stderr.splitlines(keepends=True):
            if runner == "wine" and line == "wineserver: using server-side synchronization.\n":
                continue
            print(line, end="", file=sys.stderr)
    return proc


def main() -> int:
    args = parse_args()
    func = args.function
    src = find_unit_for_function(func)
    if src is None:
        raise SystemExit(
            f"could not find function {func!r} in {REPORT_PATH} "
            "(check the name, or rebuild so report.json is current)"
        )
    cflags, mw = find_build_block(src)
    cc = ROOT / "build" / "compilers" / mw / "mwcceppc_debug.exe"
    if not cc.exists():
        raise SystemExit(
            f"missing {cc} — build the mwcc_debug compiler per "
            "melee-harness/README.md (mwcc_debug/build_macos.sh + "
            "patch_mwcceppc_for_wibo.py)"
        )

    runner = "wibo" if args.runner == "auto" else args.runner
    proc = run_compiler(runner, cc, cflags, src)

    if args.runner == "auto" and proc.returncode == -10:
        print("[mwcc_dump] wibo SIGBUS; retrying with Wine", file=sys.stderr)
        proc = run_compiler("wine", cc, cflags, src)
        runner = "wine"

    return finalize_dump(func)


if __name__ == "__main__":
    raise SystemExit(main())
