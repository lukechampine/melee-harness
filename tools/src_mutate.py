#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["tree-sitter", "tree-sitter-c"]
# ///
"""
Source-level mutation engine for the melee permuter (permute.py).

Unlike decomp-permuter, this mutates the **real** source text. It parses the
actual .c with tree-sitter-c (robust to melee's macros: unknown macro calls
parse as ordinary call/identifier nodes) and applies behaviour-preserving
edits as *byte-span splices* into the original bytes. Everything the mutation
does not touch stays byte-identical -- macros, comments, indentation -- so a
winning permutation is a real diff that applies straight to src/.../*.c.

Each `step()` re-parses the current text, picks one weighted pass, and returns
the mutated bytes (or None if nothing applied). Re-parsing every step (~1ms) is
negligible next to the mwcc compile, and it frees us from tracking byte offsets
across stacked mutations.

Standalone (debugging):
    uv run tools/src_mutate.py <file.c> <fn> [--pass NAME] [--seed N] [-n K]
prints a unified diff of the resulting mutation(s).
"""

from __future__ import annotations

import argparse
import difflib
import random
import sys
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

import tree_sitter_c
from tree_sitter import Language, Node, Parser, Tree

_C = Language(tree_sitter_c.language())


def _new_parser() -> Parser:
    try:
        return Parser(_C)
    except TypeError:  # older tree-sitter API
        p = Parser()
        p.language = _C
        return p


_PARSER = _new_parser()


def parse(src: bytes) -> Tree:
    return _PARSER.parse(src)


# (start, end, replacement_bytes); start == end means an insertion.
Edit = Tuple[int, int, bytes]

STMT_TYPES = {
    "expression_statement",
    "if_statement",
    "for_statement",
    "while_statement",
    "do_statement",
    "switch_statement",
    "return_statement",
    "compound_statement",
    "break_statement",
    "continue_statement",
    "labeled_statement",
    "goto_statement",
}

COMM_OPS = {b"+", b"*", b"&", b"|", b"^", b"==", b"!="}
REL_FLIP = {b"<": b">", b">": b"<", b"<=": b">=", b">=": b"<="}
COMPARE_OPS = COMM_OPS | set(REL_FLIP) | {b"&&", b"||"}
AUG_OPS = {b"+", b"-", b"*", b"/", b"%", b"&", b"|", b"^", b"<<", b">>"}


# --------------------------------------------------------------------------
# node helpers
# --------------------------------------------------------------------------
def field(node: Node, name: str) -> Optional[Node]:
    return node.child_by_field_name(name)


def only_named(node: Node) -> Optional[Node]:
    return node.named_children[0] if node.named_children else None


def iter_subtree(node: Node):
    """Yield node and all named descendants."""
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(n.named_children)


def fn_name_of(node: Node) -> Optional[str]:
    decl = field(node, "declarator")
    while decl is not None:
        if decl.type == "identifier":
            return decl.text.decode()
        decl = field(decl, "declarator")
    return None


def function_declarator_of(node: Node) -> Optional[Node]:
    decl = field(node, "declarator")
    while decl is not None and decl.type != "function_declarator":
        decl = field(decl, "declarator")
    return decl


def find_function(root: Node, name: str) -> Optional[Node]:
    for n in iter_subtree(root):
        if n.type == "function_definition" and fn_name_of(n) == name:
            return n
    return None


def prefix_split(src: bytes) -> int:
    """Byte offset where the leading preprocessor/comment block ends (i.e. the
    start of the first real C declaration/definition). The permuter precompiles
    src[:split] (pure #include/#define/comment -- nothing that emits code or
    data) into a PCH and only recompiles src[split:] per candidate. Returns
    len(src) if the file is all preprocessor, or 0 if it opens with code."""
    tree = parse(src)
    for ch in tree.root_node.named_children:
        if ch.type.startswith("preproc_") or ch.type == "comment":
            continue
        return ch.start_byte
    return len(src)


def body_of(fn: Node) -> Optional[Node]:
    return field(fn, "body")


def declarations(body: Node) -> List[Node]:
    return [c for c in body.named_children if c.type == "declaration"]


def statements(body: Node) -> List[Node]:
    return [c for c in body.named_children if c.type in STMT_TYPES]


