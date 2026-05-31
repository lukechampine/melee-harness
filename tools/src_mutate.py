#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["tree-sitter", "tree-sitter-c", "libclang"]
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
from pathlib import Path
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
    types: Optional[dict] = None   # {(start,end): clang type spelling}, base only


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


TEMP_EXPR_TYPES = {
    "binary_expression", "call_expression", "field_expression",
    "cast_expression", "subscript_expression", "unary_expression",
    "pointer_expression",
}


def _usable_temp_type(typ: str) -> bool:
    # Skip types we can't write as `T name` cleanly: void, functions / function
    # pointers, arrays, anonymous/unnamed aggregates.
    if not typ or typ == "void":
        return False
    return not any(c in typ for c in "([{")


def _enclosing_temp_stmt(n: Node):
    """Nearest statement ancestor + how to host the temp there:
      "wrap" -- expression_statement / return_statement: wrap in a block
                `{ T tmp = expr; <stmt> }` (scope-safe, C89-legal).
      "decl" -- single-declarator declaration: insert a sibling temp
                declaration just before it (stays in the C89 decl zone, no
                scope leak). Multi-declarator is skipped (the init could
                reference an earlier declarator in the same statement).
    Returns (stmt, mode) or (None, None). if/loop conditions are left for later.
    """
    p = n.parent
    while p is not None:
        if p.type == "declaration":
            # only a block-level (not for-init), single-declarator declaration:
            # insert a sibling temp decl before it, in the C89 decl zone.
            if p.parent is not None and p.parent.type == "compound_statement":
                inits = [c for c in p.named_children if c.type == "init_declarator"]
                if len(inits) == 1:
                    return p, "decl"
            return None, None
        if p.type in STMT_TYPES:
            # if/switch conditions are evaluated once, so hoisting a subexpression
            # before them is behaviour-preserving. while/for/do conditions
            # re-evaluate each iteration, so they're left out.
            if p.type in ("expression_statement", "return_statement",
                          "if_statement", "switch_statement"):
                return p, "wrap"
            return None, None
        p = p.parent
    return None, None


def _extract_unsafe(n: Node, stmt: Node) -> bool:
    """True if hoisting `n`'s evaluation to the top of the statement could change
    behaviour: it's an lvalue, a call's callee, or evaluated conditionally
    (short-circuit / ternary / sizeof)."""
    def _same(a: Optional[Node]) -> bool:  # tree-sitter returns fresh wrappers
        return a is not None and a.start_byte == n.start_byte and a.end_byte == n.end_byte
    par = n.parent
    if par is not None:
        if par.type == "assignment_expression" and _same(field(par, "left")):
            return True
        if par.type == "call_expression" and _same(field(par, "function")):
            return True
    def _is(a: Optional[Node]) -> bool:
        return a is not None and a.start_byte == p.start_byte and a.end_byte == p.end_byte
    p = n
    while p is not None and not _is(stmt):
        anc = p.parent
        if anc is None:
            break
        if anc.type == "sizeof_expression":
            return True
        if anc.type == "conditional_expression":
            # the controlling condition is always evaluated; the branches aren't
            if not _is(field(anc, "condition")):
                return True
        elif anc.type == "binary_expression":
            op = field(anc, "operator")
            if op is not None and op.text in (b"&&", b"||") and _is(field(anc, "right")):
                return True   # short-circuited right operand
        p = anc
    return False


