#!/usr/bin/env python3
"""Stack permuter: align r1 offsets via padding and decl reordering.

Calls `objdiff-cli diff … -f stack` to score a function. The stack format
prints a single line ``frame_diff,mismatches``:

    frame_diff = (our frame size) - (target frame size)   (bytes)
    mismatches = number of instructions whose r1 offset disagrees

``0,0`` is a perfect match. ``-x,y`` (x>0) means our frame is short by x
bytes — this tool inserts ``u8 _padX[N];`` declarations to grow it.
``x,y`` (x>0) means our frame is too large; padding can't help.

When ``frame_diff == 0`` but mismatches remain (right frame size, wrong
slot offsets), the tool also tries reordering the existing decl block —
MWCC roughly assigns slots in declaration order, so swapping two locals
shifts everything between them. Reordering is also tried as a follow-up
step after a successful pad search if offsets still disagree.

Decls that carry an initializer (``Type name = expr;``) are split during
the reorder render: pure declarations move with the permutation, while
the ``name = expr;`` assignment is re-emitted in original decl order
right after the decl block. This keeps initializer side effects in
their source-declared sequence regardless of how the decls are
permuted. Reorder will not split decls that are ``static``/``const``,
have struct/array literal initializers, are multi-decls
(``int a, b = 5;``), or use function-pointer / array-of-pointer
declarators — those move as single units (or skip reorder entirely).

Usage:
    uv run tools/stack_permute.py <function> [--timeout 30] [--max-pads 4] [-j N]

The actual source file is never modified during the search. Each worker
writes a temp ``<file>.permute_WN.c`` next to the original (so ``#include``
paths still resolve) and compiles it directly via a ``compile.sh`` cribbed
from the decomp-permuter's scratch dirs (no ``ninja`` involved). On success
the best placement is applied to the actual source; on Ctrl-C every temp
file is cleaned up.

Existing pad decls (``PAD_STACK(N);`` macros and ``u8 _pad*[N];`` variable
declarations) in the function are removed up front so the search has a clean
baseline. If the best run also achieves a perfect match, the source is left
without those pads.
"""

from __future__ import annotations

import argparse
import atexit
import concurrent.futures as futures
import itertools
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / "build/GALE01/report.json"
SRC_ROOT = ROOT / "src"
NONMATCHINGS = ROOT / "nonmatchings"
SCORE_RE = re.compile(r"^\s*(-?\d+)\s*,\s*(-?\d+)\s*$")


# ---------------------------------------------------------------------------
# Project lookups
# ---------------------------------------------------------------------------


def find_unit_for_function(func: str) -> Optional[str]:
    with REPORT_PATH.open("r") as f:
        for unit in json.load(f).get("units", []):
            for function in unit.get("functions", []):
                if function.get("name") == func:
                    return unit.get("name", "").removeprefix("main/")
    return None


def find_compile_script() -> Path:
    """Reuse any existing ``nonmatchings/*/compile.sh`` (they're all identical)."""
    if NONMATCHINGS.exists():
        for p in sorted(NONMATCHINGS.iterdir()):
            cs = p / "compile.sh"
            if cs.exists():
                return cs
    raise RuntimeError(
        "no compile.sh found under nonmatchings/. "
        "Run `uv run tools/permute.py <any_function>` once to bootstrap one."
    )


# ---------------------------------------------------------------------------
# Source parsing
# ---------------------------------------------------------------------------


_PAD_VAR_RE = re.compile(r"\b_pad\w*\s*\[")


def _looks_like_pad_decl(line: str) -> bool:
    """True if `line` is a removable pad declaration we control.

    Matches both ``PAD_STACK(N);`` macro calls and explicit pad variables
    like ``u8 _padA[8];`` (any identifier matching ``_pad*`` followed by an
    array bracket). These exist purely to grow the frame; the search is
    free to delete them.
    """
    return "PAD_STACK" in line or bool(_PAD_VAR_RE.search(line))


@dataclass
class Decl:
    start: int  # inclusive line index
    end: int  # inclusive line index of the line with the terminating `;`
    is_pad_stack: bool
    # If the decl carries an initializer (``Type name = expr;``), reorder
    # cannot just shuffle the source line — that would also reorder the
    # side effects of ``expr``. Instead we split into a pure declaration
    # (which is freely permutable) and an assignment statement that stays
    # in original order. Both fields are populated even when no split is
    # possible: ``decl_lines`` then holds the original lines, ``assign_lines``
    # is None, and reorder will move the line as-is.
    decl_lines: Optional[List[str]] = None
    assign_lines: Optional[List[str]] = None


@dataclass
class FunctionLocation:
    src_text: str
    body_start: int  # line index of opening `{`
    body_end: int  # line index of matching closing `}`
    decl_block_end: int  # one-past-last decl-block line (the blank line)
    decls: List[Decl]
    indent: str
    pad_decls: List[Decl]  # existing PAD_STACK(...); / `u8 _pad*[N];` decls
    block_scope_decls: int  # count of decls in inner { } scopes (informational)
    block_scope_blocks: int  # count of inner { } scopes containing decls

    @property
    def n_decls(self) -> int:
        return len(self.decls)


_CTRL_KEYWORDS = (
    "if", "else", "while", "for", "do", "switch", "case", "default",
    "return", "break", "continue", "goto",
)


def _looks_like_statement(stripped: str) -> bool:
    """True if a line obviously starts a statement, not a declaration."""
    # Control flow keywords. Match `if(` or `if ` etc. but not identifiers
    # that happen to share a prefix (e.g., `iface`).
    for kw in _CTRL_KEYWORDS:
        if stripped == kw or stripped.startswith(kw + " ") \
                or stripped.startswith(kw + "(") or stripped.startswith(kw + ";"):
            return True
    # `something = ...;`, `something(...);`, `something++;`, `something->x = ...;`
    # all begin with a lowercase identifier followed by an operator. We do
    # NOT match `*` here because `s32* var;` would be a false positive —
    # we'd rather miss an occasional `var *= 2;` than mis-classify pointer
    # declarations.
    return bool(re.match(r"^[a-z_]\w*\s*([=()\[\.\-+/!<>&|?])", stripped))


