#!/usr/bin/env python3
"""Flag functions with multiple Item* / Fighter* variables, scoped to the
function the agent just edited.

Two `Item*` (or `Fighter*`) names in a single function — counting parameters
and locals — almost always means a helper got inlined into another function
and should be split back out into its own function (or replaced with a call
to an existing one).

Designed to run as a Claude Code PostToolUse hook on the Edit tool: reads
the tool event JSON from stdin, locates the edit position by finding
`new_string` in the post-edit file, walks the brace structure to find the
enclosing function body, and checks only that function. Pre-existing
violations in other functions are ignored.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# A `Type *name` declaration. Casts like `(Item*) foo` never match because
# the `*` is followed by `)`, not by an identifier.
DECL_RE = re.compile(r"\b(Item|Fighter)\b\s*\*+\s*([A-Za-z_]\w*)")
NAME_RE = re.compile(r"([A-Za-z_]\w*)\s*\(")

C_SUFFIXES = {".c"}


def strip_comments_and_strings(src: str) -> str:
    """Length-preserving stripper: replaces non-newline chars inside comments
    and string/char literals with spaces. Brace and identifier positions in
    the cleaned text match the raw text byte-for-byte.
    """
    out = list(src)
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            j = src.find("\n", i)
            if j < 0:
                j = n
            for k in range(i, j):
                out[k] = " "
            i = j
        elif c == "/" and nxt == "*":
            end = src.find("*/", i + 2)
            j = n if end < 0 else end + 2
            for k in range(i, j):
                if src[k] != "\n":
                    out[k] = " "
            i = j
        elif c == '"' or c == "'":
            quote = c
            i += 1
            while i < n and src[i] != quote:
                if src[i] == "\\" and i + 1 < n:
                    if src[i] != "\n":
                        out[i] = " "
                    if src[i + 1] != "\n":
                        out[i + 1] = " "
                    i += 2
                else:
                    if src[i] != "\n":
                        out[i] = " "
                    i += 1
            if i < n:
                i += 1
        else:
            i += 1
    return "".join(out)


def find_enclosing_function(clean: str, pos: int) -> tuple[int, int, int, str] | None:
    """Locate the top-level function body containing offset `pos`.

    Returns (sig_start, body_start, body_end, name), or None if `pos` is
    outside any function (e.g. between top-level declarations).
    """
    n = len(clean)
    if n == 0 or pos < 0:
        return None
    # Walk backward from pos, peeling off matched `}` ... `{` pairs. The first
    # unmatched `{` is the brace immediately enclosing pos. Then count how
    # many further unmatched `{`s lie on the way to the start of the file —
    # the *outermost* unmatched `{` is the function's body opener.
    depth = 0
    open_braces: list[int] = []  # indices of unmatched `{`s, deepest first
    for i in range(min(pos, n - 1), -1, -1):
        c = clean[i]
        if c == "}":
            depth += 1
        elif c == "{":
            if depth == 0:
                open_braces.append(i)
            else:
                depth -= 1
    if not open_braces:
        return None
    body_start = open_braces[-1]  # outermost = function body

    # Forward-scan from body_start to find matching `}`.
    depth = 0
    body_end = -1
    for i in range(body_start, n):
        c = clean[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                body_end = i
                break
    if body_end < 0:
        return None
    if not (body_start <= pos <= body_end):
        return None  # pos was somehow outside (e.g. between funcs)

    # Walk back from body_start to find the start of the signature: the most
    # recent `;` or `}` at file scope, or the start of the file.
    sig_start = 0
    for i in range(body_start - 1, -1, -1):
        c = clean[i]
        if c == ";" or c == "}":
            sig_start = i + 1
            break
    sig = clean[sig_start:body_start]
    m = NAME_RE.search(sig)
    if m is None:
        return None  # struct/union body or other non-function block
    name = sig[m.start(1):m.end(1)]
    return (sig_start, body_start, body_end, name)


def find_all_occurrences(text: str, needle: str) -> list[int]:
    out: list[int] = []
    start = 0
    while True:
        i = text.find(needle, start)
        if i < 0:
            return out
        out.append(i)
        start = i + max(1, len(needle))


def check_function(clean: str, sig_start: int, body_end: int) -> list[tuple[str, str]]:
    region = clean[sig_start:body_end + 1]
    seen: dict[str, list[tuple[str, str]]] = {"Item": [], "Fighter": []}
    for m in DECL_RE.finditer(region):
        t, var = m.group(1), m.group(2)
        if all(v != var for _, v in seen[t]):
            seen[t].append((t, var))
    return [d for ds in seen.values() for d in ds if len(seen[d[0]]) > 1]


def main() -> int:
    if sys.stdin.isatty():
        return 0
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if event.get("tool_name") != "Edit":
        return 0
    ti = event.get("tool_input") or {}
    file_path = ti.get("file_path") or ""
    new_string = ti.get("new_string") or ""
    if not file_path or not new_string:
        return 0
    p = Path(file_path)
    if p.suffix not in C_SUFFIXES:
        return 0

    # Cheap delta gate: only run the check if the edit *net-added* at least
    # one Item*/Fighter* declaration. Avoids re-flagging pre-existing
    # violations on every unrelated edit. May miss the rare case where a
    # rename swaps types (e.g. `int foo` → `Item* foo`) without changing
    # the count, but the agent can override on a case-by-case basis.
    old_string = ti.get("old_string") or ""
    old_count = sum(1 for _ in DECL_RE.finditer(strip_comments_and_strings(old_string)))
    new_count = sum(1 for _ in DECL_RE.finditer(strip_comments_and_strings(new_string)))
    if new_count - old_count <= 0:
        return 0

    try:
        text = p.read_text(errors="replace")
    except OSError:
        return 0
    clean = strip_comments_and_strings(text)

    positions = find_all_occurrences(text, new_string)
    if not positions:
        return 0
    if not ti.get("replace_all"):
        positions = positions[:1]

    seen_fns: set[int] = set()  # body_start of fns we've already checked
    findings: list[tuple[int, str, list[tuple[str, str]]]] = []
    for pos in positions:
        loc = find_enclosing_function(clean, pos)
        if loc is None:
            continue
        sig_start, body_start, body_end, name = loc
        if body_start in seen_fns:
            continue
        seen_fns.add(body_start)
        decls = check_function(clean, sig_start, body_end)
        if decls:
            body_lineno = clean.count("\n", 0, body_start) + 1
            findings.append((body_lineno, name, decls))

    if not findings:
        return 0
    for lineno, name, decls in findings:
        joined = ", ".join(f"{t}* {v}" for t, v in decls)
        print(f"{p}:{lineno}: {name}: multiple {joined}", file=sys.stderr)
    print(
        "\nMultiple Item*/Fighter* in one function usually means an inlined "
        "helper. Extract a new function (or call an existing one).",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
