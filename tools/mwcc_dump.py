#!/usr/bin/env python3
"""Dump the mwcc_debug compiler's IR/backend listing for one function.

Resolves the function's TU (via build/GALE01/report.json, like checkdiff.py),
compiles that TU with the instrumented MWCC, then truncates pcdump.txt to
just that function's section so the output concerns only that function.

Usage: tools/mwcc_dump.py it_802E70BC
       tools/mwcc_dump.py --runner wibo it_802E70BC
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

# Melee checkout root: explicit override, then Claude Code's project dir,
# then assume this script lives at <melee>/tools/.
ROOT = Path(
    os.environ.get("MELEE_ROOT")
    or os.environ.get("CLAUDE_PROJECT_DIR")
    or Path(__file__).resolve().parents[1]
)
REPORT_PATH = ROOT / "build/GALE01/report.json"


def find_unit_for_function(func_name: str) -> Optional[str]:
    """Return the repo-relative source path for the TU defining `func_name`,
    or None if no unit in report.json declares it (same lookup as
    checkdiff.py)."""
    if not REPORT_PATH.exists():
        raise SystemExit(
            f"missing {REPORT_PATH} — run a normal build first so objdiff "
            "writes the report (function->TU lookup needs it)"
        )
    with REPORT_PATH.open("r") as f:
        for unit in json.load(f).get("units", []):
            for function in unit.get("functions", []):
                if function.get("name") == func_name:
                    obj = unit.get("name", "").removeprefix("main/")
                    return f"src/{obj}.c"
    return None


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
        description="Dump the mwcc_debug compiler's listing for one function."
    )
    parser.add_argument("function", help="function name (its TU is resolved automatically)")
    parser.add_argument(
        "--runner",
        choices=("auto", "wibo", "wine"),
        default="auto",
        help="execution backend (default auto = patched wibo with Wine "
        "fallback on SIGBUS; wibo = patched wibo only; wine = Wine only)",
    )
    return parser.parse_args()


def extract_function(body: str, func: str) -> Optional[str]:
    """Return only the `Starting function <func>` section of a pcdump body
    (up to the next `Starting function` line or EOF), or None if absent."""
    lines = body.splitlines(keepends=True)
    marker = "Starting function "
    start = None
    for i, line in enumerate(lines):
        if not line.startswith(marker):
            continue
        if start is None:
            if line[len(marker):].strip() == func:
                start = i
        else:
            return "".join(lines[start:i])
    if start is not None:
        return "".join(lines[start:])
    return None


def format_functions(funcs: list[str]) -> str:
    if len(funcs) <= 20:
        return ", ".join(funcs)
    return f"{len(funcs)} functions, first={funcs[0]}, last={funcs[-1]}"


def format_pass_counts(passes: list[str]) -> str:
    counts = Counter(passes)
    ordered = []
    for name in passes:
        if name not in ordered:
            ordered.append(name)
    return ", ".join(f"{name}={counts[name]}" for name in ordered)


def split_passes(section: str) -> list[tuple[str, list[str]]]:
    """Return dump passes as (pass name, lines) pairs."""
    passes = []
    name = None
    lines: list[str] = []
    for line in section.splitlines():
        if re.match(r"^(?:BEFORE|AFTER|FINAL) .+", line):
            if name is not None:
                passes.append((name, lines))
            name = line
            lines = [line]
        elif name is not None:
            lines.append(line)
    if name is not None:
        passes.append((name, lines))
    return passes


def choose_analysis_pass(passes: list[tuple[str, list[str]]]) -> tuple[str, list[str]]:
    for preferred in (
        "FINAL CODE AFTER INSTRUCTION SCHEDULING",
        "AFTER REGISTER COLORING",
    ):
        for name, lines in reversed(passes):
            if name == preferred:
                return name, lines
    return passes[-1] if passes else ("<none>", [])


def clean_inst(line: str) -> str:
    return line.split(";", 1)[0].strip()


def parse_inst(line: str) -> Optional[tuple[str, list[str], str]]:
    match = re.match(r"^\s+([a-z][a-z0-9.]*)\s+([^;]+)", line)
    if match is None:
        return None
    op = match.group(1)
    operands = [x.strip() for x in match.group(2).split(",")]
    return op, operands, clean_inst(line)


def reg_from_offset_operand(operand: str) -> Optional[tuple[int, str]]:
    match = re.match(r"(-?\d+)\((r\d+)\)", operand)
    if match is None:
        return None
    return int(match.group(1)), match.group(2)


def offset_expr_from_operand(operand: str) -> Optional[tuple[str, str]]:
    """Return (offset expression, base register) for offset memory operands.

    The final dump may preserve source-ish names such as sp24(r1),
    sp24+8(r1), cnt2(r1), or @725+4(r1), so this intentionally accepts more
    than numeric offsets.
    """
    match = re.match(r"([^()]+)\((r\d+)\)$", operand)
    if match is None:
        return None
    return match.group(1), match.group(2)


def split_stack_expr(expr: str) -> tuple[str, Optional[int]]:
    expr = expr.strip()
    if re.fullmatch(r"-?\d+", expr):
        return expr, 0
    match = re.fullmatch(r"([A-Za-z_@][A-Za-z0-9_@.]*)([+-]\d+)?", expr)
    if match is None:
        return expr, None
    delta = match.group(2)
    return match.group(1), int(delta) if delta is not None else 0


def is_reg(operand: str) -> bool:
    return re.fullmatch(r"r\d+", operand) is not None


def regs_in_operand(operand: str) -> list[str]:
    return re.findall(r"\br\d+\b", operand)


def format_offsets(offsets: set[int]) -> str:
    if not offsets:
        return "-"
    shown = sorted(offsets)
    if len(shown) > 6:
        return ", ".join(str(x) for x in shown[:6]) + ", ..."
    return ", ".join(str(x) for x in shown)


def is_indexed_mem_op(op: str) -> bool:
    return op in {
        "lbzx", "lhax", "lhzx", "lwzx", "lfsx",
        "stbx", "sthx", "stwx", "stfsx",
    }


def is_offset_mem_op(op: str) -> bool:
    return op in {
        "lbz", "lha", "lhz", "lwz", "lfs", "lfd",
        "stb", "sth", "stw", "stfs", "stfd",
    }


def find_address_forms(lines: list[str]) -> list[str]:
    insts = [parsed for line in lines if (parsed := parse_inst(line))]
    counts: Counter[str] = Counter()
    folded_samples = []
    byte_index_samples = []

    for i, (op, operands, text) in enumerate(insts):
        if is_indexed_mem_op(op):
            counts[f"{op} indexed"] += 1
        elif is_offset_mem_op(op):
            for operand in operands[1:]:
                parsed = reg_from_offset_operand(operand)
                if parsed is not None:
                    offset, _ = parsed
                    if offset == 0:
                        counts[f"{op} offset0"] += 1
                    else:
                        counts[f"{op} offset"] += 1

        if op == "add" and len(operands) == 3:
            dest = operands[0]
            for next_op, next_operands, next_text in insts[i + 1:i + 4]:
                if not is_offset_mem_op(next_op):
                    continue
                for operand in next_operands[1:]:
                    parsed = reg_from_offset_operand(operand)
                    if parsed is not None and parsed[0] != 0 and parsed[1] == dest:
                        folded_samples.append(f"{text}; {next_text}")
                        break
                if len(folded_samples) >= 3:
                    break

        if op == "addi" and len(operands) == 3 and operands[2] != "0":
            dest = operands[0]
            for next_op, next_operands, next_text in insts[i + 1:i + 4]:
                if not is_indexed_mem_op(next_op):
                    continue
                if any(f"({dest}" in operand for operand in next_operands):
                    byte_index_samples.append(f"{text}; {next_text}")
                    break

    if not counts and not folded_samples and not byte_index_samples:
        return []

    summary = ", ".join(f"{name}={counts[name]}" for name in sorted(counts))
    out = [f"  address forms: {summary}" if summary else "  address forms:"]
    for sample in folded_samples[:3]:
        out.append(f"    folded offset: {sample}")
    for sample in byte_index_samples[:3]:
        out.append(f"    byte-offset indexed: {sample}")
    if folded_samples and byte_index_samples:
        out.append(
            "    hint: mixed next-element forms; try byte-offset pointer casts "
            "when a target wants addi base,+N plus indexed load"
        )
    return out


def find_branch_shapes(lines: list[str]) -> list[str]:
    insts = [parsed for line in lines if (parsed := parse_inst(line))]
    branch_counts: Counter[str] = Counter()
    samples = []
    exit_blocks = set(re.findall(r"^(B\d+): Succ=\{\}", "\n".join(lines), re.M))
    exit_branches = 0

    for op, operands, text in insts:
        if op not in {"b", "bt", "bf"}:
            continue
        if op == "b":
            branch_counts["b"] += 1
            target = operands[0] if operands else ""
        else:
            cond = operands[1] if len(operands) > 1 else "?"
            branch_counts[f"{op} {cond}"] += 1
            target = operands[2] if len(operands) > 2 else ""
        if target in exit_blocks:
            exit_branches += 1
        if op in {"bt", "bf"} and len(samples) < 4:
            samples.append(text)

    if not branch_counts:
        return []

    summary = ", ".join(f"{name}={branch_counts[name]}" for name in sorted(branch_counts))
    out = [f"  branch shapes: {summary}; branches-to-exit={exit_branches}"]
    for sample in samples:
        out.append(f"    conditional: {sample}")
    return out


def format_deltas(deltas: set[int]) -> str:
    if not deltas:
        return "-"
    shown = sorted(deltas)
    values = [f"{delta:+d}" for delta in shown[:6]]
    if len(shown) > 6:
        values.append("...")
    return ", ".join(values)


def has_vec3_deltas(deltas: set[int]) -> bool:
    ordered = sorted(deltas)
    for delta in ordered:
        if {delta, delta + 4, delta + 8}.issubset(deltas):
            return True
    return False


def is_arg_reg(reg: str) -> bool:
    return re.fullmatch(r"r([3-9]|10)", reg) is not None


def find_stack_slot_summaries(lines: list[str]) -> list[str]:
    insts = [
        (idx, line, parsed)
        for idx, line in enumerate(lines)
        if (parsed := parse_inst(line))
    ]
    slots: dict[str, dict[str, object]] = {}
    stack_ptrs: dict[str, tuple[str, Optional[int], str]] = {}

    def slot_info(key: str) -> dict[str, object]:
        return slots.setdefault(key, {
            "ops": Counter(),
            "load_ops": Counter(),
            "store_ops": Counter(),
            "deltas": set(),
            "volatile": False,
            "fctiwz": False,
            "ptr_defs": [],
            "arg_ptrs": [],
            "indexed_ops": Counter(),
            "index_regs": set(),
            "samples": [],
        })

    def remember_sample(info: dict[str, object], text: str) -> None:
        samples = info["samples"]
        assert isinstance(samples, list)
        if len(samples) < 2:
            samples.append(text)

    for inst_pos, (_idx, line, (op, operands, text)) in enumerate(insts):
        if op == "addi" and len(operands) == 3 and operands[1] == "r1":
            key, delta = split_stack_expr(operands[2])
            stack_ptrs[operands[0]] = (key, delta, text)
            info = slot_info(key)
            ptr_defs = info["ptr_defs"]
            assert isinstance(ptr_defs, list)
            ptr_defs.append(text)
            if is_arg_reg(operands[0]):
                arg_ptrs = info["arg_ptrs"]
                assert isinstance(arg_ptrs, list)
                arg_ptrs.append(text)

        if is_offset_mem_op(op) and len(operands) >= 2:
            for operand in operands[1:]:
                parsed = offset_expr_from_operand(operand)
                if parsed is None:
                    continue
                expr, base = parsed
                if base != "r1":
                    continue
                key, delta = split_stack_expr(expr)
                info = slot_info(key)
                ops = info["ops"]
                assert isinstance(ops, Counter)
                ops[op] += 1
                if delta is not None:
                    deltas = info["deltas"]
                    assert isinstance(deltas, set)
                    deltas.add(delta)
                if "fIsVolatile" in line:
                    info["volatile"] = True
                if op.startswith("st"):
                    store_ops = info["store_ops"]
                    assert isinstance(store_ops, Counter)
                    store_ops[op] += 1
                else:
                    load_ops = info["load_ops"]
                    assert isinstance(load_ops, Counter)
                    load_ops[op] += 1
                if op == "stfd":
                    recent = [
                        prior[2][0]
                        for prior in insts[max(0, inst_pos - 3):inst_pos]
                    ]
                    if "fctiwz" in recent:
                        info["fctiwz"] = True
                remember_sample(info, text)

        if is_indexed_mem_op(op):
            match = re.search(r"\((r\d+),(r\d+)\)", text)
            if match is None:
                continue
            base_reg, index_reg = match.groups()
            if base_reg not in stack_ptrs:
                continue
            key, _delta, _def_text = stack_ptrs[base_reg]
            info = slot_info(key)
            indexed_ops = info["indexed_ops"]
            assert isinstance(indexed_ops, Counter)
            indexed_ops[op] += 1
            index_regs = info["index_regs"]
            assert isinstance(index_regs, set)
            index_regs.add(index_reg)
            remember_sample(info, text)

    summaries: list[tuple[int, str]] = []
    for key, info in slots.items():
        ops = info["ops"]
        load_ops = info["load_ops"]
        store_ops = info["store_ops"]
        indexed_ops = info["indexed_ops"]
        deltas = info["deltas"]
        index_regs = info["index_regs"]
        arg_ptrs = info["arg_ptrs"]
        samples = info["samples"]
        assert isinstance(ops, Counter)
        assert isinstance(load_ops, Counter)
        assert isinstance(store_ops, Counter)
        assert isinstance(indexed_ops, Counter)
        assert isinstance(deltas, set)
        assert isinstance(index_regs, set)
        assert isinstance(arg_ptrs, list)
        assert isinstance(samples, list)

        parts = []
        priority = 0
        if has_vec3_deltas(deltas) and store_ops.get("stfs", 0) >= 3:
            parts.append(f"Vec3-like f32 stores at {format_deltas(deltas)}")
            priority = max(priority, 4)
        if indexed_ops:
            indexed_summary = ", ".join(
                f"{name}={indexed_ops[name]}" for name in sorted(indexed_ops)
            )
            index_summary = ", ".join(sorted(index_regs))
            parts.append(
                f"indexed stack array ({indexed_summary}; index regs {index_summary})"
            )
            priority = max(priority, 4)
        if info["volatile"] and load_ops.get("lwz", 0) and store_ops.get("stw", 0):
            parts.append(
                f"volatile s32 slot (lwz={load_ops['lwz']}, stw={store_ops['stw']})"
            )
            priority = max(priority, 3)
        if info["fctiwz"] and store_ops.get("stfd", 0):
            parts.append("fctiwz conversion scratch")
            priority = max(priority, 3)
        if arg_ptrs:
            parts.append(f"passed by pointer via {arg_ptrs[0]}")
            priority = max(priority, 2)

        # Avoid noisy callee-save and frame-bookkeeping slots unless they also
        # matched a useful local-stack pattern above.
        if not parts or priority < 2:
            continue

        if samples:
            parts.append(f"sample: {samples[0]}")
        summaries.append((priority, f"    {key}: " + "; ".join(parts)))

    if not summaries:
        return []

    summaries.sort(key=lambda item: (-item[0], item[1]))
    out = ["  stack slots:"]
    out.extend(text for _, text in summaries[:8])
    return out


def infer_register_roles(lines: list[str]) -> list[str]:
    insts = [(idx, parsed) for idx, line in enumerate(lines) if (parsed := parse_inst(line))]
    mem_offsets: dict[str, set[int]] = {}
    indexed_mem_uses: Counter[str] = Counter()
    copy_from: dict[str, tuple[str, str, int]] = {}
    copy_to: dict[str, list[tuple[str, str, int]]] = {}
    li_values: dict[str, set[int]] = {}
    cmp_zero: set[str] = set()
    byte_load_dests: dict[str, list[tuple[str, int]]] = {}
    half_load_dests: dict[str, list[tuple[str, int]]] = {}
    stores_from: dict[str, list[tuple[str, int]]] = {}

    for idx, (op, operands, text) in insts:
        if op == "mr" and len(operands) == 2:
            copy_from[operands[0]] = (operands[1], text, idx)
            copy_to.setdefault(operands[1], []).append((operands[0], text, idx))
        elif op == "addi" and len(operands) == 3 and operands[2] == "0":
            copy_from[operands[0]] = (operands[1], text, idx)
            copy_to.setdefault(operands[1], []).append((operands[0], text, idx))
        elif op == "li" and len(operands) == 2 and is_reg(operands[0]):
            try:
                li_values.setdefault(operands[0], set()).add(int(operands[1], 0))
            except ValueError:
                pass
        elif op in {"cmpi", "cmpwi", "cmpli", "cmplwi"} and len(operands) >= 3:
            reg = operands[-2]
            imm = operands[-1]
            if is_reg(reg) and imm == "0":
                cmp_zero.add(reg)

        if is_offset_mem_op(op) and len(operands) >= 2:
            dest_or_src = operands[0]
            for operand in operands[1:]:
                parsed = reg_from_offset_operand(operand)
                if parsed is None:
                    continue
                offset, base = parsed
                mem_offsets.setdefault(base, set()).add(offset)
                is_stack_access = base == "r1"
                if op.startswith("st") and is_reg(dest_or_src):
                    if not is_stack_access:
                        stores_from.setdefault(dest_or_src, []).append((text, idx))
                elif op in {"lbz", "lha", "lhz", "lwz"} and is_reg(dest_or_src):
                    if op == "lbz":
                        byte_load_dests.setdefault(dest_or_src, []).append((text, idx))
                    elif op == "lhz":
                        half_load_dests.setdefault(dest_or_src, []).append((text, idx))

        if is_indexed_mem_op(op) and len(operands) >= 2:
            dest_or_src = operands[0]
            for operand in operands[1:]:
                for reg in regs_in_operand(operand):
                    indexed_mem_uses[reg] += 1
            if op.startswith("st") and is_reg(dest_or_src):
                stores_from.setdefault(dest_or_src, []).append((text, idx))
            elif op == "lbzx" and is_reg(dest_or_src):
                byte_load_dests.setdefault(dest_or_src, []).append((text, idx))
            elif op == "lhzx" and is_reg(dest_or_src):
                half_load_dests.setdefault(dest_or_src, []).append((text, idx))

    roles = []

    for reg, offsets in sorted(mem_offsets.items(), key=lambda item: (-len(item[1]), item[0])):
        if len(offsets) < 3:
            continue
        origin = copy_from.get(reg)
        if origin is not None and origin[0] == "r3":
            roles.append(
                f"    {reg}: likely arg0/base pointer from r3; "
                f"offset memory ops={len(offsets)} at {format_offsets(offsets)}; "
                f"copy: {origin[1]}"
            )
        elif reg in {"r28", "r29", "r30", "r31"}:
            roles.append(
                f"    {reg}: callee-saved base pointer; "
                f"offset memory ops={len(offsets)} at {format_offsets(offsets)}"
            )

    for reg, values in sorted(li_values.items()):
        if {0, 1}.issubset(values) and reg in cmp_zero:
            roles.append(
                f"    {reg}: likely boolean flag; initialized/set with li 0/1 "
                "and compared against zero"
            )

    for reg, (src, text, copy_idx) in sorted(copy_from.items()):
        if reg not in {"r28", "r29", "r30", "r31"}:
            continue
        store_samples = [sample for sample, store_idx in stores_from.get(reg, [])
                         if store_idx > copy_idx]
        prior_load = None
        for loads in (half_load_dests.get(src, []), byte_load_dests.get(src, [])):
            for sample, load_idx in loads:
                if load_idx < copy_idx and (prior_load is None or load_idx > prior_load[1]):
                    prior_load = (sample, load_idx)
        if prior_load is not None or src in {"r4", "r5", "r6"}:
            if prior_load is not None:
                role = f"saved loaded value from {src}; load: {prior_load[0]}"
            else:
                role = f"saved incoming/current {src} value"
            roles.append(
                f"    {reg}: likely {role}; copy: {text}"
                + (f"; later store: {store_samples[0]}" if store_samples else "")
            )

    for src, copies in sorted(copy_to.items()):
        if src not in byte_load_dests:
            continue
        for dest, text, copy_idx in copies:
            if dest == "r3":
                prior_byte_loads = [
                    (sample, load_idx) for sample, load_idx in byte_load_dests[src]
                    if load_idx < copy_idx
                ]
                if not prior_byte_loads:
                    continue
                sample, _ = prior_byte_loads[-1]
                roles.append(
                    f"    return byte copy: {sample}; {text}"
                )

    # De-duplicate while preserving order.
    seen = set()
    deduped = []
    for role in roles:
        if role not in seen:
            seen.add(role)
            deduped.append(role)
    return deduped[:10]


def find_copy_shapes(section: str, lines: list[str]) -> list[str]:
    log = section.split("BEFORE GLOBAL OPTIMIZATION", 1)[0]
    log_counts = {
        "propagatable assignments": len(re.findall(r"Found propagatable assignment", log)),
        "expression propagations": len(re.findall(r"Found expression propagation", log)),
        "common-sub replacements": len(re.findall(r"Replacing common sub", log)),
        "dead assignments": len(re.findall(r"Removing dead assignment", log)),
    }
    insts = [parsed for line in lines if (parsed := parse_inst(line))]
    zero_copies = []
    for op, operands, text in insts:
        is_mr = op == "mr" and len(operands) == 2
        is_addi_zero = op == "addi" and len(operands) == 3 and operands[2] == "0"
        if is_mr or is_addi_zero:
            zero_copies.append(text)

    if not any(log_counts.values()) and not zero_copies:
        return []

    out = [
        "  optimizer facts: "
        + ", ".join(f"{name}={count}" for name, count in log_counts.items())
        + f"; zero-copy ops={len(zero_copies)}"
    ]
    roles = infer_register_roles(lines)
    if roles:
        out.append("  register role clues:")
        out.extend(roles)
    else:
        for sample in zero_copies[:5]:
            out.append(f"    copy: {sample}")
    return out


# HSD_GObj.user_data offset: GET_ITEM/GET_FIGHTER/GET_GROUND all expand to
# `(T*)gobj->user_data`, i.e. `lwz rD, 0x2c(rGobj)`. A function holds its own
# `item`/`fighter`/`ground` in one local; a *second* load of 0x2c from the
# same (unchanged) gobj arg is the fingerprint of an inlined helper that ran
# `GET_x(gobj)` of its own.
GOBJ_USER_DATA_OFF = 0x2C


def _def_reg(op: str, operands: list[str]) -> Optional[str]:
    """The GPR an instruction redefines (operands[0]), or None for
    stores/branches/compares that don't write a GPR. Good enough to tell
    whether a base register changed between two loads."""
    if not operands:
        return None
    if op.startswith("st") or op in {"b", "bt", "bf", "bl", "blr", "cmp",
                                     "cmpi", "cmpwi", "cmpli", "cmplwi"}:
        return None
    return operands[0] if is_reg(operands[0]) else None


def _arg0_aliases(insts: list[tuple[str, list[str], str]]) -> set[str]:
    """Registers that hold arg0 (the gobj). r3, plus anything copied from an
    arg0 alias via `mr`/`addi rD,rS,0`, transitively — until that register is
    redefined by something else."""
    aliases = {"r3"}
    for op, operands, _text in insts:
        if op == "mr" and len(operands) == 2:
            if operands[1] in aliases:
                aliases.add(operands[0])
            elif operands[0] in aliases:
                aliases.discard(operands[0])  # clobbered by an unrelated mr
        elif op == "addi" and len(operands) == 3 and operands[2] == "0":
            if operands[1] in aliases:
                aliases.add(operands[0])
            elif operands[0] in aliases:
                aliases.discard(operands[0])
        else:
            d = _def_reg(op, operands)
            if d in aliases and d != "r3":
                aliases.discard(d)
    return aliases


def find_rederivations(lines: list[str]) -> list[str]:
    """Flag values re-derived from a still-live base — a strong (but not
    exhaustive) tell that an inlined helper lived here. Phrased as a lead to
    investigate, never a directive.

    Headline signal: `lwz rD, 0x2c(gobj)` (GET_ITEM/GET_FIGHTER/GET_GROUND)
    issued 2+ times against the same, unredefined gobj-arg register. The
    top-level function holds that pointer in one local; a re-fetch is almost
    always an inlined `GET_x(gobj)` from a helper body.

    Caveat (do not over-read a *clean* result): a helper that takes the value
    as a parameter (e.g. `f(item_gobj, attr, …)`) leaves no re-derivation
    fingerprint, so absence of this flag is NOT evidence that no helper was
    factored out.
    """
    insts = [parsed for line in lines if (parsed := parse_inst(line))]
    arg0 = _arg0_aliases(insts)

    def base_unchanged(p1: int, p2: int, base: str) -> bool:
        return not any(
            _def_reg(insts[p][0], insts[p][1]) == base
            for p in range(p1 + 1, p2)
        )

    gobj_hits: list[str] = []
    generic_hits: list[str] = []

    # GObj re-fetch: `lwz rD, 0x2c(gobj)` (GET_ITEM/GET_FIGHTER/GET_GROUND).
    # Group by the *value* (arg0), not the physical reg: the top-level fetch
    # and an inlined helper's fetch usually land on different alias regs
    # (e.g. r3 vs a callee-saved copy r27). arg0 is a function argument, so
    # it is effectively never clobbered — no base-unchanged check needed.
    gobj_loads = [
        (pos, parsed[1])
        for pos, (op, operands, _t) in enumerate(insts)
        if op == "lwz" and len(operands) >= 2
        and (parsed := reg_from_offset_operand(operands[1])) is not None
        and parsed[0] == GOBJ_USER_DATA_OFF
        and parsed[1] in arg0
        and operands[0] != parsed[1]
    ]
    if len(gobj_loads) >= 2:
        regs = ", ".join(dict.fromkeys(b for _p, b in gobj_loads))
        gobj_hits.append(
            f"    gobj->user_data fetched {len(gobj_loads)}x (lwz rD,0x2c "
            f"via {regs}) — top-level GET_x holds it in one local; the "
            f"extra fetch(es) are an inlined GET_ITEM/GET_FIGHTER/GET_GROUND"
        )

    # Generic: a pointer field reloaded from the same, unredefined base, with
    # no call and no store anywhere between (a call/store would make the
    # reload ordinary rather than a source-level re-derivation). Strict, to
    # stay low-noise; excludes the GObj case handled above.
    occ: dict[tuple[int, str], list[int]] = {}
    for pos, (op, operands, _text) in enumerate(insts):
        if op != "lwz" or len(operands) < 2:
            continue
        parsed = reg_from_offset_operand(operands[1])
        if parsed is None:
            continue
        offset, base = parsed
        if operands[0] == base:  # pointer-walk (lwz rX,k(rX)), not a re-fetch
            continue
        if offset == GOBJ_USER_DATA_OFF and base in arg0:
            continue  # already covered by the GObj path
        occ.setdefault((offset, base), []).append(pos)

    for (offset, base), positions in sorted(occ.items()):
        if len(positions) < 3:
            continue
        stable = 1
        for prev, cur in zip(positions, positions[1:]):
            if base_unchanged(prev, cur, base):
                stable += 1
            else:
                break
        if stable < 3:
            continue
        lo, hi = positions[0], positions[stable - 1]
        between = insts[lo + 1:hi]
        if any(o == "bl" or o.startswith("st") for o, _ops, _t in between):
            continue
        if base in arg0 or base in {"r28", "r29", "r30", "r31"}:
            generic_hits.append(
                f"    {base}: 0x{offset:x}({base}) reloaded {stable}x with no "
                f"call/store between and base unchanged — value re-derived "
                f"in straight-line code"
            )

    if not gobj_hits and not generic_hits:
        return []
    out = ["  re-derivation (possible inlined-helper boundary — a lead, "
           "not a directive):"]
    out.extend(gobj_hits[:3])
    out.extend(generic_hits[:3])
    out.append(
        "    consider whether the re-deriving region was a separate "
        "static-inline helper (won't fire for param-passing helpers)"
    )
    return out


def find_frame_summary(lines: list[str]) -> list[str]:
    """Report the prologue frame size, saved registers, and distinct local
    stack slots. This is the first thing to check for an objdiff `f`/`s`
    (frame/stack) mismatch: it lets you compare frame size and slot count
    without opening the multi-thousand-line pcdump.
    """
    frame = None
    saved = []
    local_slots: dict[str, Counter] = {}
    conv_slots: set[str] = set()

    insts = [parsed for line in lines if (parsed := parse_inst(line))]
    for pos, (op, operands, _text) in enumerate(insts):
        if op == "stwu" and len(operands) >= 2 and operands[0] == "r1":
            m = re.match(r"-(\d+)\(r1\)", operands[1])
            if m is not None and frame is None:
                n = int(m.group(1))
                frame = f"0x{n:x} ({n} bytes)"
        elif op in {"stmw", "stw", "stfd"} and len(operands) >= 2:
            base = reg_from_offset_operand(operands[1])
            if base is not None and base[1] == "r1" and re.fullmatch(
                r"f?r?\d+", operands[0]
            ):
                # Saved callee registers live at high, fixed offsets; skip
                # @NNN compiler temps (handled below).
                if "@" not in operands[1]:
                    saved.append(f"{op} {operands[0]}@{base[0]}")
        # Distinct compiler local slots: @NNN(r1) / addi rD,r1,@NNN.
        for operand in operands:
            m = re.search(r"(@\d+)(?:\+\d+)?\(r1\)", operand)
            if m is None and operand.startswith("@") and "r1" in (
                operands[0] if operands else ""
            ):
                m = re.match(r"(@\d+)", operand)
            if m is not None:
                slot = m.group(1)
                local_slots.setdefault(slot, Counter())[op] += 1
                window = [p[0] for p in insts[max(0, pos - 3):pos]]
                if op == "lfd" and ("xoris" in window or "fctiwz" in window):
                    conv_slots.add(slot)

    if frame is None and not local_slots:
        return []
    out = [f"  frame: {frame or 'unknown'}"]
    if saved:
        # De-dup stmw expands to one entry; keep it compact.
        uniq = []
        for s in saved:
            if s not in uniq:
                uniq.append(s)
        out.append("  saved regs: " + ", ".join(uniq[:8]))
    if local_slots:
        parts = []
        for slot, ops in sorted(local_slots.items()):
            tag = " (i2f/fctiwz temp)" if slot in conv_slots else ""
            opsum = ",".join(f"{k}={v}" for k, v in sorted(ops.items()))
            parts.append(f"{slot}[{opsum}]{tag}")
        out.append(f"  local stack slots ({len(local_slots)}): " + "; ".join(parts[:10]))
    return out


def print_shape_summary(func: str, pcdump: Path, section: str) -> None:
    passes = split_passes(section)
    pass_names = [name for name, _ in passes]
    pass_name, pass_lines = choose_analysis_pass(passes)
    section_lines = section.splitlines()

    print(
        f"[mwcc_dump] {func}: {len(section_lines)} lines; "
        f"full function dump available at: {pcdump}"
    )
    print(f"[mwcc_dump] passes: {format_pass_counts(pass_names)}")
    print(f"[mwcc_dump] shape analysis from: {pass_name}")

    details = (
        find_frame_summary(pass_lines)
        + find_address_forms(pass_lines)
        + find_stack_slot_summaries(pass_lines)
        + find_branch_shapes(pass_lines)
        + find_copy_shapes(section, pass_lines)
        + find_rederivations(pass_lines)
    )
    if details:
        for line in details:
            print(f"[mwcc_dump]{line}")
    else:
        print("[mwcc_dump]  no address/branch/copy patterns recognized")


def finalize_dump(func: str) -> int:
    """Truncate pcdump.txt to just `func`'s section and print a one-line
    summary. Returns a process-style exit code (0 = section found)."""
    pcdump = ROOT / "pcdump.txt"
    if not pcdump.exists():
        print("[mwcc_dump] no pcdump.txt produced", file=sys.stderr)
        return 1

    body = pcdump.read_text(errors="replace")
    section = extract_function(body, func)

    if section is None:
        # Leave the full dump in place so the user can inspect it; the most
        # useful thing we can offer is the list of names that *are* present
        # (the function was likely inlined, or the name is wrong).
        present = re.findall(r"^Starting function (\S+)", body, re.M)
        print(f"[mwcc_dump] {func!r} not found (inlined or wrong name); "
              f"present: {format_functions(present)}", file=sys.stderr)
        return 1

    pcdump.write_text(section)
    print_shape_summary(func, pcdump, section)
    return 0


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
    func = args.function
    src = find_unit_for_function(func)
    if src is None:
        raise SystemExit(
            f"could not find function {func!r} in {REPORT_PATH} "
            "(check the name, or rebuild so report.json is current)"
        )
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

    return finalize_dump(func)


if __name__ == "__main__":
    raise SystemExit(main())