def p_temp_for_expr(ctx: Ctx) -> Optional[List[Edit]]:
    """Extract a value subexpression into a temporary -- the workhorse for
    shifting register allocation. Needs the type (from the clang oracle), so it
    only fires on base-source steps. Emits, in place of the statement:
        { T tmp = <expr>; <statement with expr replaced by tmp> }
    """
    if ctx.types is None:
        return None
    cands = []
    for n in iter_subtree(ctx.fn):
        if n.type not in TEMP_EXPR_TYPES:
            continue
        typ = ctx.types.get((n.start_byte, n.end_byte))
        if typ is None or not _usable_temp_type(typ):
            continue
        stmt, mode = _enclosing_temp_stmt(n)
        if stmt is None or _extract_unsafe(n, stmt):
            continue
        # Skip the whole (discarded) expression of an expression_statement: it's
        # pointless to temp, and is often a statement-macro (e.g. PAD_STACK) that
        # clang gives a type to but isn't a real assignable rvalue.
        if mode == "wrap" and stmt.type == "expression_statement":
            inner = stmt.named_children[0] if stmt.named_children else None
            if inner is not None and (inner.start_byte, inner.end_byte) == (n.start_byte, n.end_byte):
                continue
        cands.append((n, typ, stmt, mode))
    if not cands:
        return None
    n, typ, stmt, mode = ctx.rng.choice(cands)
    name = b"tmp_p%d" % n.start_byte          # deterministic -> dedups cleanly
    decl = typ.encode() + b" " + name + b" = " + n.text + b";"
    indent = ctx.src[ctx.src.rfind(b"\n", 0, stmt.start_byte) + 1:stmt.start_byte]

    if mode == "decl":
        # insert a sibling declaration before stmt; point the init at the temp
        return [
            (stmt.start_byte, stmt.start_byte, decl + b"\n" + indent),
            (n.start_byte, n.end_byte, name),
        ]

    # "wrap": replace the statement with a block holding the temp
    s0, s1 = stmt.start_byte, stmt.end_byte
    ra, rb = n.start_byte - s0, n.end_byte - s0
    stmt_text = ctx.src[s0:s1]
    new_stmt = stmt_text[:ra] + name + stmt_text[rb:]
    block = (b"{\n" + indent + b"    " + decl + b"\n"
             + indent + b"    " + new_stmt + b"\n" + indent + b"}")
    return [(s0, s1, block)]


def _declared_name(node: Node) -> Optional[str]:
    """Identifier a declarator/parameter introduces (descends pointer/array/etc.)."""
    d = field(node, "declarator") or node
    while d is not None:
        if d.type == "identifier":
            return d.text.decode()
        d = field(d, "declarator")
    return None


def _local_names(fn: Node) -> set:
    """Names declared in `fn`: its parameters and its body's local declarations.
    An identifier referencing one of these is a free variable that must become a
    helper parameter; anything else (global, function, enum, macro) the helper
    can reference directly."""
    names: set = set()
    fdecl = function_declarator_of(fn)
    if fdecl is not None:
        pl = field(fdecl, "parameters")
        if pl is not None:
            for p in pl.named_children:
                if p.type == "parameter_declaration":
                    nm = _declared_name(p)
                    if nm:
                        names.add(nm)
    for n in iter_subtree(fn):
        if n.type != "declaration":
            continue
        typ = field(n, "type")
        for c in n.named_children:
            if typ is not None and (c.start_byte, c.end_byte) == (typ.start_byte, typ.end_byte):
                continue
            nm = _declared_name(c)
            if nm:
                names.add(nm)
    return names


def _expr_base_id(e: Optional[Node]) -> Optional[Node]:
    """Leftmost identifier an lvalue is rooted at (through ->/.//[]/(*)/casts)."""
    cur = e
    while cur is not None:
        t = cur.type
        if t == "identifier":
            return cur
        if t == "field_expression":
            cur = field(cur, "argument")
        elif t == "subscript_expression":
            cur = field(cur, "argument") or only_named(cur)
        elif t in ("parenthesized_expression", "pointer_expression"):
            cur = field(cur, "argument") or only_named(cur)
        elif t == "cast_expression":
            cur = field(cur, "value")
        else:
            return None
    return None


def _inline_pos_ok(n: Node) -> bool:
    """Can `n` be replaced in place by a call rvalue? Not if it's an lvalue being
    assigned, addressed, incremented, or used as a call's callee."""
    par = n.parent
    if par is None:
        return True
    if par.type == "expression_statement":
        return False   # discarded value -- pointless, and catches statement-macros
    if par.type == "assignment_expression":
        l = field(par, "left")
        if l is not None and (l.start_byte, l.end_byte) == (n.start_byte, n.end_byte):
            return False
    if par.type == "update_expression":
        return False
    if par.type == "pointer_expression":
        op = field(par, "operator")
        if op is not None and op.text == b"&":
            return False
    if par.type == "call_expression":
        f = field(par, "function")
        if f is not None and (f.start_byte, f.end_byte) == (n.start_byte, n.end_byte):
            return False
    return True