# Storage classes / qualifiers that block init-split: ``static`` initializers
# fire at program load (not on each call), and ``const`` cannot be assigned
# to. Spotting these as whole words avoids matching e.g. ``constraint``.
_NO_SPLIT_PREFIX_RE = re.compile(r"\b(?:static|const|volatile|register|extern)\b")


def _find_top_level(text: str, target: str, start: int = 0) -> int:
    """Index of the first ``target`` char in ``text`` at paren/brace/bracket
    depth 0, ignoring string and char literals. Returns -1 if not found.
    Skips ``==``/``+=``/``!=`` etc. when target is ``=``.
    """
    depth = 0
    in_string = False
    in_char = False
    i = start
    n = len(text)
    while i < n:
        ch = text[i]
        if in_string:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == '"':
                in_string = False
        elif in_char:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == "'":
                in_char = False
        elif ch == '"':
            in_string = True
        elif ch == "'":
            in_char = True
        elif ch in "({[":
            depth += 1
        elif ch in ")}]":
            depth -= 1
        elif ch == target and depth == 0:
            if target == "=":
                # skip == and compound assignments
                if i + 1 < n and text[i + 1] == "=":
                    i += 2
                    continue
                if i > 0 and text[i - 1] in "!<>+-*/%&|^=":
                    i += 1
                    continue
            return i
        i += 1
    return -1


def _split_decl_init(
    lines: List[str], start: int, end: int, indent: str
) -> Tuple[List[str], Optional[List[str]]]:
    """Try to split ``Type name = expr;`` into pure decl + assignment.

    Returns ``(decl_lines, assign_lines or None)``. If the decl has no
    initializer, or the initializer can't be safely separated (struct/array
    literal, ``static``/``const``, function pointer, multi-decl, etc.),
    ``assign_lines`` is None and ``decl_lines`` is the original lines.
    """
    original = lines[start:end + 1]
    text = "\n".join(original)
    eq_pos = _find_top_level(text, "=")
    if eq_pos < 0:
        return original, None

    decl_part = text[:eq_pos].rstrip()
    # Reject storage-class / qualifier prefixes that change semantics on split.
    if _NO_SPLIT_PREFIX_RE.search(decl_part):
        return original, None
    # Reject multi-decls (commas at top level before `=`).
    if _find_top_level(decl_part, ",") >= 0:
        return original, None

    semi_pos = _find_top_level(text, ";", eq_pos)
    if semi_pos < 0:
        return original, None

    init_part = text[eq_pos + 1:semi_pos].strip()
    # Reject struct/array literal initializers — those need a compound
    # literal cast in C99 (``(Type){...}``) and MWCC may not support it.
    if init_part.startswith("{"):
        return original, None

    # Extract the variable name: last identifier in decl_part, optionally
    # followed by ``[...]`` (array). Function-pointer / array-of-pointer
    # declarators (``void (*f)(...)``, ``int (*a)[3]``) close their last
    # paren after the name and are too fiddly to handle here — bail.
    if decl_part.rstrip().endswith(")"):
        return original, None
    m = re.search(r"(\w+)\s*(?:\[[^\]]*\])*\s*$", decl_part)
    if not m:
        return original, None
    name = m.group(1)
    # Sanity: name shouldn't be a type-ish keyword.
    if name in {
        "void", "int", "char", "short", "long", "float", "double", "signed",
        "unsigned", "struct", "union", "enum",
    }:
        return original, None

    decl_text = decl_part + ";"
    init_text = " ".join(init_part.split())  # collapse newlines
    assign_text = f"{indent}{name} = {init_text};"
    return [decl_text], [assign_text]


def _parse_decls(lines: List[str], body_start: int) -> Tuple[List[Decl], int]:
    decls: List[Decl] = []
    i = body_start + 1
    while i < len(lines):
        stripped = lines[i].strip()
        # Blank lines may appear *between* decl groups (e.g. PAD_STACK is
        # often offset by a blank) -- skip them and keep parsing. We only
        # stop on a line that opens an inner block or starts a statement.
        if not stripped:
            i += 1
            continue
        if stripped.startswith("{"):
            break
        if _looks_like_statement(stripped):
            break
        depth = 0
        end = i
        found_semi = False
        aborted = False
        while end < len(lines) and not found_semi and not aborted:
            for ch in lines[end]:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth < 0:
                        # Walked past the function's closing brace; this
                        # "decl" ran off the end without a `;` at depth 0.
                        aborted = True
                        break
                elif ch == ";" and depth == 0:
                    found_semi = True
                    break
            if not found_semi and not aborted:
                end += 1
        if not found_semi or aborted:
            break
        is_pad = _looks_like_pad_decl(lines[i])
        first_line = lines[i]
        line_indent = first_line[: len(first_line) - len(first_line.lstrip())]
        if is_pad:
            decl_lines, assign_lines = lines[i:end + 1], None
        else:
            decl_lines, assign_lines = _split_decl_init(
                lines, i, end, line_indent
            )
        decls.append(
            Decl(
                start=i,
                end=end,
                is_pad_stack=is_pad,
                decl_lines=decl_lines,
                assign_lines=assign_lines,
            )
        )
        i = end + 1
    return decls, i


def _find_body_end(lines: List[str], body_start: int) -> int:
    """Return line index of the `}` that closes the brace at `body_start`."""
    depth = 0
    for i in range(body_start, len(lines)):
        for ch in lines[i]:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return i
    return len(lines) - 1


