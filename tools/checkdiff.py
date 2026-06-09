#!/usr/bin/env python3
"""
Helper script for LLM-driven decompiling. Fixes any missing imports, rebuilds,
and runs objdiff-cli on the specified function.

Usage:
  tools/checkdiff.py <function_name>                   # focused diff
  tools/checkdiff.py --full-diff <function_name>       # don't hide matching lines
  tools/checkdiff.py --summary <function_name> [...]   # PASS/FAIL per function

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
import subprocess
import sys
from pathlib import Path
from typing import Optional

from objdiff_path import objdiff_cli
from ninja_compile import (
    CompiledObject,
    direct_compile,
    find_unit_for_function,
)

# Melee checkout root: explicit override, then Claude Code's project dir,
# then assume this script lives at <melee>/tools/.
from melee_root import resolve_root

ROOT = resolve_root()
SRC_ROOT = ROOT / "src"
# Sibling harness scripts live next to this one, not in the melee tree.
TOOLS = Path(__file__).resolve().parent


def build_unit(obj_path: str) -> Optional[CompiledObject]:
    """Fix includes, then compile the translation unit.
    Returns the temporary object on success."""
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


def build_units(func_units: dict[str, list[str]]) -> dict[str, CompiledObject]:
    """Compile each translation unit once. Returns compiled objects by path."""
    built: dict[str, CompiledObject] = {}
    for obj_path in func_units:
        compiled = build_unit(obj_path)
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


def check_single(func_name: str, full_diff: bool) -> int:
    """Check a single function, printing the full diff."""
    obj_path = find_unit_for_function(func_name)
    if obj_path is None:
        print(f"error: could not find function '{func_name}' in report.json", file=sys.stderr)
        return 1

    compiled = build_unit(obj_path)
    if compiled is None:
        return 1

    result = run_diff(obj_path, compiled.obj, func_name, capture=True)
    out = result.stdout if full_diff else collapse_matching(result.stdout)
    print(out, end="")
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    return result.returncode


def check_multiple(func_names: list[str]) -> int:
    """Check multiple functions, printing OK/FAIL summary for each."""
    func_units = resolve_functions(func_names)
    if not func_units:
        return 1

    built = build_units(func_units)
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
    ap.add_argument("--full-diff", action="store_true",
                    help="Show every diff line, including matching ones (default: collapse runs of 5+)")
    ap.add_argument("functions", nargs="+", metavar="function", help="Function name(s)")
    args = ap.parse_args()

    if args.summary:
        return check_multiple(args.functions)

    if len(args.functions) != 1:
        ap.error("pass exactly one function without --summary, "
                 "or use --summary to get PASS/FAIL lines for multiple functions")
    return check_single(args.functions[0], full_diff=args.full_diff)


if __name__ == "__main__":
    raise SystemExit(main())