def _inline_byval_unsafe(n: Node, ftypes: dict) -> bool:
    """Free vars are passed by value, so writing to / taking the address of a
    *non-pointer* free var inside the expr would be lost. (Writes/addresses
    through a pointer free var are fine -- same pointee.)"""
    def base_nonptr_free(lv: Optional[Node]) -> bool:
        b = _expr_base_id(lv)
        if b is None:
            return False
        nm = b.text.decode()
        return nm in ftypes and not ftypes[nm].rstrip().endswith("*")

    for m in iter_subtree(n):
        if m.type == "assignment_expression":
            if base_nonptr_free(field(m, "left")):
                return True
        elif m.type == "update_expression":
            if base_nonptr_free(field(m, "argument") or only_named(m)):
                return True
        elif m.type == "pointer_expression":
            op = field(m, "operator")
            if op is not None and op.text == b"&" and base_nonptr_free(
                    field(m, "argument") or only_named(m)):
                return True
    return False


def _expr_inline_sites(ctx: Ctx):
    """Yield (start, end, helper_bytes, call_bytes) for every value subexpression
    extractable into a `static inline` value helper."""
    if ctx.types is None:
        return
    locals_ = _local_names(ctx.fn)
    fname = fn_name_of(ctx.fn) or "fn"
    for n in iter_subtree(ctx.fn):
        if n.type not in TEMP_EXPR_TYPES:
            continue
        typ = ctx.types.get((n.start_byte, n.end_byte))
        if typ is None or not _usable_temp_type(typ) or not _inline_pos_ok(n):
            continue
        order: List[str] = []
        occ: dict = {}
        for idn in iter_subtree(n):
            if idn.type != "identifier":
                continue
            nm = idn.text.decode()
            if nm not in locals_:
                continue
            occ.setdefault(nm, []).append(idn)
            if nm not in order:
                order.append(nm)
        if len(order) > 6:
            continue
        ftypes: dict = {}
        ok = True
        for nm in order:
            t = _occ_type(occ[nm], ctx.types)
            if not t or not _usable_temp_type(t):
                ok = False
                break
            ftypes[nm] = t
        if not ok or _inline_byval_unsafe(n, ftypes):
            continue
        hid = f"{fname}_pi{n.start_byte}".encode()
        params = (b"void" if not order else
                  b", ".join(ftypes[nm].encode() + b" " + nm.encode() for nm in order))
        args = b", ".join(nm.encode() for nm in order)
        helper = (b"static inline " + typ.encode() + b" " + hid + b"(" + params + b")\n{\n"
                  + b"    return " + n.text + b";\n}\n\n")
        yield (n.start_byte, n.end_byte, helper, hid + b"(" + args + b")")


def p_inline(ctx: Ctx) -> Optional[List[Edit]]:
    """Extract a pure value subexpression into a `static inline` helper at file
    scope and replace it with a call -- recreating the helper-shaped units mwcc
    was originally fed (it inlines them back, but allocates registers across the
    boundary differently). Free variables become parameters (named the same, so
    the body is `return <expr>;` verbatim); globals/functions are referenced
    directly. Needs types, so it only fires on base steps."""
    sites = list(_expr_inline_sites(ctx))
    if not sites:
        return None
    s, e, helper, call = ctx.rng.choice(sites)
    return [(ctx.fn.start_byte, ctx.fn.start_byte, helper), (s, e, call)]


def _storage_base(lv: Optional[Node]) -> Optional[str]:
    """Variable whose own storage a write to `lv` modifies, or None if the path
    dereferences a pointer (-> or *) -- then a pointee changes, not a local."""
    cur = lv
    while cur is not None:
        t = cur.type
        if t == "identifier":
            return cur.text.decode()
        if t == "field_expression":
            op = field(cur, "operator")
            if op is not None and op.text == b"->":
                return None
            cur = field(cur, "argument")
        elif t in ("parenthesized_expression", "subscript_expression"):
            cur = field(cur, "argument") or only_named(cur)
        elif t == "pointer_expression":   # (*p) = ...
            return None
        elif t == "cast_expression":
            cur = field(cur, "value")
        else:
            return None
    return None