def _count_block_scope_decls(
    lines: List[str], start: int, end: int
) -> Tuple[int, int]:
    """Count decls inside top-level inner ``{ … }`` scopes within [start, end].

    Returns (decl_count, block_count). Only counts blocks that contain at
    least one decl. Nested inner blocks aren't double-counted: when we
    enter a top-level inner block, ``_parse_decls`` parses just its leading
    decls, and we then skip past its closing `}` before looking for siblings.
    """
    decls = 0
    blocks = 0
    depth = 0  # 0 = before/after function body; 1 = inside body proper
    i = start
    while i <= end:
        line = lines[i]
        col = 0
        while col < len(line):
            ch = line[col]
            if ch == "{":
                # `body_start`'s `{` brings depth 0 → 1; we skip it.
                # An inner block opens when we see `{` while depth == 1.
                if depth == 1:
                    inner_decls, _ = _parse_decls(lines, i)
                    if inner_decls:
                        blocks += 1
                        decls += len(inner_decls)
                depth += 1
            elif ch == "}":
                depth -= 1
            col += 1
        i += 1
    return decls, blocks


def locate_function(src_text: str, func: str) -> FunctionLocation:
    lines = src_text.split("\n")
    sig_pat = re.compile(rf"^[\w][\w\s\*&]*\b{re.escape(func)}\s*\(")
    for i, line in enumerate(lines):
        if not sig_pat.search(line):
            continue
        body_start: Optional[int] = None
        for j in range(i, min(i + 25, len(lines))):
            stripped = lines[j].strip()
            if "{" in lines[j]:
                body_start = j
                break
            if stripped.endswith(";"):
                break
        if body_start is None:
            continue
        body_end = _find_body_end(lines, body_start)
        all_decls, decl_block_end = _parse_decls(lines, body_start)
        # Pull out pad decls (PAD_STACK macros and `u8 _pad*[N];` variables);
        # their presence is recorded on the location so we can drop them when
        # rendering. The decl list we permute over is everything *but* pads.
        pad_decls = [d for d in all_decls if d.is_pad_stack]
        decls = [d for d in all_decls if not d.is_pad_stack]
        indent = "    "
        if decls:
            first = lines[decls[0].start]
            indent = first[: len(first) - len(first.lstrip())]
        elif pad_decls:
            line = lines[pad_decls[0].start]
            indent = line[: len(line) - len(line.lstrip())]
        block_decls, block_count = _count_block_scope_decls(
            lines, body_start, body_end
        )
        return FunctionLocation(
            src_text=src_text,
            body_start=body_start,
            body_end=body_end,
            decl_block_end=decl_block_end,
            decls=decls,
            indent=indent,
            pad_decls=pad_decls,
            block_scope_decls=block_decls,
            block_scope_blocks=block_count,
        )
    raise RuntimeError(f"could not locate function {func}")


# ---------------------------------------------------------------------------
# Source rendering
# ---------------------------------------------------------------------------


def _line_for_position(loc: FunctionLocation, pos: int) -> int:
    """Translate a decl-relative position (0..n_decls) into a source-line index."""
    pos = max(0, min(pos, loc.n_decls))
    if pos < loc.n_decls:
        return loc.decls[pos].start
    if loc.decls:
        return loc.decls[-1].end + 1
    return loc.body_start + 1


def _pad_line(loc: FunctionLocation, label: Optional[str], size: int, pos: int) -> str:
    """A pad inserted *after* every decl is conventionally written as
    ``PAD_STACK(N);`` (a statement) rather than ``u8 _padX[N];`` (which
    would have to live in the decl block proper). Anywhere else, we still
    need a real declaration so it sits among the other locals."""
    if pos == loc.n_decls:
        return f"{loc.indent}PAD_STACK({size});"
    return f"{loc.indent}u8 _pad{label}[{size}];"


def render_source(
    loc: FunctionLocation,
    pads: List[Tuple[int, int]],
    strip_pad_stack: bool = True,
) -> str:
    """Return source with optional pad-decl removal and `pads = [(size, pos)]`
    inserted.

    ``strip_pad_stack=True`` (the default, used by the search-based path)
    drops any existing ``PAD_STACK(N);`` macros and ``u8 _pad*[N];`` variable
    declarations so the search has a clean baseline. ``--fix-frame`` passes
    ``False`` to add padding on top of existing pads without disturbing them.
    """
    lines = loc.src_text.split("\n")
    # Insert pads first (they reference original line indices). Sort by source
    # position to assign A, B, C... names; insert from the back so prior
    # insertions don't shift later ones. End-of-block pads render as
    # PAD_STACK and don't consume a label letter.
    indexed = sorted(enumerate(pads), key=lambda kv: (kv[1][1], kv[0]))
    label_for: dict[int, str] = {}
    label_idx = 0
    for orig_idx, (_, pos) in indexed:
        if pos != loc.n_decls:
            label_for[orig_idx] = chr(ord("A") + label_idx)
            label_idx += 1
    for orig_idx, (size, pos) in sorted(indexed, key=lambda kv: -kv[1][1]):
        decl_line = _pad_line(loc, label_for.get(orig_idx), size, pos)
        lines.insert(_line_for_position(loc, pos), decl_line)
    # Strip the existing pad decls, if any. We do it last so the line indices
    # we computed against the original source remain valid above. Delete from
    # the back to keep earlier indices stable.
    if strip_pad_stack and loc.pad_decls:
        for pad in sorted(loc.pad_decls, key=lambda d: -d.start):
            # Translate the original pad's start index forward by the number
            # of pad inserts that happened at or before it.
            offset = sum(1 for _, (_, pos) in indexed
                         if _line_for_position(loc, pos) <= pad.start)
            del lines[pad.start + offset]
    return "\n".join(lines)


