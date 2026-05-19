#!/usr/bin/env python3
"""
Run the decomp-permuter on a function, identified by name.

Usage:
  tools/permute.py <func_name> [permute_fn ...] [permuter_args...]
  tools/permute.py fn_802B7E34 tools/permute.py fn_802B7E34 my_helper
  # randomize ONLY my_helper tools/permute.py fn_802B7E34 fn_802B7E34 my_helper
  # randomize both tools/permute.py fn_802B7E34 helper_a helper_b      #
  randomize multiple tools/permute.py fn_802B7E34 -j4 tools/permute.py
  fn_802B7E34 --reimport             # re-import even if exists

The first positional argument is the function whose object code is matched
against the target binary. Any subsequent positional arguments form the set of
functions to randomize each iteration; the randomizer picks one of them
uniformly per iteration.

If no extra positional arguments are given, the randomizer permutes the match
target itself (the usual behaviour). If one or more are given, ONLY those
functions are randomized — the match target is permuted only if you list it
explicitly. This lets you focus the permuter on a single helper while still
scoring against the caller's compiled output.

On Ctrl+C, prints the best diff found so far.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
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
# Vendored decomp-permuter lives next to this script (in the harness),
# not in the melee tree.
PERMUTER = Path(__file__).resolve().parent / "decomp-permuter"
NONMATCHINGS = ROOT / "nonmatchings"


def find_unit_for_function(func_name: str) -> Optional[str]:
    with REPORT_PATH.open("r") as f:
        for unit in json.load(f).get("units", []):
            for function in unit.get("functions", []):
                if function.get("name") == func_name:
                    return unit.get("name", "").removeprefix("main/")
    return None


def diff_size(d: Path) -> int:
    """Size of the diff.diff file in this output directory."""
    diff_file = d / "diff.diff"
    if diff_file.exists():
        return len(diff_file.read_text())
    return 0


def _objdiff_percent(target_o: Path, cand_o: Path, func_name: str) -> Optional[float]:
    try:
        result = subprocess.run(
            [
                objdiff_cli(), "diff",
                "--format", "percent",
                "-c", "functionRelocDiffs=data_value",
                "-1", str(target_o),
                "-2", str(cand_o),
                func_name,
            ],
            capture_output=True,
            text=True,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None



DISCLAIMER = "Note: percentages calculated from synthesized TU, not original source"


def _compile_candidate(nonmatch_dir: Path, source_c: Path, cand_o: Path) -> bool:
    """Compile source_c via the nonmatch_dir's compile.sh into cand_o."""
    compile_sh = nonmatch_dir / "compile.sh"
    # compile.sh resolves $3 with realpath, which on macOS errors on a
    # non-existent path — touch the output first.
    cand_o.touch()
    try:
        result = subprocess.run(
            ["bash", str(compile_sh), str(source_c), "x", str(cand_o)],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return result.returncode == 0


def show_best(
    nonmatch_dir: Path, func_name: str, baseline: Optional[float]
) -> None:
    """Pick the candidate with the highest objdiff percent against target.o.
    The baseline is what the wrapper printed at the top — used here to show
    the delta. Returns early without printing anything when no candidate
    beats the baseline (the live counter already showed where we ended up)."""
    target_o = nonmatch_dir / "target.o"
    cand_o = nonmatch_dir / "best.o"

    best_dir: Optional[Path] = None
    best_percent: Optional[float] = baseline
    for d in sorted(nonmatch_dir.iterdir()):
        if not d.is_dir() or not d.name.startswith("output-"):
            continue
        source_c = d / "source.c"
        if not source_c.exists():
            continue
        if not _compile_candidate(nonmatch_dir, source_c, cand_o):
            continue
        pct = _objdiff_percent(target_o, cand_o, func_name)
        if pct is None:
            continue
        if best_percent is None or pct > best_percent:
            best_percent = pct
            best_dir = d

    if best_dir is None or best_percent is None:
        return

    if baseline is not None:
        delta = best_percent - baseline
        print(f"\nBest score: {best_percent:.2f}% ({delta:+.2f}%)")
    else:
        print(f"\nBest score: {best_percent:.2f}%")
    diff_file = best_dir / "diff.diff"
    if diff_file.exists():
        print(diff_file.read_text(), end="")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "func_name",
        help="Function whose .o output is matched against the target binary",
    )
    parser.add_argument(
        "permute_fn_names",
        nargs="*",
        metavar="permute_fn",
        help="Functions to randomize each iteration. If omitted, defaults to "
        "func_name (standard behaviour). If given, ONLY these functions are "
        "randomized — func_name is permuted only if explicitly listed.",
    )
    parser.add_argument(
        "--reimport",
        action="store_true",
        help="Re-import the function even if it already exists",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        metavar="SECONDS",
        help="Stop the permuter after this many seconds",
    )
    args, permuter_args = parser.parse_known_args()

    func_name = args.func_name
    permute_fn_names = args.permute_fn_names
    unit = find_unit_for_function(func_name)
    if unit is None:
        print(f"error: could not find function '{func_name}' in report.json", file=sys.stderr)
        return 1

    c_file = SRC_ROOT / f"{unit}.c"
    asm_file = ROOT / "build/GALE01/asm" / f"{unit}.s"

    if not c_file.exists():
        print(f"error: C file not found: {c_file}", file=sys.stderr)
        return 1
    if not asm_file.exists():
        print(f"error: assembly file not found: {asm_file}", file=sys.stderr)
        return 1

    nonmatch_dir = NONMATCHINGS / func_name
    permute_fns_file = nonmatch_dir / "permute_fns.txt"

    # Reimport if source is newer than the last import, or if the requested
    # randomization set has changed.
    if nonmatch_dir.exists() and not args.reimport:
        base_c = nonmatch_dir / "base.c"
        if base_c.exists() and c_file.stat().st_mtime > base_c.stat().st_mtime:
            args.reimport = True
        else:
            existing_permute: list = []
            if permute_fns_file.exists():
                existing_permute = [
                    line.strip()
                    for line in permute_fns_file.read_text().splitlines()
                    if line.strip()
                ]
            if existing_permute != list(permute_fn_names):
                args.reimport = True

    if args.reimport and nonmatch_dir.exists():
        shutil.rmtree(nonmatch_dir)

    if not nonmatch_dir.exists():
        result = subprocess.run(
            [
                sys.executable,
                str(PERMUTER / "import.py"),
                str(c_file),
                str(asm_file),
                "--function", func_name,
                "--no-prune",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            sys.stdout.write(result.stdout)
            sys.stderr.write(result.stderr)
            return result.returncode

    if permute_fn_names:
        permute_fns_file.write_text("\n".join(permute_fn_names) + "\n")
    elif permute_fns_file.exists():
        permute_fns_file.unlink()

    if not permuter_args:
        permuter_args = ["-j8", "--better-only", "--stop-on-zero"]

    # Compute the synthesized-TU baseline percent up front so we can print it
    # alongside the disclaimer and pass it to the inner permuter (which uses
    # it for the live counter's delta).
    baseline = _objdiff_percent(
        nonmatch_dir / "target.o", nonmatch_dir / "base.o", func_name
    )
    print(DISCLAIMER, flush=True)
    if baseline is not None:
        print(f"Baseline: {baseline:.2f}%", flush=True)
        permuter_args = permuter_args + ["--baseline-percent", f"{baseline:.4f}"]

    try:
        proc = subprocess.Popen(
            [sys.executable, str(PERMUTER / "permuter.py"), str(nonmatch_dir)] + permuter_args,
            cwd=ROOT,
            start_new_session=True,
        )
        proc.wait(timeout=args.timeout)
    except (KeyboardInterrupt, subprocess.TimeoutExpired):
        # SIGINT (not SIGTERM) lets the workers raise KeyboardInterrupt and
        # tear down multiprocessing semaphores cleanly. Fall back to SIGTERM
        # if it doesn't exit promptly.
        os.killpg(proc.pid, signal.SIGINT)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait()

    show_best(nonmatch_dir, func_name, baseline)
    shutil.rmtree(nonmatch_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