def swap_spans(a: Node, b: Node) -> List[Edit]:
    """Swap the text of two disjoint nodes (indentation preserved, since each
    node's span excludes the surrounding whitespace)."""
    return [(a.start_byte, a.end_byte, b.text), (b.start_byte, b.end_byte, a.text)]


def apply_edits(src: bytes, edits: List[Edit]) -> bytes:
    out = bytearray()
    pos = 0
    for s, e, rep in sorted(edits, key=lambda x: x[0]):
        if s < pos:
            raise ValueError("overlapping edits")
        out += src[pos:s]
        out += rep
        pos = e
    out += src[pos:]
    return bytes(out)


# --------------------------------------------------------------------------
# mutation passes:  Ctx -> Optional[List[Edit]]
# --------------------------------------------------------------------------
@dataclass
class Ctx:
    src: bytes
    root: Node
    fn: Node
    rng: random.Random


def _pick_pair(rng: random.Random, n: int, adjacent_prob: float) -> Tuple[int, int]:
    i = rng.randrange(n)
    if n > 2 and rng.random() >= adjacent_prob:
        j = rng.randrange(n)
        while j == i:
            j = rng.randrange(n)
    else:
        j = i + 1 if i + 1 < n else i - 1
    return i, j


def p_reorder_decls(ctx: Ctx) -> Optional[List[Edit]]:
    decls = declarations(body_of(ctx.fn))
    if len(decls) < 2:
        return None
    i, j = _pick_pair(ctx.rng, len(decls), adjacent_prob=0.4)
    if i == j:
        return None
    return swap_spans(decls[i], decls[j])


def p_reorder_stmts(ctx: Ctx) -> Optional[List[Edit]]:
    stmts = statements(body_of(ctx.fn))
    if len(stmts) < 2:
        return None
    i, j = _pick_pair(ctx.rng, len(stmts), adjacent_prob=0.85)
    if i == j:
        return None
    return swap_spans(stmts[i], stmts[j])


def p_commutative(ctx: Ctx) -> Optional[List[Edit]]:
    cands = []
    for n in iter_subtree(ctx.fn):
        if n.type != "binary_expression":
            continue
        op = field(n, "operator")
        if op is not None and (op.text in COMM_OPS or op.text in REL_FLIP):
            cands.append((n, op))
    if not cands:
        return None
    n, op = ctx.rng.choice(cands)
    l, r = field(n, "left"), field(n, "right")
    if l is None or r is None:
        return None
    edits = [(l.start_byte, l.end_byte, r.text), (r.start_byte, r.end_byte, l.text)]
    if op.text in REL_FLIP:
        edits.append((op.start_byte, op.end_byte, REL_FLIP[op.text]))
    return edits


def p_add_sub(ctx: Ctx) -> Optional[List[Edit]]:
    cands = []
    for n in iter_subtree(ctx.fn):
        if n.type != "binary_expression":
            continue
        op = field(n, "operator")
        if op is not None and op.text == b"-":
            cands.append((n, op))
    if not cands:
        return None
    n, op = ctx.rng.choice(cands)
    r = field(n, "right")
    if r is None:
        return None
    # a - b  ->  a + -(b)
    return [(op.start_byte, op.end_byte, b"+"), (r.start_byte, r.end_byte, b"-(" + r.text + b")")]


def p_compound_assignment(ctx: Ctx) -> Optional[List[Edit]]:
    contract: List[Tuple] = []
    expand: List[Tuple] = []
    aug_eq = {o + b"=" for o in AUG_OPS}
    for n in iter_subtree(ctx.fn):
        if n.type != "assignment_expression":
            continue
        op, l, r = field(n, "operator"), field(n, "left"), field(n, "right")
        if op is None or l is None or r is None:
            continue
        if op.text == b"=" and r.type == "binary_expression":
            rop, rl, rr = field(r, "operator"), field(r, "left"), field(r, "right")
            if (
                rop is not None
                and rl is not None
                and rr is not None
                and rop.text in AUG_OPS
                and rl.text == l.text
            ):
                contract.append((n, l, rop, rr))
        elif op.text in aug_eq:
            expand.append((n, l, op, r))
    choices = [("c", x) for x in contract] + [("e", x) for x in expand]
    if not choices:
        return None
    kind, item = ctx.rng.choice(choices)
    if kind == "c":
        n, l, rop, rr = item
        return [(n.start_byte, n.end_byte, l.text + b" " + rop.text + b"= " + rr.text)]
    n, l, op, r = item
    base = op.text[:-1]  # strip '='
    return [(n.start_byte, n.end_byte, l.text + b" = " + l.text + b" " + base + b" " + r.text)]