def _occ_type(nodes: List[Node], types: dict) -> Optional[str]:
    for n in nodes:
        t = types.get((n.start_byte, n.end_byte))
        if t:
            return t
    return None


def _jump_escapes_run(jump: Node, run_start: int, run_end: int) -> bool:
    """A break/continue is fine only if its target loop/switch is inside the run."""
    targets = {"for_statement", "while_statement", "do_statement"}
    if jump.type == "break_statement":
        targets = targets | {"switch_statement"}
    p = jump.parent
    while p is not None:
        if p.type in targets:
            return not (run_start <= p.start_byte and p.end_byte <= run_end)
        p = p.parent
    return True


def _rederivable_locals(fn: Node, locals_: set) -> dict:
    """Map each body local with a single-declarator initializer to
    `(decl_text, {freevar_name: id_node}, decl_start)`. The freevars are other
    in-scope locals the initializer reads. This lets a helper *re-derive* the
    local from its initializer (the very common `Item* ip = GET_ITEM(gobj);` at
    the top of the helper) and take those freevars as params, instead of
    receiving the local directly -- the shape the "pass all used outer locals"
    heuristic can't otherwise reproduce. Top-level body decls only (the common
    case; keeps it simple and avoids scope subtleties)."""
    body = body_of(fn)
    out: dict = {}
    if body is None:
        return out
    for d in body.named_children:
        if d.type != "declaration":
            continue
        ty = field(d, "type")
        decls = [c for c in d.named_children
                 if not (ty is not None
                         and (c.start_byte, c.end_byte) == (ty.start_byte, ty.end_byte))]
        if len(decls) != 1 or decls[0].type != "init_declarator":
            continue
        idc = decls[0]
        nm = _declared_name(idc)
        init = field(idc, "value")
        if not nm or init is None:
            continue
        freev: dict = {}
        bad = False
        for m in iter_subtree(init):
            if m.type in ("assignment_expression", "update_expression"):
                bad = True   # re-running a side-effecting init would diverge
                break
            if m.type == "identifier":
                inm = m.text.decode()
                if inm in locals_ and inm != nm:
                    freev.setdefault(inm, m)
        if bad:
            continue
        out[nm] = (d.text, freev, d.start_byte)
    return out


def _decl_spell_map(fn: Node, src: bytes) -> dict:
    """`{local_name: b'<type> <declarator>'}` (e.g. `b'HSD_GObj* gobj'`) for the
    function's params and single-declarator body locals -- the exact verbatim
    spelling to reuse when a freevar becomes a helper parameter. Verbatim (vs the
    type oracle) so it survives macro-buried freevars and exotic types."""
    out: dict = {}
    fdecl = function_declarator_of(fn)
    if fdecl is not None:
        pl = field(fdecl, "parameters")
        if pl is not None:
            for p in pl.named_children:
                if p.type == "parameter_declaration":
                    nm = _declared_name(p)
                    if nm:
                        out[nm] = p.text
    body = body_of(fn)
    if body is not None:
        for d in body.named_children:
            if d.type != "declaration":
                continue
            ty = field(d, "type")
            kids = [c for c in d.named_children
                    if not (ty is not None
                            and (c.start_byte, c.end_byte) == (ty.start_byte, ty.end_byte))]
            if len(kids) != 1:
                continue
            inner = field(kids[0], "declarator") if kids[0].type == "init_declarator" else kids[0]
            nm = _declared_name(kids[0])
            if nm and inner is not None:
                out[nm] = src[d.start_byte:inner.end_byte]
    return out


