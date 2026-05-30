"""Locate the melee checkout for the in-place harness tools.

These tools live in `melee-harness/tools/`, *not* inside the melee checkout, so
they can't derive the checkout from their own location. `resolve_root()` finds
it from, in order:

  1. ``$MELEE_ROOT``                 explicit override
  2. ``$CLAUDE_PROJECT_DIR``         Claude Code's project dir
  3. a walk up from the current directory for a ``build/GALE01`` marker
     (so you can run a tool from anywhere inside a melee checkout)
  4. the current directory                                (last resort)

The result is always absolute: a relative root (e.g. ``MELEE_ROOT=.``) leaves
mwcc ``-precompile`` output paths un-relativizable, which mwcc rejects with
OSErr -43.
"""

import os
from pathlib import Path
from typing import Optional

# A directory is a melee checkout if it has the built object tree under here.
_MARKER = ("build", "GALE01")


def find_checkout(start: Optional[Path] = None) -> Optional[Path]:
    """Walk up from `start` (default: cwd) for a dir containing build/GALE01.
    Returns the checkout root, or None if none is found."""
    base = (start or Path.cwd()).resolve()
    for d in (base, *base.parents):
        if d.joinpath(*_MARKER).is_dir():
            return d
    return None


def resolve_root() -> Path:
    """Absolute path to the melee checkout (see module docstring for order)."""
    env = os.environ.get("MELEE_ROOT") or os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return Path(env).resolve()
    return (find_checkout() or Path.cwd()).resolve()