def p_struct_ref(ctx: Ctx) -> Optional[List[Edit]]:
    fwd: List[Tuple] = []
    rev: List[Tuple] = []
    for n in iter_subtree(ctx.fn):
        if n.type != "field_expression":
            continue
        op, arg, fld = field(n, "operator"), field(n, "argument"), field(n, "field")
        if op is None or arg is None or fld is None:
            continue
        if op.text == b"->":
            fwd.append((n, arg, fld))
        elif op.text == b"." and arg.type == "parenthesized_expression":
            inner = only_named(arg)
            if inner is not None and inner.type == "pointer_expression":
                ptr = field(inner, "argument") or only_named(inner)
                if ptr is not None:
                    rev.append((n, ptr, fld))
    choices = [("f", x) for x in fwd] + [("r", x) for x in rev]
    if not choices:
        return None
    kind, (n, a, fld) = ctx.rng.choice(choices)
    if kind == "f":
        return [(n.start_byte, n.end_byte, b"(*(" + a.text + b"))." + fld.text)]
    return [(n.start_byte, n.end_byte, a.text + b"->" + fld.text)]


def p_condition(ctx: Ctx) -> Optional[List[Edit]]:
    wrap: List[Node] = []
    unwrap: List[Tuple[Node, Node]] = []
    for n in iter_subtree(ctx.fn):
        if n.type not in ("if_statement", "while_statement", "do_statement"):
            continue
        cond = field(n, "condition")
        if cond is None or cond.type != "parenthesized_expression":
            continue
        inner = only_named(cond)
        if inner is None:
            continue
        if inner.type == "binary_expression":
            op = field(inner, "operator")
            if op is not None and op.text == b"!=":
                r = field(inner, "right")
                if r is not None and r.text == b"0":
                    left = field(inner, "left")
                    if left is not None:
                        unwrap.append((inner, left))
                    continue
            if op is not None and op.text in COMPARE_OPS:
                continue  # already a comparison; don't add noise
        wrap.append(inner)
    choices = [("w", x) for x in wrap] + [("u", x) for x in unwrap]
    if not choices:
        return None
    kind, item = ctx.rng.choice(choices)
    if kind == "w":
        inner = item
        return [(inner.start_byte, inner.end_byte, inner.text + b" != 0")]
    inner, left = item
    return [(inner.start_byte, inner.end_byte, left.text)]


def p_remove_cast(ctx: Ctx) -> Optional[List[Edit]]:
    casts = [n for n in iter_subtree(ctx.fn) if n.type == "cast_expression"]
    if not casts:
        return None
    n = ctx.rng.choice(casts)
    v = field(n, "value")
    if v is None:
        return None
    return [(n.start_byte, n.end_byte, v.text)]


def p_pad_var_decl(ctx: Ctx) -> Optional[List[Edit]]:
    decls = declarations(body_of(ctx.fn))
    if not decls:
        return None
    typ = field(ctx.rng.choice(decls), "type")
    if typ is None:
        return None
    name = f"_perm_pad{ctx.rng.randrange(1_000_000)}".encode()
    anchor = ctx.rng.choice(decls)
    line_start = ctx.src.rfind(b"\n", 0, anchor.start_byte) + 1
    indent = ctx.src[line_start:anchor.start_byte]
    pad = typ.text + b" " + name + b";\n" + indent
    return [(anchor.start_byte, anchor.start_byte, pad)]


