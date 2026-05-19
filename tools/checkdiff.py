#!/usr/bin/env python3
"""
Helper script for LLM-driven decompiling. Fixes any missing imports, runs
the stack-frame autofix, then rebuilds and runs objdiff-cli on the specified
function.

Usage:
  tools/checkdiff.py <function_name>                   # focused diff
  tools/checkdiff.py --full-diff <function_name>       # don't hide matching lines
  tools/checkdiff.py --summary <function_name> [...]   # PASS/FAIL per function
  tools/checkdiff.py --no-fix-frame <function_name>    # skip the autofix

Without --summary, exactly one function must be given and the diff is printed.
By default, runs of 5+ matching lines are collapsed into a placeholder, with
1 line of context kept adjacent to diff lines. Pass --full-diff to disable.

With --summary, one or more functions may be given, and each gets a one-line
result:
  function_name: PASS
  function_name: FAIL (87.45%)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from objdiff_path import objdiff_cli

# Melee checkout root: explicit override, then Claude Code's project dir,
# then assume this script lives at <melee>/tools/.
ROOT = Path(
    os.environ.get("MELEE_ROOT")
    or os.environ.get("CLAUDE_PROJECT_DIR")
    or Path(__file__).resolve().parents[1]
)
REPORT_PATH = ROOT / "build/GALE01/report.json"
SRC_ROOT = ROOT / "src"
# Sibling harness scripts live next to this one, not in the melee tree.
TOOLS = Path(__file__).resolve().parent


@dataclass
class BuildBlock:
    rule: str
    src: str
    mw_version: str
    cflags: str
    extab_padding: Optional[str] = None


@dataclass
class CompiledObject:
    obj: Path
    tmpdir: tempfile.TemporaryDirectory


def find_unit_for_function(func_name: str) -> Optional[str]:
    with REPORT_PATH.open("r") as f:
        for unit in json.load(f).get("units", []):
            for function in unit.get("functions", []):
                if function.get("name") == func_name:
                    return unit.get("name", "").removeprefix("main/")
    return None


def find_build_block(obj_path: str) -> BuildBlock:
    """Parse build.ninja for the MWCC build edge that produces obj_path."""
    target = f"build/GALE01/src/{obj_path}.o"
    text = (ROOT / "build.ninja").read_text()
    # Unfold ninja line continuations so cflags can be read as one value.
    text = text.replace("$\n", " ")

    blocks = re.split(r"^build ", text, flags=re.M)
    for block in blocks:
        if not (block.startswith(f"{target}:") or block.startswith(f"{target} :")):
            continue

        build_line = block.splitlines()[0]
        match = re.match(rf"{re.escape(target)}\s*:\s*(\S+)\s+(.+)", build_line)
        if match is None:
            raise RuntimeError(f"could not parse build edge for {target}")

        rule = match.group(1)
        explicit_inputs = re.split(r"\s+\|\|?\s+", match.group(2), maxsplit=1)[0]
        inputs = shlex.split(explicit_inputs)
        if not inputs:
            raise RuntimeError(f"build edge for {target} has no source input")

        vars = {
            m.group(1): m.group(2).strip()
            for m in re.finditer(r"^\s+([A-Za-z_][A-Za-z0-9_]*) = (.*)$", block, re.M)
        }
        try:
            mw_version = vars["mw_version"]
            cflags = vars["cflags"]
        except KeyError as e:
            raise RuntimeError(f"build edge for {target} is missing {e.args[0]}") from e

        return BuildBlock(
            rule=rule,
            src=inputs[0],
            mw_version=mw_version,
            cflags=cflags,
            extab_padding=vars.get("extab_padding"),
        )

    raise RuntimeError(f"no build edge for {target}")


def direct_compile(obj_path: str) -> Optional[CompiledObject]:
    """Compile one TU directly from its build.ninja MWCC settings.

    The output goes to a unique temporary object, avoiding Ninja state and the
    normal build-tree object path.
    """
    try:
        block = find_build_block(obj_path)
    except RuntimeError as e:
        print(f"build.ninja lookup failed: {e}", file=sys.stderr)
        return None

    mwcc_rules = {"mwcc", "mwcc_sjis", "mwcc_extab", "mwcc_sjis_extab"}
    if block.rule not in mwcc_rules:
        print(f"unsupported build rule for direct compile: {block.rule}", file=sys.stderr)
        return None

    wibo = ROOT / "build/tools/wibo"
    sjiswrap = ROOT / "build/tools/sjiswrap.exe"
    dtk = ROOT / "build/tools/dtk"
    compiler = ROOT / "build" / "compilers" / block.mw_version / "mwcceppc.exe"

    required = [wibo, compiler]
    if "sjis" in block.rule:
        required.append(sjiswrap)
    if "extab" in block.rule:
        required.append(dtk)
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        print("missing build prerequisite(s):", file=sys.stderr)
        for path in missing:
            print(f"  {path}", file=sys.stderr)
        print("run `ninja tools` once to fetch/build prerequisites", file=sys.stderr)
        return None

    build_tmp = ROOT / "build"
    build_tmp.mkdir(exist_ok=True)
    tmpdir = tempfile.TemporaryDirectory(prefix="checkdiff-", dir=build_tmp)
    tmp_obj = Path(tmpdir.name) / f"{Path(obj_path).name}.o"

    cmd = [str(wibo)]
    if "sjis" in block.rule:
        cmd.append(str(sjiswrap))
    cmd += [
        str(compiler),
        *shlex.split(block.cflags),
        "-c",
        block.src,
        "-o",
        str(tmp_obj),
    ]

    result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        print("direct compile failed:", file=sys.stderr)
        print(result.stdout)
        print(result.stderr, file=sys.stderr)
        tmpdir.cleanup()
        return None

    if not tmp_obj.exists():
        objs = list(Path(tmpdir.name).glob("*.o"))
        if len(objs) == 1:
            tmp_obj = objs[0]
        else:
            print(f"direct compile did not produce {tmp_obj}", file=sys.stderr)
            tmpdir.cleanup()
            return None

    if "extab" in block.rule:
        padding = block.extab_padding or ""
        result = subprocess.run(
            [str(dtk), "extab", "clean", "--padding", padding, str(tmp_obj), str(tmp_obj)],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print("extab post-processing failed:", file=sys.stderr)
            print(result.stdout)
            print(result.stderr, file=sys.stderr)
            tmpdir.cleanup()
            return None

    return CompiledObject(obj=tmp_obj, tmpdir=tmpdir)


def auto_fix_frame(func_name: str) -> None:
    """Run `stack_permute.py --fix-frame` on `func_name`. Modifies the source if
    a strict improvement is available. Normal output is suppressed; only stderr
    (errors) is surfaced. For verbose stack-fix output, run stack_permute.py
    directly."""
    stack_permute = TOOLS / "stack_permute.py"
    proc = subprocess.run(
        [sys.executable, str(stack_permute), func_name, "--fix-frame"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if proc.stderr.strip():
        print(proc.stderr, file=sys.stderr, end="")


def build_unit(
    obj_path: str, fix_frame_funcs: Optional[list[str]] = None
) -> Optional[CompiledObject]:
    """Fix includes, optionally run --fix-frame on each named function, then
    compile the translation unit. Returns the temporary object on success."""
    c_file = SRC_ROOT / f"{obj_path}.c"

    fix_includes = TOOLS / "fix_includes.py"
    result = subprocess.run(
        [sys.executable, str(fix_includes), str(c_file)],
        cwd=ROOT,
        capture_output=True,
    )
    if result.returncode != 0:
        print(f"fix_includes.py failed:", file=sys.stderr)
        print(result.stderr.decode(), file=sys.stderr)
        return None

    for func_name in (fix_frame_funcs or []):
        auto_fix_frame(func_name)

    return direct_compile(obj_path)


def run_diff(
    obj_path: str,
    candidate_obj: Path,
    func_name: str,
    fmt: str = "two-column",
    capture: bool = False,
):
    """Run objdiff-cli. Returns CompletedProcess."""
    ref_obj = f"./build/GALE01/obj/{obj_path}.o"
    return subprocess.run(
        [
            objdiff_cli(), "diff",
            "--format", fmt,
            "-c", "functionRelocDiffs=data_value",
            "-1", ref_obj,
            "-2", str(candidate_obj),
            func_name,
        ],
        cwd=ROOT,
        capture_output=capture,
        text=capture,
    )


def resolve_functions(func_names: list[str]) -> dict[str, list[str]]:
    """Map function names to their translation units. Prints errors for unknown functions."""
    func_units: dict[str, list[str]] = {}
    for func_name in func_names:
        obj_path = find_unit_for_function(func_name)
        if obj_path is None:
            print(f"error: could not find function '{func_name}' in report.json", file=sys.stderr)
            continue
        func_units.setdefault(obj_path, []).append(func_name)
    return func_units


def build_units(func_units: dict[str, list[str]], fix_frame: bool) -> dict[str, CompiledObject]:
    """Compile each translation unit once. Returns compiled objects by path.
    If `fix_frame` is True, runs --fix-frame on every function first."""
    built: dict[str, CompiledObject] = {}
    for obj_path, funcs in func_units.items():
        compiled = build_unit(obj_path, funcs if fix_frame else None)
        if compiled is not None:
            built[obj_path] = compiled
    return built


MATCH_SKIP_THRESHOLD = 5
CONTEXT_LINES = 1


def is_matching_line(line: str) -> bool:
    """A two-column diff line that the diff tool considers matching (no marker
    char in column 0)."""
    if "|" not in line:
        return False
    return not line or line[0].isspace()


def collapse_matching(output: str) -> str:
    """Replace runs of `MATCH_SKIP_THRESHOLD`+ matching lines with a placeholder,
    keeping `CONTEXT_LINES` of context adjacent to diff lines."""
    lines = output.splitlines()
    result: list[str] = []
    buf: list[str] = []
    prev_diff = False

    def flush(next_diff: bool):
        nonlocal buf
        if len(buf) < MATCH_SKIP_THRESHOLD:
            result.extend(buf)
            buf = []
            return
        head = CONTEXT_LINES if prev_diff else 0
        tail = CONTEXT_LINES if next_diff else 0
        skipped = len(buf) - head - tail
        if skipped <= 0:
            result.extend(buf)
            buf = []
            return
        result.extend(buf[:head])
        result.append(f"... {skipped} matching lines skipped ...")
        if tail:
            result.extend(buf[-tail:])
        buf = []

    for line in lines:
        if is_matching_line(line):
            buf.append(line)
        else:
            is_diff = "|" in line
            flush(next_diff=is_diff)
            result.append(line)
            prev_diff = is_diff
    flush(next_diff=False)

    out = "\n".join(result)
    if output.endswith("\n"):
        out += "\n"
    return out


def check_single(func_name: str, fix_frame: bool, full_diff: bool) -> int:
    """Check a single function, printing the full diff."""
    obj_path = find_unit_for_function(func_name)
    if obj_path is None:
        print(f"error: could not find function '{func_name}' in report.json", file=sys.stderr)
        return 1

    compiled = build_unit(obj_path, [func_name] if fix_frame else None)
    if compiled is None:
        return 1

    result = run_diff(obj_path, compiled.obj, func_name, capture=True)
    out = result.stdout if full_diff else collapse_matching(result.stdout)
    print(out, end="")
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    return result.returncode


def check_multiple(func_names: list[str], fix_frame: bool) -> int:
    """Check multiple functions, printing OK/FAIL summary for each."""
    func_units = resolve_functions(func_names)
    if not func_units:
        return 1

    built = build_units(func_units, fix_frame)
    rc = 0

    for obj_path, funcs in func_units.items():
        compiled = built.get(obj_path)
        if compiled is None:
            for func_name in funcs:
                print(f"{func_name}: ERROR (compile failed)")
            rc = 1
            continue

        for func_name in funcs:
            result = run_diff(obj_path, compiled.obj, func_name, fmt="percent", capture=True)
            percent = result.stdout.strip()
            if percent == "100.00":
                print(f"{func_name}: PASS")
            else:
                print(f"{func_name}: FAIL ({percent}%)")
                rc = 1

    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-s", "--summary", action="store_true",
                    help="Print PASS/FAIL summary line per function instead of full diff")
    ap.add_argument("--no-fix-frame", dest="fix_frame", action="store_false",
                    help="Skip the automatic stack-frame fix that runs after fix_includes.py")
    ap.add_argument("--full-diff", action="store_true",
                    help="Show every diff line, including matching ones (default: collapse runs of 5+)")
    ap.set_defaults(fix_frame=True)
    ap.add_argument("functions", nargs="+", metavar="function", help="Function name(s)")
    args = ap.parse_args()

    if args.summary:
        return check_multiple(args.functions, fix_frame=args.fix_frame)

    if len(args.functions) != 1:
        ap.error("pass exactly one function without --summary, "
                 "or use --summary to get PASS/FAIL lines for multiple functions")
    return check_single(args.functions[0], fix_frame=args.fix_frame, full_diff=args.full_diff)


if __name__ == "__main__":
    raise SystemExit(main())