def _run_local(order: List[str], occ: dict, fn: Node, run_end: int) -> set:
    """Outer locals whose first use in the run is a plain `nm = ...` full
    (re)definition and which are never read after the run -- pure run
    temporaries (e.g. loop-scratch `jobj`, assigned then used only inside the
    loop). They get declared *inside* the helper instead of passed, which fixes
    the 'uninitialized variable passed by value' compile error you'd otherwise
    get from handing a write-before-read local to the helper call."""
    after = {m.text.decode() for m in iter_subtree(fn)
             if m.type == "identifier" and m.start_byte >= run_end}
    res: set = set()
    for nm in order:
        if nm in after:
            continue
        first = min(occ[nm], key=lambda n: n.start_byte)
        par = first.parent
        if par is None or par.type != "assignment_expression":
            continue
        op, lhs = field(par, "operator"), field(par, "left")
        if op is None or op.text != b"=" or lhs is None:
            continue
        if (lhs.start_byte, lhs.end_byte) == (first.start_byte, first.end_byte):
            res.add(nm)
    return res


def _block_site(ctx: Ctx, run: List[Node], locals_: set, fname: str,
                spell: dict, rederive=None):
    """Build (start, end, helper_bytes, call_bytes) for extracting `run` into a
    `static inline void` helper, or None if it can't be cleanly extracted."""
    run_start, run_end = run[0].start_byte, run[-1].end_byte
    decl_inside: set = set()
    occ: dict = {}
    order: List[str] = []
    for s in run:
        for m in iter_subtree(s):
            t = m.type
            if t in ("return_statement", "goto_statement"):
                return None
            if t in ("break_statement", "continue_statement") and \
                    _jump_escapes_run(m, run_start, run_end):
                return None
            if t == "declaration":
                ty = field(m, "type")
                for c in m.named_children:
                    if ty is not None and (c.start_byte, c.end_byte) == (ty.start_byte, ty.end_byte):
                        continue
                    nm = _declared_name(c)
                    if nm:
                        decl_inside.add(nm)
            elif t == "identifier":
                nm = m.text.decode()
                if nm in locals_:
                    occ.setdefault(nm, []).append(m)
                    if nm not in order:
                        order.append(nm)
    order = [nm for nm in order if nm not in decl_inside]

    # Pure run-temporaries (defined-then-used only within the run): declare them
    # inside the helper rather than pass them. Needs a verbatim spelling.
    run_local = {nm for nm in _run_local(order, occ, ctx.fn, run_end) if nm in spell}
    order = [nm for nm in order if nm not in run_local]
    prelude: List[Tuple[int, bytes]] = [
        (min(n.start_byte for n in occ[nm]), spell[nm] + b";") for nm in run_local]

    out: set = set()
    for s in run:
        for m in iter_subtree(s):
            lv = None
            if m.type == "assignment_expression":
                lv = field(m, "left")
            elif m.type == "update_expression":
                lv = field(m, "argument") or only_named(m)
            elif m.type == "pointer_expression":
                op = field(m, "operator")
                if op is not None and op.text == b"&":
                    lv = field(m, "argument") or only_named(m)
            if lv is None:
                continue
            base = _storage_base(lv)
            if base in order:
                t = _occ_type(occ[base], ctx.types)
                if t and not t.rstrip().endswith("*"):
                    out.add(base)

    ptypes: dict = {}
    for nm in order:
        t = _occ_type(occ[nm], ctx.types)
        if not t or not _usable_temp_type(t):
            return None
        ptypes[nm] = t
    if len(order) > 6:
        return None
    in_params = [nm for nm in order if nm not in out]
    out_params = [nm for nm in order if nm in out]

    # Optional re-derivation: rather than receive an in-param by value, the
    # helper can re-declare it from its own initializer (verbatim, e.g.
    # `Item* ip = GET_ITEM(gobj);`) and take that initializer's freevars as
    # params instead -- the melee shape where helpers recompute ip from gobj.
    # Freevar params are spelled from their verbatim declarations (`spell`), not
    # the type oracle: the key freevar (gobj) usually appears only inside a
    # macro (GET_ITEM) where clang exposes no per-token type.
    extra_decls: List[bytes] = []
    extra_args: List[bytes] = []
    did_rederive = False
    if rederive:
        rmap = rederive
        targets = [nm for nm in in_params if nm in rmap]
        # keep a name as a param if another re-derived name's init reads it
        targets = [nm for nm in targets
                   if not any(nm in rmap[o][1] for o in targets if o != nm)]
        drop: set = set()
        seen = set(in_params) | set(out_params)
        for nm in targets:
            decl_text, freev, decl_start = rmap[nm]
            # every freevar must be passable by value: not an out-param, and
            # with a verbatim declaration to spell it as a helper parameter.
            if any(fnm in out_params or fnm not in spell for fnm in freev):
                continue
            drop.add(nm)
            prelude.append((decl_start, decl_text))
            for fnm in freev:
                if fnm not in seen:
                    seen.add(fnm)
                    extra_decls.append(spell[fnm])
                    extra_args.append(fnm.encode())
        if drop:
            in_params = [nm for nm in in_params if nm not in drop]
            did_rederive = True

    # 'r' marks the re-derived variant so it has a distinct name from the plain
    # extraction of the same run (run-local decls alone don't rename).
    hid = (f"{fname}_blk{run_start}").encode() + (b"r" if did_rederive else b"")
    decls = [ptypes[nm].encode() + b" " + nm.encode() for nm in in_params]
    decls += extra_decls
    decls += [ptypes[nm].encode() + b" *" + nm.encode() for nm in out_params]
    params = b", ".join(decls) if decls else b"void"
    args = ([nm.encode() for nm in in_params] + extra_args
            + [b"&" + nm.encode() for nm in out_params])

    body_text = bytearray(ctx.src[run_start:run_end])
    derefs: List[Edit] = []
    for nm in out_params:
        for node in occ[nm]:
            derefs.append((node.start_byte - run_start, node.end_byte - run_start,
                           b"(*" + nm.encode() + b")"))
    for s0, e0, rep in sorted(derefs, key=lambda x: x[0], reverse=True):
        body_text[s0:e0] = rep

    pre = b""
    for _, decl_text in sorted(prelude, key=lambda x: x[0]):
        pre += decl_text + b"\n    "
    helper = (b"static inline void " + hid + b"(" + params + b")\n{\n    "
              + pre + bytes(body_text) + b"\n}\n\n")
    return (run_start, run_end, helper, hid + b"(" + b", ".join(args) + b");")


