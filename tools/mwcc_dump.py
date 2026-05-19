#!/usr/bin/env python3
"""Compile one TU with the mwcc_debug compiler to produce pcdump.txt.

The debug compiler (build/compilers/<mw>/mwcceppc_debug.exe + MWDBG326.dll)
emits IR-optimizer and PPC-backend listings to ./pcdump.txt. It must be run
WITHOUT the sjiswrap wrapper (sjiswrap + the debug DLL bus-errors), so this
reuses the exact cflags parsed from build.ninja.

This script overlays onto a melee checkout (run as <melee>/tools/mwcc_dump.py,
like checkdiff.py); its source of truth lives in the melee-harness repo. It
reads build.ninja / compiles sources / writes pcdump.txt in the melee checkout,
but the patched wibo is a harness build artifact resolved via wibo_path().

Runner: defaults to "auto" = the patched wibo (built in the harness, see
wibo_path()) with a Wine fallback on SIGBUS. The patched wibo rewrites the
LJMP64 32<->64-bit trampoline (fixes the formatoperands SIGBUS the stock
wibo hits on @NNN scratch temps) and relocates nested PEs (fixes the
sjiswrap crash); it is ~14x faster than Wine and produces a byte-identical
object and an identical pcdump (modulo the uninitialized LOOPWEIGHT garbage
field). The Wine fallback covers a missing patched binary (stock wibo still
crashes) or a new crash slipping through.

Build the patched wibo and the mwcc_debug compiler per melee-harness/README.md.

Usage: tools/mwcc_dump.py src/melee/it/items/itarwinglaser.c
       tools/mwcc_dump.py --runner wibo src/melee/it/items/itarwinglaser.c
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


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
        description="Compile one TU with the mwcc_debug compiler to produce pcdump.txt."
    )
    parser.add_argument("source", help="source .c file from the repo root")
    parser.add_argument(
        "--runner",
        choices=("auto", "wibo", "wine"),
        default="auto",
        help="execution backend (default auto = patched wibo with Wine "
        "fallback on SIGBUS; wibo = patched wibo only; wine = Wine only)",
    )
    return parser.parse_args()


def format_functions(funcs: list[str]) -> str:
    if len(funcs) <= 20:
        return repr(funcs)
    return f"{len(funcs)} functions, first={funcs[0]!r}, last={funcs[-1]!r}"


def format_pass_counts(passes: list[str]) -> str:
    counts = Counter(passes)
    ordered = []
    for name in passes:
        if name not in ordered:
            ordered.append(name)
    return ", ".join(f"{name}={counts[name]}" for name in ordered)


def summarize_dump(proc: subprocess.CompletedProcess[str], runner: str) -> None:
    pcdump = ROOT / "pcdump.txt"
    if pcdump.exists():
        body = pcdump.read_text(errors="replace")
        funcs = re.findall(r"^Starting function (\S+)", body, re.M)
        passes = re.findall(r"^(?:BEFORE|AFTER|FINAL) .+", body, re.M)
        last = body.rstrip().splitlines()[-1] if body.strip() else ""
        print(f"\n[mwcc_dump] runner={runner} rc={proc.returncode} pcdump.txt: "
              f"{len(body.splitlines())} lines, functions={format_functions(funcs)}")
        print(f"[mwcc_dump] backend pass counts: {format_pass_counts(passes)}")
        print(f"[mwcc_dump] last line: {last!r}")
    else:
        print(f"[mwcc_dump] runner={runner} rc={proc.returncode}; no pcdump.txt produced")


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
    src = args.source
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

    summarize_dump(proc, runner)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