def render_reordered(
    loc: FunctionLocation,
    perm: Tuple[int, ...],
    pads: Optional[List[Tuple[int, int]]] = None,
    strip_pad_stack: bool = True,
) -> str:
    """Render with original decls re-emitted in ``perm`` order, optionally
    with new pad decls inserted at given positions.

    ``perm`` is a tuple of length ``loc.n_decls`` where ``perm[i]`` is the
    original decl index that should appear at new position ``i``. ``pads``
    is a list of ``(size, pos)`` where ``pos`` is the *new* position in
    [0..len(perm)] (0 = before all decls, len(perm) = after all decls).

    Decls that have an initializer (``Type name = expr;``) are split into a
    pure declaration (which moves with the permutation) and an assignment
    statement that's re-emitted in *original decl order*, right after the
    decl block. This keeps initializer side effects in their source order
    so reorder only changes stack-slot layout, not program semantics.

    Pad decls (``PAD_STACK(N);`` / ``u8 _pad*[N];``) are stripped by default
    so the search starts from a clean baseline; pass
    ``strip_pad_stack=False`` to keep them in place after the reordered
    decls.

    The decl region is computed as the contiguous span from the first
    decl/pad-decl line to the last. Anything inside that span that *isn't*
    a decl line (e.g. comments) is dropped — reorder mode trades comment
    locality for offset-search flexibility.
    """
    if len(perm) != loc.n_decls:
        raise ValueError(
            f"perm has length {len(perm)}, expected {loc.n_decls}"
        )
    lines = loc.src_text.split("\n")
    if not loc.decls and not loc.pad_decls:
        return loc.src_text

    span_blocks = sorted(
        loc.decls + loc.pad_decls, key=lambda d: d.start
    )
    region_start = span_blocks[0].start
    region_end = span_blocks[-1].end + 1  # exclusive

    pad_specs = sorted(
        enumerate(pads or []), key=lambda kv: (kv[1][1], kv[0])
    )
    label_for: dict[int, str] = {}
    label_idx = 0
    for orig_idx, (_, pos) in pad_specs:
        if pos != loc.n_decls:
            label_for[orig_idx] = chr(ord("A") + label_idx)
            label_idx += 1
    pads_at: dict[int, List[Tuple[int, int]]] = {}
    for orig_idx, (size, pos) in pad_specs:
        pads_at.setdefault(pos, []).append((orig_idx, size))

    new_block: List[str] = []
    for new_pos in range(len(perm) + 1):
        for orig_idx, size in pads_at.get(new_pos, []):
            new_block.append(_pad_line(loc, label_for.get(orig_idx), size, new_pos))
        if new_pos < len(perm):
            d = loc.decls[perm[new_pos]]
            decl_lines = (d.decl_lines if d.decl_lines is not None
                          else lines[d.start:d.end + 1])
            new_block.extend(decl_lines)
    # Emit init assignments after all decls, in original decl order, so
    # initializer side effects run in their source-declared sequence.
    for d in loc.decls:
        if d.assign_lines:
            new_block.extend(d.assign_lines)
    if not strip_pad_stack:
        for d in loc.pad_decls:
            new_block.extend(lines[d.start:d.end + 1])

    return "\n".join(lines[:region_start] + new_block + lines[region_end:])


# ---------------------------------------------------------------------------
# Search space
# ---------------------------------------------------------------------------


def ordered_compositions(n: int, k: int) -> Iterable[Tuple[int, ...]]:
    if k == 1:
        yield (n,)
        return
    for first in range(1, n - k + 2):
        for rest in ordered_compositions(n - first, k - 1):
            yield (first,) + rest


def candidate_placements(
    needed_bytes: int,
    n_decls: int,
    max_pads: int,
    skip: List[List[Tuple[int, int]]],
) -> Iterable[List[Tuple[int, int]]]:
    if needed_bytes <= 0 or needed_bytes % 4 != 0:
        return
    units = needed_bytes // 4
    skip_set = {tuple(sorted(s)) for s in (skip or [])}
    upper = min(units, max_pads)
    for k in range(1, upper + 1):
        for split in ordered_compositions(units, k):
            sizes = tuple(s * 4 for s in split)
            for positions in itertools.combinations(range(n_decls + 1), k):
                pads = list(zip(sizes, positions))
                key = tuple(sorted(pads))
                if key in skip_set:
                    continue
                yield pads


def score_better(a: Tuple[int, int], b: Tuple[int, int]) -> bool:
    return (abs(a[0]), a[1]) < (abs(b[0]), b[1])


def candidate_permutations(n: int) -> Iterable[Tuple[int, ...]]:
    """Yield non-identity permutations of ``range(n)``.

    For small ``n`` (<= 7), every permutation is enumerated. For larger
    ``n`` the search space (n!) gets unwieldy, so we restrict to
    "neighborhood" moves — single pair swaps and single-element shifts —
    which together cover the moves most likely to nudge an offset by a
    few bytes without massive code restructure.
    """
    identity = tuple(range(n))
    if n < 2:
        return
    if n <= 7:
        for p in itertools.permutations(range(n)):
            if p != identity:
                yield p
        return
    seen = {identity}
    # Pair swaps: O(n^2)
    for i in range(n):
        for j in range(i + 1, n):
            p = list(identity)
            p[i], p[j] = p[j], p[i]
            t = tuple(p)
            if t not in seen:
                seen.add(t)
                yield t
    # Single-element shifts: pull element i out and insert at j
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            p = list(identity)
            elem = p.pop(i)
            p.insert(j, elem)
            t = tuple(p)
            if t not in seen:
                seen.add(t)
                yield t


# ---------------------------------------------------------------------------
# Compile + score (one worker)
# ---------------------------------------------------------------------------


@dataclass
class Workspace:
    """Per-worker scratch paths. Temp .c lives next to the original so includes resolve."""

    src_path: Path
    func: str
    ref_obj: Path
    compile_sh: Path
    temp_c: Path
    temp_o: Path

    def cleanup(self) -> None:
        for p in (self.temp_c, self.temp_o):
            try:
                p.unlink()
            except FileNotFoundError:
                pass