def _block_inline_sites(ctx: Ctx):
    """Yield a site for every contiguous run of 1..5 top-level statements."""
    if ctx.types is None:
        return
    body = body_of(ctx.fn)
    if body is None:
        return
    stmts = [c for c in body.named_children if c.type in STMT_TYPES]
    locals_ = _local_names(ctx.fn)
    fname = fn_name_of(ctx.fn) or "fn"
    spell = _decl_spell_map(ctx.fn, ctx.src)
    rederivable = _rederivable_locals(ctx.fn, locals_)
    for i in range(len(stmts)):
        for j in range(i, min(len(stmts), i + 5)):
            run = stmts[i:j + 1]
            site = _block_site(ctx, run, locals_, fname, spell)
            if site is not None:
                yield site
            # Also offer a variant that re-derives every eligible in-param from
            # its initializer (passing gobj, declaring ip inside) -- a distinct
            # helper signature the plain extraction can't reach.
            if rederivable:
                rsite = _block_site(ctx, run, locals_, fname, spell, rederive=rederivable)
                if rsite is not None and rsite != site:
                    yield rsite


def p_inline_block(ctx: Ctx) -> Optional[List[Edit]]:
    """Extract a contiguous run of statements into a `static inline void` helper
    -- the `it_NNNN_inline_N(gobj, arg1, &pos)` shape. Reads of outer locals
    become by-value params; non-pointer locals whose storage the run writes
    become pointer out-params (uses rewritten to `(*v)`, call passes `&v`);
    nested declarations stay helper-local. Needs types -> base steps only."""
    sites = list(_block_inline_sites(ctx))
    if not sites:
        return None
    s, e, helper, call = ctx.rng.choice(sites)
    return [(ctx.fn.start_byte, ctx.fn.start_byte, helper), (s, e, call)]


