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
import subprocess
import sys
from pathlib import Path
from typing import Optional

from objdiff_path import objdiff_cli

ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / "build/GALE01/report.json"
SRC_ROOT = ROOT / "src"


def find_unit_for_function(func_name: str) -> Optional[str]:
    with REPORT_PATH.open("r") as f:
        for unit in json.load(f).get("units", []):
            for function in unit.get("functions", []):
                if function.get("name") == func_name:
                    return unit.get("name", "").removeprefix("main/")
    return None


def auto_fix_frame(func_name: str) -> None:
    """Run `stack_permute.py --fix-frame` on `func_name`. Modifies the source if
    a strict improvement is available. Normal output is suppressed; only stderr
    (errors) is surfaced. For verbose stack-fix output, run stack_permute.py
    directly."""
    stack_permute = ROOT / "tools" / "stack_permute.py"
    proc = subprocess.run(
        [sys.executable, str(stack_permute), func_name, "--fix-frame"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if proc.stderr.strip():
        print(proc.stderr, file=sys.stderr, end="")


def build_unit(obj_path: str, fix_frame_funcs: Optional[list[str]] = None) -> bool:
    """Fix includes, optionally run --fix-frame on each named function, then
    build the translation unit. Returns True on success."""
    c_file = SRC_ROOT / f"{obj_path}.c"

    fix_includes = ROOT / "tools" / "fix_includes.py"
    result = subprocess.run(
        [sys.executable, str(fix_includes), str(c_file)],
        cwd=ROOT,
        capture_output=True,
    )
    if result.returncode != 0:
        print(f"fix_includes.py failed:", file=sys.stderr)
        print(result.stderr.decode(), file=sys.stderr)
        return False

    for func_name in (fix_frame_funcs or []):
        auto_fix_frame(func_name)

    our_obj = f"./build/GALE01/src/{obj_path}.o"
    result = subprocess.run(["ninja", our_obj], cwd=ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        print("ninja failed:", file=sys.stderr)
        print(result.stdout)
        print(result.stderr, file=sys.stderr)
        return False

    return True


def run_diff(obj_path: str, func_name: str, fmt: str = "two-column", capture: bool = False):
    """Run objdiff-cli. Returns CompletedProcess."""
    ref_obj = f"./build/GALE01/obj/{obj_path}.o"
    our_obj = f"./build/GALE01/src/{obj_path}.o"
    return subprocess.run(
        [
            objdiff_cli(), "diff",
            "--format", fmt,
            "-c", "functionRelocDiffs=data_value",
            "-1", ref_obj,
            "-2", our_obj,
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


def build_units(func_units: dict[str, list[str]], fix_frame: bool) -> set[str]:
    """Build each translation unit once. Returns set of successfully built paths.
    If `fix_frame` is True, runs --fix-frame on every function before the build."""
    built: set[str] = set()
    for obj_path, funcs in func_units.items():
        if build_unit(obj_path, funcs if fix_frame else None):
            built.add(obj_path)
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

    if not build_unit(obj_path, [func_name] if fix_frame else None):
        return 1

    result = run_diff(obj_path, func_name, capture=True)
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
        if obj_path not in built:
            for func_name in funcs:
                print(f"{func_name}: ERROR (build failed)")
            rc = 1
            continue

        for func_name in funcs:
            result = run_diff(obj_path, func_name, fmt="percent", capture=True)
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