def make_workspace(
    src_path: Path, func: str, ref_obj: Path, compile_sh: Path, worker_id: int
) -> Workspace:
    temp_c = src_path.with_name(f"{src_path.stem}.permute_W{worker_id}.c")
    temp_o = (
        Path(tempfile.gettempdir())
        / f"stack_permute_{func}_{os.getpid()}_W{worker_id}.o"
    )
    return Workspace(src_path, func, ref_obj, compile_sh, temp_c, temp_o)


def search_permutations(
    loc: FunctionLocation,
    baseline: Tuple[int, int],
    workspaces: List["Workspace"],
    deadline: float,
    pads: Optional[List[Tuple[int, int]]] = None,
) -> Tuple[Tuple[int, int], List[int], int]:
    """Try permutations of ``loc.decls`` order to fix offset mismatches.

    If ``pads`` is given, every candidate permutation is rendered with that
    pad set inserted at the corresponding new positions — used to refine
    a pad-search result whose frame fits but whose offsets disagree.

    Returns ``(best_score, best_perm, n_tested)``. ``best_perm`` is the
    identity if nothing beat ``baseline``.
    """
    n = loc.n_decls
    best_score = baseline
    best_perm: List[int] = list(range(n))
    tested = 0
    if n < 2:
        return best_score, best_perm, tested

    state_lock = threading.Lock()
    stop_event = threading.Event()

    def evaluate(perm: Tuple[int, ...],
                 ws: "Workspace") -> Tuple[Tuple[int, ...], Optional[Tuple[int, int]]]:
        if stop_event.is_set():
            return perm, None
        rendered = render_reordered(loc, perm, pads=pads)
        score = compile_and_score(ws, rendered)
        return perm, score

    print(f"phase reorder: permuting {n} decls "
          f"(timeout {int(max(0.0, deadline - time.time()))}s, "
          f"j={len(workspaces)})")
    perms_iter = candidate_permutations(n)
    with futures.ThreadPoolExecutor(max_workers=len(workspaces)) as pool:
        pending: dict = {}
        for ws in workspaces:
            try:
                p = next(perms_iter)
            except StopIteration:
                break
            pending[pool.submit(evaluate, p, ws)] = ws
        while pending and not stop_event.is_set():
            if time.time() > deadline:
                print("  timeout reached.")
                stop_event.set()
                break
            done, _ = futures.wait(
                pending,
                timeout=max(0.0, deadline - time.time()),
                return_when=futures.FIRST_COMPLETED,
            )
            if not done:
                continue
            for fut in done:
                ws = pending.pop(fut)
                perm, score = fut.result()
                tested += 1
                if score is None:
                    pass  # compile failed (e.g. perm broke an init dep)
                else:
                    with state_lock:
                        if score_better(score, best_score):
                            best_score = score
                            best_perm = list(perm)
                            print(f"  new best {score} with perm={list(perm)}")
                            if score == (0, 0):
                                stop_event.set()
                                break
                if not stop_event.is_set():
                    try:
                        next_p = next(perms_iter)
                        pending[pool.submit(evaluate, next_p, ws)] = ws
                    except StopIteration:
                        pass
        for fut in pending:
            fut.cancel()

    return best_score, best_perm, tested