def p_multi_inline(ctx: Ctx) -> Optional[List[Edit]]:
    """Apply several non-overlapping inline extractions in one candidate -- the
    lever for functions that need multiple coordinated helpers at once. Every
    site is computed from the same (base) source, so combining them is just
    concatenating disjoint edits: K helper defs (all inserted at the function
    start, where they concatenate) plus K in-place call replacements."""
    sites = list(_expr_inline_sites(ctx)) + list(_block_inline_sites(ctx))
    if len(sites) < 2:
        return None
    ctx.rng.shuffle(sites)
    chosen: List[Tuple[int, int, bytes, bytes]] = []
    occupied: List[Tuple[int, int]] = []
    k = ctx.rng.randint(2, min(5, len(sites)))
    for s, e, helper, call in sites:
        if any(not (e <= a or s >= b) for a, b in occupied):
            continue   # overlaps an already-chosen site
        chosen.append((s, e, helper, call))
        occupied.append((s, e))
        if len(chosen) >= k:
            break
    if len(chosen) < 2:
        return None
    edits: List[Edit] = []
    for s, e, helper, call in chosen:
        edits.append((ctx.fn.start_byte, ctx.fn.start_byte, helper))
        edits.append((s, e, call))
    return edits


PASSES: List[Tuple[str, Callable[[Ctx], Optional[List[Edit]]], float]] = [
    ("temp_for_expr", p_temp_for_expr, 16.0),
    ("inline", p_inline, 12.0),
    ("inline_block", p_inline_block, 12.0),
    ("multi_inline", p_multi_inline, 10.0),
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

    def step(
        self,
        src: bytes,
        rng: random.Random,
        *,
        tree: Optional[Tree] = None,
        fn: Optional[Node] = None,
        types: Optional[dict] = None,
    ) -> Optional[bytes]:
        # `tree`/`fn` let a caller pass a pre-parsed tree for `src` (e.g. the
        # cached parse of the unchanged base source), avoiding a re-parse +
        # find_function on the hot path. They must correspond to `src`.
        # `types` is the clang type oracle, keyed by base-source spans; pass it
        # only when `src` is the base (otherwise the spans won't line up).
        if tree is None:
            tree = parse(src)
        if fn is None:
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
                edits = func(Ctx(src, tree.root_node, fn, rng, types))
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

    def step_named(self, src: bytes, name: str, rng: random.Random,
                   types: Optional[dict] = None) -> Optional[bytes]:
        """Run exactly one pass by name (debugging)."""
        tree = parse(src)
        fn = find_function(tree.root_node, self.fn_name)
        if fn is None:
            return None
        for n, f, _w in self.passes:
            if n == name:
                edits = f(Ctx(src, tree.root_node, fn, rng, types))
                if not edits:
                    return None
                return apply_edits(src, edits)
        raise SystemExit(f"unknown pass: {name}")


def _preview_oracle(c_file: Path) -> Optional[dict]:
    """Best-effort clang type oracle for the standalone preview, so temp_for_expr
    and the inline passes (which need expression types) are inspectable. Resolves
    the checkout from the file's location. Returns None if anything is missing --
    the preview just falls back to the type-free passes."""
    try:
        import type_oracle
        import melee_root
        root = melee_root.find_checkout(c_file.resolve().parent)
        if root is None or not type_oracle.available():
            return None
        flags = type_oracle.clang_flags_for(c_file.resolve(), root / "compile_commands.json")
        if flags is None:
            return None
        return type_oracle.build_oracle(c_file.resolve(), flags)
    except Exception:
        return None


def _main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file")
    ap.add_argument("fn")
    ap.add_argument("--pass", dest="pass_name", default=None)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("-n", type=int, default=1, help="number of stacked mutations")
    ap.add_argument("--no-types", action="store_true",
                    help="skip the clang type oracle (disables temp_for_expr/inline previews)")
    args = ap.parse_args()

    src = open(args.file, "rb").read()
    rng = random.Random(args.seed)
    types = None if args.no_types else _preview_oracle(Path(args.file))
    mut = Mutator(args.fn)
    cur = src
    for _ in range(args.n):
        # the oracle is keyed by base-source spans, so only the first (base) step
        # may use it; stacked steps mutate derived source where spans don't align.
        t = types if cur is src else None
        if args.pass_name:
            new = mut.step_named(cur, args.pass_name, rng, types=t)
        else:
            new = mut.step(cur, rng, types=t)
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
