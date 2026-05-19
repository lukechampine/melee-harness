#!/usr/bin/env python3
"""Flag type-erasing casts and m2c residue introduced by an Edit.

Catches casts through `void*`, `u8*`, or `char*` and uses of `M2C_FIELD`.
These all indicate the involved types haven't been nailed down yet and
should be strengthened (richer struct, union, or proper type).

Designed to run as a Claude Code PostToolUse hook on the Edit tool: reads
the tool event JSON from stdin and inspects the `new_string` payload, so
it only flags what the current edit added — pre-existing violations
elsewhere in the file are ignored.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# Match `(void *)`, `(void**)`, `(u8 *)`, `(char *)`, etc. — a type cast
# where the inner type is `void`, `u8`, or `char` followed only by `*`s.
# Casts like `(void) expr` (no `*`) and decls like `void* foo` (no
# enclosing parens) don't match.
CAST_RE = re.compile(r"\(\s*(?:void|u8|char)\s*\*+\s*\)")
M2C_RE = re.compile(r"\bM2C_FIELD\s*\(")

C_SUFFIXES = {".c", ".h"}


def strip_comments_and_strings(src: str) -> str:
    """Replace contents of strings and comments with spaces (preserving newlines).

    Line/column positions are preserved so caller can map matches back to the
    original line numbers.
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


def scan(text: str) -> list[tuple[int, str, str]]:
    """Return [(lineno_in_text, kind, snippet), ...] for violations in `text`."""
    findings: list[tuple[int, str, str]] = []
    clean = strip_comments_and_strings(text)
    for lineno, line in enumerate(clean.splitlines(), 1):
        for m in CAST_RE.finditer(line):
            findings.append((lineno, "type-erasing cast", m.group(0)))
        for m in M2C_RE.finditer(line):
            findings.append((lineno, "M2C_FIELD", "M2C_FIELD(...)"))
    return findings


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

    findings = scan(new_string)
    if not findings:
        return 0

    # Translate new_string-relative line numbers into file line numbers.
    try:
        text = p.read_text(errors="replace")
    except OSError:
        text = ""
    pos = text.find(new_string)
    base_line = text.count("\n", 0, pos) if pos >= 0 else 0

    for rel_lineno, kind, snippet in findings:
        print(f"{p}:{base_line + rel_lineno}: {kind}: {snippet}", file=sys.stderr)
    print(
        "\nThese casts/macros erase type information. Strengthen the involved "
        "types (richer struct field, union, or correct pointer type) instead.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