def compile_and_score(ws: Workspace, src_text: str) -> Optional[Tuple[int, int]]:
    ws.temp_c.write_text(src_text)
    # `compile.sh` uses `realpath "$3"`, which fails on non-existent paths on
    # macOS; pre-create the output so it resolves.
    ws.temp_o.touch()
    proc = subprocess.run(
        ["bash", str(ws.compile_sh), str(ws.temp_c), "x", str(ws.temp_o)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or ws.temp_o.stat().st_size == 0:
        return None
    proc = subprocess.run(
        [
            "objdiff-cli", "diff",
            "-1", str(ws.ref_obj),
            "-2", str(ws.temp_o),
            ws.func,
            "-f", "stack",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    for line in reversed((proc.stdout + proc.stderr).splitlines()):
        m = SCORE_RE.match(line)
        if m:
            return int(m.group(1)), int(m.group(2))
    return None


def read_match_pct(ws: Workspace) -> Optional[float]:
    """Run objdiff in `percent` format against the workspace's current
    ``temp_o`` and return the function's match percentage (0..100). Caller
    must have already populated ``temp_o`` via ``compile_and_score``."""
    proc = subprocess.run(
        [
            "objdiff-cli", "diff",
            "--format", "percent",
            "-c", "functionRelocDiffs=data_value",
            "-1", str(ws.ref_obj),
            "-2", str(ws.temp_o),
            ws.func,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def format_pads(pads: List[Tuple[int, int]], loc: FunctionLocation) -> str:
    if not pads:
        return "(no pads inserted)"
    lines = loc.src_text.split("\n")

    def neighbor(pos: int) -> str:
        if pos == 0:
            if loc.decls:
                return "before " + lines[loc.decls[0].start].strip()
            return "at top of body"
        if pos <= loc.n_decls:
            anchor = loc.decls[pos - 1]
            return "after " + lines[anchor.end].strip()
        return "at end of decl block"

    sorted_pads = sorted(enumerate(pads), key=lambda kv: kv[1][1])
    parts: List[str] = []
    label_idx = 0
    for _, (size, pos) in sorted_pads:
        if pos == loc.n_decls:
            parts.append(f"PAD_STACK({size}); ({neighbor(pos)})")
        else:
            label = chr(ord("A") + label_idx)
            label_idx += 1
            parts.append(f"u8 _pad{label}[{size}]; ({neighbor(pos)})")
    return "\n  ".join(parts)


def format_perm(perm: List[int], loc: FunctionLocation) -> str:
    """Return a readable summary of decl ordering ``perm``.

    Renders moves relative to the identity, so ``[1, 0, 2]`` shows up as
    "swapped decl #0 and #1". Larger rearrangements just print the
    permutation tuple plus the new decl-line order.
    """
    n = loc.n_decls
    identity = list(range(n))
    if list(perm) == identity:
        return "(no reorder)"
    lines = loc.src_text.split("\n")

    def first_line(idx: int) -> str:
        return lines[loc.decls[idx].start].strip()

    moved = [(i, perm[i]) for i in range(n) if perm[i] != i]
    if len(moved) == 2:
        a, b = moved[0][0], moved[1][0]
        if perm[a] == b and perm[b] == a:
            return (f"swapped decl #{a} ({first_line(a)!r}) "
                    f"with #{b} ({first_line(b)!r})")
    parts = [f"perm={list(perm)}"]
    parts.extend(f"  [{i}] {first_line(orig_idx)}"
                 for i, orig_idx in enumerate(perm))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def cmd_fix_frame(
    args: argparse.Namespace,
    src_path: Path,
    original_text: str,
    loc: FunctionLocation,
    workspaces: List[Workspace],
) -> int:
    """Fast-path frame-size fix.

    Workflow:
      1. Score the original source.
      2. If a PAD_STACK is present, strip it and re-score. This catches
         oversized-PAD_STACK cases (the strip alone may already be the fix)
         and keeps the final result a single, clean pad declaration.
      3. If the stripped baseline still has ``frame_diff < 0``, try
         ``u8 _padA[N];`` at the top and bottom of the decl block in parallel.
      4. Among the strip and the two pad placements, pick the candidate that
         strictly beats the original. Otherwise leave the source alone.
    """

    ws = workspaces[0]

    # --- 1. Original score ---
    original_score = compile_and_score(ws, original_text)
    if original_score is None:
        print("could not compile the original source; aborting",
              file=sys.stderr)
        return 1
    # Capture match % now while ws.temp_o still holds the original's .o,
    # so the safety check at the end doesn't need to recompile.
    original_pct = read_match_pct(ws)
    print(f"original: frame_diff={original_score[0]}, "
          f"mismatches={original_score[1]}")
    if original_score == (0, 0):
        print("already a perfect r1 match; nothing to fix.")
        return 0

    # --- 2. Strip existing pad decls (if any) for a clean baseline ---
    has_pads = bool(loc.pad_decls)
    if has_pads:
        baseline_src = render_source(loc, [], strip_pad_stack=True)
        baseline_score = compile_and_score(ws, baseline_src)
        if baseline_score is None:
            print("baseline compile failed after stripping existing pads",
                  file=sys.stderr)
            return 1
        print(f"baseline (existing pads stripped): "
              f"frame_diff={baseline_score[0]}, mismatches={baseline_score[1]}")
    else:
        baseline_src = original_text
        baseline_score = original_score

    # --- 3. Build candidate set ---
    Candidate = Tuple[Tuple[int, int], List[Tuple[int, int]], str, str]
    candidates: List[Candidate] = []
    if has_pads:
        # The strip itself is a candidate — it may already be the fix.
        candidates.append((baseline_score, [], baseline_src, "strip"))

    if baseline_score[0] < 0 and baseline_score[0] % 4 == 0:
        needed = -baseline_score[0]
        plan = [
            ("top",    [(needed, 0)]),
            ("bottom", [(needed, loc.n_decls)]),
        ]
        print(f"need {needed} bytes; trying top and bottom in parallel")
        with futures.ThreadPoolExecutor(
                max_workers=min(2, len(workspaces))) as pool:
            future_to_meta = {}
            for i, (label, pads) in enumerate(plan):
                ws_use = workspaces[i % len(workspaces)]
                rendered = render_source(loc, pads, strip_pad_stack=True)
                future_to_meta[pool.submit(compile_and_score, ws_use, rendered)] = (
                    label, pads, rendered
                )
            for future in futures.as_completed(future_to_meta):
                label, pads, rendered = future_to_meta[future]
                score = future.result()
                score_str = "compile failed" if score is None else str(score)
                print(f"  {label:6s}: {score_str}")
                if score is not None:
                    candidates.append((score, pads, rendered, label))
    elif baseline_score[0] < 0:
        print(f"baseline frame deficit ({baseline_score[0]} bytes) is not a "
              f"multiple of 4; skipping pad search.")
    elif baseline_score[0] > 0 and not has_pads:
        print(
            f"frame is {baseline_score[0]} bytes too large; --fix-frame "
            f"can only add padding, not remove it. try `volatile` on a "
            f"local or reduce register pressure."
        )
        return 1

    # --- 4. Pick best candidate that strictly beats the original ---
    # Tie-break by label preference: a clean strip is best (no pad needed),
    # then `PAD_STACK(N);` at the bottom of the decl block (idiomatic), then
    # `u8 _padA[N];` at the top. The parallel top/bottom search returns
    # candidates in completion order, so without a deterministic tiebreaker
    # the winner of equal-score races is whichever finished first.
    label_rank = {"strip": 0, "bottom": 1, "top": 2}
    viable = [c for c in candidates
              if score_better(c[0], original_score)]
    if not viable:
        print(f"\nnothing beat the original {original_score}; "
              f"source unchanged")
        return 1
    best = min(viable,
               key=lambda c: (abs(c[0][0]), c[0][1], label_rank.get(c[3], 99)))

    score, pads, rendered, label = best

    # --- 5. Match-% guard: don't apply if it would decrease overall match.
    # The score is stack-only; an apparent stack improvement could in
    # principle coincide with a regression in non-stack instructions.
    pct_after: Optional[float] = None
    if compile_and_score(ws, rendered) is not None:
        pct_after = read_match_pct(ws)
    if original_pct is not None and pct_after is not None \
            and pct_after < original_pct:
        print(f"\ncandidate {label} would drop match "
              f"{original_pct:.2f}% -> {pct_after:.2f}%; source unchanged")
        return 1

    src_path.write_text(rendered)
    if pads:
        suffix = (" (replaces existing pads)"
                  if has_pads else "")
        print(f"\napplied {label}: {format_pads(pads, loc)}{suffix}")
    else:
        print(f"\napplied {label}: removed existing pads (no pad needed)")
    return 0 if score == (0, 0) else 2


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("function")
    ap.add_argument("--timeout", type=int, default=30,
                    help="search budget in seconds (default 30)")
    ap.add_argument("--max-pads", type=int, default=4,
                    help="maximum number of pad declarations (default 4)")
    ap.add_argument("-j", "--jobs", type=int, default=4,
                    help="parallel workers (default 4)")
    ap.add_argument("--src",
                    help="path to the source file (auto-detected if omitted)")
    ap.add_argument("--fix-frame", action="store_true",
                    help="fast path: skip the search and just try `u8 _padA[N]` "
                         "at the top and bottom of the decl block, applying "
                         "whichever is better (or neither if neither helps). "
                         "Preserves any existing PAD_STACK.")
    args = ap.parse_args()

    obj_path = find_unit_for_function(args.function)
    if obj_path is None:
        print(f"could not find function '{args.function}' in report.json",
              file=sys.stderr)
        return 1

    src_path = Path(args.src) if args.src else (SRC_ROOT / f"{obj_path}.c")
    ref_obj = ROOT / f"build/GALE01/obj/{obj_path}.o"
    try:
        compile_sh = find_compile_script()
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 1

    print(f"source:     {src_path.relative_to(ROOT)}")
    print(f"unit:       {obj_path}")
    print(f"compile.sh: {compile_sh.relative_to(ROOT)}")

    original_text = src_path.read_text()
    loc = locate_function(original_text, args.function)
    parts: List[str] = [
        f"{loc.n_decls} declaration{'s' if loc.n_decls != 1 else ''}"
    ]
    if loc.block_scope_decls:
        parts.append(
            f"also {loc.block_scope_decls} declaration"
            f"{'s' if loc.block_scope_decls != 1 else ''} in "
            f"{loc.block_scope_blocks} inner block"
            f"{'s' if loc.block_scope_blocks != 1 else ''}, ignored"
        )
    if loc.pad_decls:
        action = ("kept" if args.fix_frame
                  else "will be removed for the search")
        lines_str = ", ".join(str(d.start + 1) for d in loc.pad_decls)
        plural = "s" if len(loc.pad_decls) != 1 else ""
        parts.append(
            f"existing pad decl{plural} at line{plural} {lines_str} {action}"
        )
    print(f"decl block: {'; '.join(parts)}")

    # Pre-build worker scratch paths and ensure cleanup on any exit.
    workspaces = [
        make_workspace(src_path, args.function, ref_obj, compile_sh, w)
        for w in range(max(1, args.jobs))
    ]

    def cleanup_all() -> None:
        for ws in workspaces:
            ws.cleanup()

    atexit.register(cleanup_all)

    def on_signal(_signum: int, _frame) -> None:
        cleanup_all()
        print("\ninterrupted; original source untouched")
        sys.exit(130)

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    if args.fix_frame:
        return cmd_fix_frame(args, src_path, original_text, loc, workspaces)

    # If the function already has any pad decls, score the unstripped source
    # so we can decide later whether stripping is a real improvement.
    original_score: Optional[Tuple[int, int]] = None
    if loc.pad_decls:
        original_score = compile_and_score(workspaces[0], original_text)
        if original_score is None:
            print(
                "could not compile the original source; aborting before "
                "touching anything",
                file=sys.stderr,
            )
            return 1
        print(
            f"original (existing pads kept): "
            f"frame_diff={original_score[0]}, mismatches={original_score[1]}"
        )

    # Baseline (existing pads stripped if present, no new pads).
    baseline_src = render_source(loc, [])
    baseline = compile_and_score(workspaces[0], baseline_src)
    if baseline is None:
        print(
            "baseline compile failed after stripping existing pads; "
            "leaving source unchanged",
            file=sys.stderr,
        )
        return 1
    if loc.pad_decls:
        print(
            f"baseline (existing pads stripped): "
            f"frame_diff={baseline[0]}, mismatches={baseline[1]}"
        )
    else:
        print(f"baseline: frame_diff={baseline[0]}, mismatches={baseline[1]}")

    # `best_score` / `best_pads` track the best *applyable* state. If we have
    # an original_score that beats the stripped baseline, we won't apply
    # anything that's worse than it.
    best_score = baseline
    best_pads: List[Tuple[int, int]] = []

    def maybe_apply_and_exit(score: Tuple[int, int],
                             rendered: str,
                             pads: List[Tuple[int, int]],
                             tail: str) -> int:
        if original_score is not None and not score_better(score, original_score):
            print(
                f"\nbest improvement {score} is no better than the original "
                f"({original_score}); leaving existing pads in place."
            )
            return 0 if score == (0, 0) else 2
        src_path.write_text(rendered)
        print(tail)
        if loc.pad_decls:
            print("  (and removed existing pad decls)")
        return 0 if score == (0, 0) else 2

    if baseline == (0, 0):
        if loc.pad_decls:
            return maybe_apply_and_exit(
                baseline, baseline_src, [],
                "removed redundant pad decls; r1 match is already perfect",
            )
        print("already a perfect r1 match; nothing to do.")
        return 0
    if baseline[0] > 0:
        print(
            f"frame is {baseline[0]} bytes too large; padding cannot fix "
            f"that. try removing a local, marking one `volatile`, or "
            f"reducing register pressure."
        )
        return 1
    if baseline[0] == 0:
        # Padding can't fix offsets, but reordering existing decls can —
        # MWCC assigns stack slots in roughly declaration order, so swapping
        # two locals can shift everything between them. Try permutations of
        # the decl block before giving up.
        if loc.n_decls < 2:
            if loc.pad_decls:
                return maybe_apply_and_exit(
                    baseline, baseline_src, [],
                    f"stripping existing pad decls improved score to "
                    f"{baseline}; applied.",
                )
            print(
                f"frame size already correct, but offsets disagree "
                f"({baseline[1]} mismatches), and there are too few "
                f"decls to reorder. try `volatile`-ing one to spill "
                f"it to the stack."
            )
            return 1
        deadline = time.time() + args.timeout
        reorder_score, reorder_perm, reorder_tested = search_permutations(
            loc, baseline, workspaces, deadline,
        )
        print(
            f"\ntested {reorder_tested} permutation(s); "
            f"best score: {reorder_score}"
        )
        if score_better(reorder_score, baseline):
            rendered = render_reordered(loc, tuple(reorder_perm))
            return maybe_apply_and_exit(
                reorder_score, rendered, [],
                f"applied reordering: {format_perm(reorder_perm, loc)}",
            )
        # No improvement from reorder. If stripping pad_decls itself helped,
        # apply that; otherwise give up.
        if loc.pad_decls:
            return maybe_apply_and_exit(
                baseline, baseline_src, [],
                f"stripping existing pad decls improved score to "
                f"{baseline}; applied.",
            )
        print(
            "no permutation improved on the baseline. try `volatile`-ing "
            "a local to spill it to the stack, or look for an inlined "
            "helper whose locals affect the layout."
        )
        return 1
    if baseline[0] % 4 != 0:
        print(f"frame deficit ({baseline[0]} bytes) is not a multiple of 4; "
              f"aborting.")
        return 1

    needed = -baseline[0]
    print(f"need {needed} bytes of padding; using {len(workspaces)} workers")

    tested = 0
    deadline = time.time() + args.timeout
    state_lock = threading.Lock()
    stop_event = threading.Event()

    def evaluate(pads: List[Tuple[int, int]],
                 ws: Workspace) -> Tuple[List[Tuple[int, int]], Optional[Tuple[int, int]]]:
        if stop_event.is_set():
            return pads, None
        rendered = render_source(loc, pads)
        score = compile_and_score(ws, rendered)
        return pads, score

    # Phase 1: a single pad at the end of the decl block.
    end_pads = [(needed, loc.n_decls)]
    print("phase 1: single pad at end")
    pads, score = evaluate(end_pads, workspaces[0])
    tested += 1
    if score is not None and score_better(score, best_score):
        best_score = score
        best_pads = pads
        print(f"  new best {score} with {pads}")
    if score == (0, 0):
        print("phase 1 matched.")
    else:
        # Phase 2: enumerate the rest, parallelized.
        print(f"phase 2: enumerating placements (timeout {args.timeout}s, "
              f"j={len(workspaces)})")
        with futures.ThreadPoolExecutor(max_workers=len(workspaces)) as pool:
            pending: dict = {}
            ws_iter = itertools.cycle(workspaces)
            placements = candidate_placements(
                needed, loc.n_decls, args.max_pads, skip=[end_pads]
            )
            for ws in workspaces:
                try:
                    pads = next(placements)
                except StopIteration:
                    break
                fut = pool.submit(evaluate, pads, ws)
                pending[fut] = ws
            while pending and not stop_event.is_set():
                if time.time() > deadline:
                    print("timeout reached.")
                    stop_event.set()
                    break
                done, _ = futures.wait(
                    pending,
                    timeout=max(0.0, deadline - time.time()),
                    return_when=futures.FIRST_COMPLETED,
                )
                if not done:
                    continue
                for fut in done:
                    ws = pending.pop(fut)
                    pads, score = fut.result()
                    tested += 1
                    if score is None:
                        continue
                    with state_lock:
                        if score_better(score, best_score):
                            best_score = score
                            best_pads = pads
                            print(f"  new best {score} with {pads}")
                            if score == (0, 0):
                                stop_event.set()
                                break
                    # Submit next placement on this workspace.
                    if not stop_event.is_set():
                        try:
                            next_pads = next(placements)
                            pending[pool.submit(evaluate, next_pads, ws)] = ws
                        except StopIteration:
                            pass
            for fut in pending:
                fut.cancel()

    print(f"\ntested {tested} placement(s); best score: {best_score}")

    # If the pad search reached frame_diff=0 but offsets still disagree,
    # reordering existing decls (with the chosen pads still in place) may
    # close the gap. Skip when n_decls < 2 (nothing to permute) or when
    # we already hit a perfect match.
    best_perm: List[int] = list(range(loc.n_decls))
    if (best_score[0] == 0 and best_score[1] > 0
            and loc.n_decls >= 2 and time.time() < deadline):
        reorder_score, reorder_perm, reorder_tested = search_permutations(
            loc, best_score, workspaces, deadline, pads=best_pads,
        )
        print(f"\ntested {reorder_tested} permutation(s); "
              f"best score: {reorder_score}")
        if score_better(reorder_score, best_score):
            best_score = reorder_score
            best_perm = reorder_perm

    if best_perm != list(range(loc.n_decls)):
        final_text = render_reordered(
            loc, tuple(best_perm), pads=best_pads,
        )
        tail = (f"applied to source:\n  {format_perm(best_perm, loc)}")
        if best_pads:
            tail += f"\n  with pads: {format_pads(best_pads, loc)}"
    else:
        final_text = render_source(loc, best_pads)
        tail = f"applied to source:\n  {format_pads(best_pads, loc)}"
    return maybe_apply_and_exit(best_score, final_text, best_pads, tail)


if __name__ == "__main__":
    sys.exit(main())
