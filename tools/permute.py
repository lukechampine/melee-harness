#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["tree-sitter", "tree-sitter-c", "libclang"]
# ///
"""
Source-level permuter for melee. Unlike the vendored decomp-permuter (which
mutates a macro-expanded, pretty-printed copy of the source), this mutates the
**real** translation unit text via tree-sitter byte-span edits (src_mutate.py),
compiles the real TU with the exact mwcc command from build.ninja
(ninja_compile.py), and scores in-process against the real build target
(scorer.py, a vendored objdump scorer, isolated to one function). A win is
printed as a unified diff that applies straight to src/.../*.c with `git apply`.

Usage:
  permute.py <func_name> [permute_fn ...] [options]

  <func_name>     function whose object code is scored against the target
  [permute_fn]    function(s) to mutate each iteration (default: func_name).
                  If given, ONLY these are mutated (one chosen per iteration);
                  func_name is mutated only if listed. They must live in the
                  same translation unit as func_name.

Options:
  -j N            worker threads (default 8)
  --timeout S     stop after S seconds
  --seed N        base RNG seed (default 0)
  --keep-prob P   probability of stacking another mutation vs. restarting from
                  the original source each step (default 0.25)
  --apply MODE    write the best candidate back to the real source:
                    match   (default) only on a 100% match
                    always  even for a partial improvement
                    never   leave the source untouched
  --max-iters N   stop after N compiled candidates

The search always stops as soon as a 100% match (score 0) is found. On Ctrl+C /
timeout / max-iters, prints the best diff found so far.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import random
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

_HARNESS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_HARNESS / "tools"))            # for sibling modules

import src_mutate  # noqa: E402
import type_oracle  # noqa: E402
from ninja_compile import (  # noqa: E402
    ROOT,
    build_pch,
    compile_batch,
    compile_source_text,
    find_unit_for_function,
)
from objdiff_path import objdiff_cli  # noqa: E402
from scorer import Scorer  # noqa: E402

PPC_OBJDUMP = ROOT / "build/binutils/powerpc-eabi-objdump"


def objdump_command(fn: str) -> str:
    # Match decomp-permuter's PPC defaults (raw bytes + relocs) so its parser
    # works, then restrict to one symbol to isolate the function in a real,
    # multi-function TU object.
    return f"{PPC_OBJDUMP} -dr -EB -mpowerpc -M broadway --disassemble={fn}"


def make_scorer(unit: str, fn: str) -> Scorer:
    return Scorer(
        str(ROOT / f"build/GALE01/obj/{unit}.o"),
        stack_differences=True,
        algorithm="difflib",
        debug_mode=False,
        ign_branch_targets=False,
        objdump_command=objdump_command(fn),
    )


def objdiff_percent(unit: str, fn: str, cand_o: Path) -> Optional[float]:
    try:
        r = subprocess.run(
            [objdiff_cli(), "diff", "--format", "percent",
             "-c", "functionRelocDiffs=data_value",
             "-1", str(ROOT / f"build/GALE01/obj/{unit}.o"),
             "-2", str(cand_o), fn],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if r.returncode != 0:
        return None
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


def unified_diff(unit: str, base: bytes, cand: bytes) -> str:
    import difflib
    rel = f"src/{unit}.c"
    a = base.decode("utf-8", "replace").splitlines(keepends=True)
    b = cand.decode("utf-8", "replace").splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(a, b, fromfile=f"a/{rel}", tofile=f"b/{rel}")
    )


@dataclass
class Shared:
    base_score: int
    base_source: bytes
    unit: str
    fn: str
    keep_prob: float
    max_iters: Optional[int]
    use_pch: bool = False
    split: int = 0
    pch_path: Optional[Path] = None
    types: Optional[dict] = None   # clang type oracle (base spans -> type)
    batch: int = 8
    lock: threading.Lock = field(default_factory=threading.Lock)
    stop: threading.Event = field(default_factory=threading.Event)
    best_score: int = 0
    best_source: Optional[bytes] = None
    best_percent: Optional[float] = None
    iters: int = 0
    compiles_failed: int = 0
    n_mutate_none: int = 0
    n_dup: int = 0
    prof_mutate: float = 0.0
    prof_compile: float = 0.0
    prof_score: float = 0.0
    seen_source: Set[bytes] = field(default_factory=set)
    seen_asm: Set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.best_score = self.base_score


def report_find(sh: Shared, score: int, source: bytes, cand_o: Path) -> None:
    pct = objdiff_percent(sh.unit, sh.fn, cand_o)
    delta = score - sh.base_score
    pstr = f", {pct:.2f}%" if pct is not None else ""
    print(f"\n*** improvement: score {score} (delta {delta:+d}{pstr}) ***")
    print(unified_diff(sh.unit, sh.base_source, source), end="")
    sys.stdout.flush()


def print_profile(sh: Shared, elapsed: float, jobs: int) -> None:
    worker_wall = jobs * elapsed
    n_mut = sh.iters + sh.compiles_failed + sh.n_dup + sh.n_mutate_none
    n_comp = sh.iters + sh.compiles_failed
    n_score = sh.iters

    def row(name: str, total: float, count: int) -> None:
        mean = (total / count * 1000) if count else 0.0
        pct = (total / worker_wall * 100) if worker_wall else 0.0
        print(f"  {name:8s} {total:8.2f}s  {pct:5.1f}%  {mean:7.2f} ms/call  ({count} calls)")

    print("\n--- profile (summed across workers) ---")
    print(f"  wall {elapsed:.1f}s x {jobs} workers = {worker_wall:.1f}s worker-time; "
          f"{sh.iters} scored ({sh.iters / elapsed:.1f}/s)")
    print(f"  compile-fail {sh.compiles_failed}, dup {sh.n_dup}, no-mutation {sh.n_mutate_none}")
    row("mutate", sh.prof_mutate, n_mut)
    row("compile", sh.prof_compile, n_comp)
    row("score", sh.prof_score, n_score)
    acc = sh.prof_mutate + sh.prof_compile + sh.prof_score
    if worker_wall:
        print(f"  accounted {acc:.1f}s ({acc / worker_wall * 100:.0f}% of worker-time; "
              f"rest = lock/dedup/tempfile/idle)")


def worker(sh: Shared, mutators: Dict[str, "src_mutate.Mutator"],
           mutate_fns: List[str], seed: int) -> None:
    rng = random.Random(seed)
    scorer = make_scorer(sh.unit, sh.fn)
    base_prefix = sh.base_source[:sh.split]
    # Parse the base source once per worker; ~75% of steps restart from it
    # (keep_prob), so reusing this tree avoids re-parsing the whole .c each time.
    base_tree = src_mutate.parse(sh.base_source)
    base_fns = {
        name: src_mutate.find_function(base_tree.root_node, name)
        for name in set(mutate_fns)
    }
    cur = sh.base_source
    tm = tc = ts = 0.0          # per-thread phase timers (merged at exit)
    n_none = 0
    try:
        while not sh.stop.is_set():
            # --- build a batch of distinct, compilable-looking candidates ---
            t0 = time.perf_counter()
            cands: list = []
            attempts = 0
            while len(cands) < sh.batch and attempts < sh.batch * 4:
                attempts += 1
                if cur is not sh.base_source and rng.random() >= sh.keep_prob:
                    cur = sh.base_source
                mfn = rng.choice(mutate_fns)
                if cur is sh.base_source:
                    cand = mutators[mfn].step(cur, rng, tree=base_tree,
                                              fn=base_fns[mfn], types=sh.types)
                else:
                    cand = mutators[mfn].step(cur, rng)
                if cand is None:
                    n_none += 1
                    cur = sh.base_source
                    continue
                cur = cand
                h = hashlib.sha256(cand).digest()
                with sh.lock:
                    dup = h in sh.seen_source
                    if dup:
                        sh.n_dup += 1   # live, for the status line (free: lock held)
                    elif len(sh.seen_source) < 200_000:
                        sh.seen_source.add(h)
                if dup:
                    continue
                cands.append(cand)
            tm += time.perf_counter() - t0
            if not cands:
                continue

            # --- compile the whole batch in one mwcc invocation ---
            t0 = time.perf_counter()
            if sh.use_pch and all(c[:sh.split] == base_prefix for c in cands):
                sources = [c[sh.split:].decode("utf-8", "surrogateescape") for c in cands]
                objs, cleanups = compile_batch(sh.unit, sources, prefix_pch=sh.pch_path)
            else:
                sources = [c.decode("utf-8", "surrogateescape") for c in cands]
                objs, cleanups = compile_batch(sh.unit, sources)
            tc += time.perf_counter() - t0

            # --- score each candidate ---
            try:
                for cand, obj in zip(cands, objs):
                    if sh.stop.is_set():
                        break
                    if obj is None:
                        with sh.lock:
                            sh.compiles_failed += 1
                        continue
                    if not obj.exists():
                        continue  # vanished (e.g. tmpdir cleaned during shutdown)
                    t0 = time.perf_counter()
                    try:
                        score, asm_hash = scorer.score(str(obj))
                    except (subprocess.CalledProcessError, OSError) as e:
                        # Ctrl-C reaches the in-flight objdump as SIGINT (the only
                        # check_output in the loop). Shut down quietly instead of
                        # letting the worker thread dump a traceback.
                        if sh.stop.is_set() or (
                            isinstance(e, subprocess.CalledProcessError)
                            and (e.returncode or 0) < 0
                        ):
                            sh.stop.set()
                            break
                        raise
                    ts += time.perf_counter() - t0
                    with sh.lock:
                        sh.iters += 1
                        improved = score < sh.best_score
                        is_zero = score == 0
                        new_asm = asm_hash not in sh.seen_asm
                        if improved:
                            sh.best_score = score
                            sh.best_source = cand
                        if (improved or is_zero) and new_asm:
                            sh.seen_asm.add(asm_hash)
                            report_find(sh, score, cand, obj)
                        if is_zero:
                            sh.stop.set()  # 100% match: stop the whole search
                        if sh.max_iters is not None and sh.iters >= sh.max_iters:
                            sh.stop.set()
            finally:
                for c in cleanups:
                    c.cleanup()
    finally:
        with sh.lock:
            sh.prof_mutate += tm
            sh.prof_compile += tc
            sh.prof_score += ts
            sh.n_mutate_none += n_none


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("func_name")
    ap.add_argument("permute_fn_names", nargs="*", metavar="permute_fn")
    ap.add_argument("-j", type=int, default=8, dest="jobs")
    ap.add_argument("--timeout", type=float, default=None)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--keep-prob", type=float, default=0.25)
    ap.add_argument("--apply", choices=["match", "always", "never"], default="match",
                    help="write the best candidate back to the real source: "
                         "'match' (default) only on a 100%% match, 'always' even "
                         "for a partial improvement, 'never' to leave it alone")
    ap.add_argument("--max-iters", type=int, default=None)
    ap.add_argument("--profile", action="store_true",
                    help="print a per-phase timing breakdown on exit")
    ap.add_argument("--no-pch", action="store_true",
                    help="disable the precompiled-header fast path (compile full TU each time)")
    ap.add_argument("--batch", type=int, default=16, metavar="K",
                    help="candidates compiled per mwcc invocation, per worker "
                         "(amortizes process startup; default 16, 1 to disable)")
    args = ap.parse_args()

    fn = args.func_name
    unit = find_unit_for_function(fn)
    if unit is None:
        print(f"error: function '{fn}' not in report.json", file=sys.stderr)
        return 1

    c_file = ROOT / f"src/{unit}.c"
    if not c_file.exists():
        print(f"error: source not found: {c_file}", file=sys.stderr)
        return 1

    mutate_fns = args.permute_fn_names or [fn]
    base_source = c_file.read_bytes()

    # Validate mutate targets exist in this TU, and build per-function mutators.
    tree = src_mutate.parse(base_source)
    mutators: Dict[str, src_mutate.Mutator] = {}
    for mfn in mutate_fns:
        if src_mutate.find_function(tree.root_node, mfn) is None:
            print(f"error: function '{mfn}' not found in {c_file}", file=sys.stderr)
            return 1
        mutators[mfn] = src_mutate.Mutator(mfn)

    # Baseline: compile the unmodified real source and score it.
    scorer = make_scorer(unit, fn)
    base_co = compile_source_text(unit, base_source.decode("utf-8", "surrogateescape"),
                                  show_errors=True)
    if base_co is None:
        print("error: baseline source failed to compile", file=sys.stderr)
        return 1
    base_score, _ = scorer.score(str(base_co.obj))
    base_pct = objdiff_percent(unit, fn, base_co.obj)
    base_co.tmpdir.cleanup()

    pstr = f" ({base_pct:.2f}%)" if base_pct is not None else ""
    print(f"permuting {fn} in {unit}.c; mutating {', '.join(mutate_fns)}")
    print(f"baseline score {base_score}{pstr}; {args.jobs} workers; "
          f"apply={args.apply}")
    if base_score == 0:
        print("baseline already matches (score 0).")
        return 0

    # Precompiled-header fast path: precompile the TU's constant header/preproc
    # prefix once, then recompile only the mutated body per candidate (~2x).
    use_pch = False
    split = 0
    pch_path: Optional[Path] = None
    if not args.no_pch:
        split = src_mutate.prefix_split(base_source)
        if 0 < split < len(base_source):
            pch_path = build_pch(
                unit, base_source[:split].decode("utf-8", "surrogateescape"), quiet=False)
    if pch_path is not None:
        # Fidelity gate: the PCH body compile must score identically to the full
        # compile against the real target, or we fall back to full compiles.
        body0 = base_source[split:].decode("utf-8", "surrogateescape")
        pch_co = compile_source_text(unit, body0, prefix_pch=pch_path, show_errors=True)
        pch_score = None
        if pch_co is not None:
            pch_score, _ = scorer.score(str(pch_co.obj))
            pch_co.tmpdir.cleanup()
        if pch_score == base_score:
            use_pch = True
        else:
            print(f"PCH: disabled (fidelity gate pch={pch_score} vs full={base_score}); "
                  "using full compiles")
            pch_path.unlink(missing_ok=True)
            pch_path = None

    # Type oracle (clang): expression types so temp_for_expr can extract
    # subexpressions into typed temporaries. One clang parse of the base TU at
    # startup; passes then just look up spans. Auto-disables (permuter still
    # runs, minus temp_for_expr) if libclang / compile_commands are unavailable.
    types: dict = {}
    flags = type_oracle.clang_flags_for(c_file, ROOT / "compile_commands.json")
    if type_oracle.available() and flags is not None:
        types = type_oracle.build_oracle(c_file, flags)
    print(f"type oracle: {len(types)} expression types"
          if types else "type oracle: unavailable; temp_for_expr disabled")

    sh = Shared(
        base_score=base_score, base_source=base_source, unit=unit, fn=fn,
        keep_prob=args.keep_prob, max_iters=args.max_iters,
        use_pch=use_pch, split=split, pch_path=pch_path, types=types,
        batch=max(1, args.batch),
    )

    # Ctrl-C just asks the workers to stop. Handling SIGINT here (rather than
    # relying on KeyboardInterrupt) keeps the whole shutdown path -- the join,
    # the cleanup -- free of stray tracebacks. Worker subprocesses still receive
    # the terminal's group SIGINT directly; worker() absorbs that.
    prev_sigint = signal.signal(signal.SIGINT, lambda *_: sh.stop.set())

    threads = [
        threading.Thread(target=worker, args=(sh, mutators, mutate_fns,
                                               args.seed * 1000 + i), daemon=True)
        for i in range(args.jobs)
    ]
    for t in threads:
        t.start()

    start = time.time()
    try:
        while not sh.stop.is_set() and any(t.is_alive() for t in threads):
            time.sleep(0.2)
            with sh.lock:
                it, bs, cf, nd = sh.iters, sh.best_score, sh.compiles_failed, sh.n_dup
            elapsed = time.time() - start
            rate = it / elapsed if elapsed else 0.0
            total = nd + it + cf
            dup_pct = (nd / total * 100) if total else 0.0
            sys.stderr.write(
                f"\r{int(elapsed)}s  iters={it} ({rate:.1f}/s)  "
                f"best={bs}  dup={dup_pct:.0f}%  compile-fail={cf}   ")
            sys.stderr.flush()
            if args.timeout is not None and elapsed >= args.timeout:
                sh.stop.set()
    finally:
        # Keep the stop-setting handler installed through the join: a group
        # SIGINT pending for the main thread must not fire the default handler
        # (KeyboardInterrupt) while we're joining workers. Restore it last.
        sh.stop.set()
        for t in threads:
            t.join(timeout=10)
        signal.signal(signal.SIGINT, prev_sigint)
        sys.stderr.write("\n")
        if pch_path is not None:
            pch_path.unlink(missing_ok=True)

    if args.profile:
        print_profile(sh, time.time() - start, args.jobs)

    with sh.lock:
        best_score, best_source = sh.best_score, sh.best_source
    if best_source is None or best_score >= base_score:
        print(f"no improvement (best {best_score}, baseline {base_score}).")
        return 0

    matched = best_score == 0
    print(f"\nbest score {best_score} (baseline {base_score})"
          + ("  -- 100% match!" if matched else ""))

    do_apply = args.apply == "always" or (args.apply == "match" and matched)
    if do_apply:
        c_file.write_bytes(best_source)
        print(f"applied best candidate to {c_file}")
    else:
        print(f"(not applied; apply the diff above, or re-run with "
              f"--apply=always to write to {c_file})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