def p_reorder_params(ctx: Ctx) -> Optional[List[Edit]]:
    fdecl = function_declarator_of(ctx.fn)
    if fdecl is None:
        return None
    # Only safe for static functions: a non-static function's prototype lives
    # in a header we can't see/edit, so reordering its params here would
    # mismatch that prototype (a "redeclared" error). The functions worth
    # reordering (the static inline helpers) are static anyway.
    if b"static" not in ctx.src[ctx.fn.start_byte:fdecl.start_byte]:
        return None
    plist = field(fdecl, "parameters")
    if plist is None:
        return None
    params = [c for c in plist.named_children if c.type == "parameter_declaration"]
    if len(params) < 2:
        return None
    name = fn_name_of(ctx.fn)
    i, j = _pick_pair(ctx.rng, len(params), adjacent_prob=0.5)
    if i == j:
        return None

    edits: List[Edit] = []
    # Every declarator (definition + any prototypes) for this function must
    # have the same arity, or we bail rather than emit an inconsistent TU.
    for fd in iter_subtree(ctx.root):
        if fd.type != "function_declarator":
            continue
        nm = field(fd, "declarator")
        if nm is None or nm.type != "identifier" or nm.text.decode() != name:
            continue
        pl = field(fd, "parameters")
        ps = [c for c in pl.named_children if c.type == "parameter_declaration"] if pl else []
        if len(ps) != len(params):
            return None
        edits += swap_spans(ps[i], ps[j])

    # Every call site must take exactly that many args, or we bail.
    for n in iter_subtree(ctx.root):
        if n.type != "call_expression":
            continue
        f = field(n, "function")
        if f is None or f.type != "identifier" or f.text.decode() != name:
            continue
        al = field(n, "arguments")
        args = [c for c in al.named_children] if al is not None else []
        if len(args) != len(params):
            return None
        edits += swap_spans(args[i], args[j])
    return edits


PASSES: List[Tuple[str, Callable[[Ctx], Optional[List[Edit]]], float]] = [
    ("reorder_decls", p_reorder_decls, 10.0),
    ("reorder_stmts", p_reorder_stmts, 10.0),
    ("reorder_params", p_reorder_params, 6.0),
    ("commutative", p_commutative, 5.0),
    ("add_sub", p_add_sub, 5.0),
    ("struct_ref", p_struct_ref, 5.0),
    ("compound_assignment", p_compound_assignment, 4.0),
    ("condition", p_condition, 4.0),
    ("remove_cast", p_remove_cast, 3.0),
    ("pad_var_decl", p_pad_var_decl, 2.0),
]


class Mutator:
    """Applies one weighted random pass to a named function per step()."""

    def __init__(
        self,
        fn_name: str,
        weights: Optional[dict] = None,
        passes=PASSES,
    ) -> None:
        self.fn_name = fn_name
        self.passes = [
            (n, f, (weights or {}).get(n, w)) for n, f, w in passes
        ]

    def step(self, src: bytes, rng: random.Random) -> Optional[bytes]:
        tree = parse(src)
        fn = find_function(tree.root_node, self.fn_name)
        if fn is None:
            return None
        pool = [(n, f, w) for n, f, w in self.passes if w > 0]
        while pool:
            total = sum(w for _, _, w in pool)
            r = rng.uniform(0, total)
            acc = 0.0
            idx = 0
            for k, (_n, _f, w) in enumerate(pool):
                acc += w
                if r <= acc:
                    idx = k
                    break
            _name, func, _w = pool.pop(idx)
            try:
                edits = func(Ctx(src, tree.root_node, fn, rng))
            except Exception:
                edits = None
            if not edits:
                continue
            try:
                new = apply_edits(src, edits)
            except ValueError:
                continue
            if new != src:
                return new
        return None

    def step_named(self, src: bytes, name: str, rng: random.Random) -> Optional[bytes]:
        """Run exactly one pass by name (debugging)."""
        tree = parse(src)
        fn = find_function(tree.root_node, self.fn_name)
        if fn is None:
            return None
        for n, f, _w in self.passes:
            if n == name:
                edits = f(Ctx(src, tree.root_node, fn, rng))
                if not edits:
                    return None
                return apply_edits(src, edits)
        raise SystemExit(f"unknown pass: {name}")


def _main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file")
    ap.add_argument("fn")
    ap.add_argument("--pass", dest="pass_name", default=None)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("-n", type=int, default=1, help="number of stacked mutations")
    args = ap.parse_args()

    src = open(args.file, "rb").read()
    rng = random.Random(args.seed)
    mut = Mutator(args.fn)
    cur = src
    for _ in range(args.n):
        if args.pass_name:
            new = mut.step_named(cur, args.pass_name, rng)
        else:
            new = mut.step(cur, rng)
        if new is None:
            print("(no mutation applied)", file=sys.stderr)
            break
        cur = new

    diff = difflib.unified_diff(
        src.decode(errors="replace").splitlines(keepends=True),
        cur.decode(errors="replace").splitlines(keepends=True),
        fromfile=args.file,
        tofile=args.file + " (mutated)",
    )
    sys.stdout.write("".join(diff))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
