"""Resolve the path to the harness-local objdiff-cli binary.

Resolution order:
  1. $OBJDIFF_CLI                              explicit override
  2. <harness>/bin/objdiff-cli                 installed by ../setup.sh
  3. <harness>/objdiff/target/release/objdiff-cli   raw cargo build output
  4. objdiff-cli on PATH                       last-resort fallback

<harness> is the repo root, located relative to this file (via resolve(), so
it works whether the tools run in place or are symlinked into a melee
checkout).
"""

import os
import shutil
from pathlib import Path

_HARNESS_ROOT = Path(__file__).resolve().parents[1]


def objdiff_cli() -> str:
    override = os.environ.get("OBJDIFF_CLI")
    if override:
        return override
    for cand in (
        _HARNESS_ROOT / "bin" / "objdiff-cli",
        _HARNESS_ROOT / "objdiff" / "target" / "release" / "objdiff-cli",
    ):
        if cand.is_file():
            return str(cand)
    found = shutil.which("objdiff-cli")
    if found:
        return found
    raise SystemExit(
        "objdiff-cli not found. Build it with ./setup.sh (see README) "
        "or set $OBJDIFF_CLI to an existing binary."
    )
