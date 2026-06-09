use std::{
    collections::HashSet,
    io::{BufRead, Write},
    path::Path,
};

use anyhow::{Context, Result};
use argp::FromArgs;
use objdiff_core::{
    diff::{
        self, DiffObjConfig, DiffSide, FunctionRelocDiffs, InstructionDiffKind, InstructionDiffRow,
        MappingConfig, SymbolDiff,
    },
    obj::{self, InstructionArg, InstructionArgValue, Object},
};

#[derive(FromArgs, PartialEq, Debug)]
/// Persistent scoring server for the source permuter. Parses the target object
/// once, then reads candidate object-file paths (one per line) on stdin and
/// writes each one's `diff_score <hash> <hard> <regswap> <stack>` for a single
/// function on stdout.
/// `diff_score` is objdiff's raw penalty (0 = perfect match); `hash` is a
/// deterministic fingerprint of the function's code bytes (for novelty/dedup).
/// The final three numbers are a compact mismatch ranking breakdown for the
/// source permuter: structural/instruction-selection mismatches first, deduped
/// register swaps second, and stack/frame differences last.
#[argp(subcommand, name = "score")]
pub struct Args {
    #[argp(positional)]
    /// Target (expected) object file.
    target: String,
    #[argp(positional)]
    /// Function symbol to score.
    function: String,
    #[argp(option, short = 'c')]
    /// Extra diff config property (key=value), repeatable.
    config: Vec<String>,
}

pub fn run(args: Args) -> Result<()> {
    // data_value relocs so a true match (score 0) requires referenced data to
    // match too -- matching the percent the permuter reports for finds.
    let mut diff_config = DiffObjConfig {
        function_reloc_diffs: FunctionRelocDiffs::DataValue,
        ..Default::default()
    };
    super::apply_config_args(&mut diff_config, &args.config)?;
    let mapping = MappingConfig::default();

    let target = obj::read::read(Path::new(&args.target), &diff_config, DiffSide::Target)
        .with_context(|| format!("Failed to read target {}", args.target))?;

    let stdin = std::io::stdin();
    let stdout = std::io::stdout();
    let mut out = stdout.lock();
    // Handshake so the client knows the (one-time) target parse is done.
    writeln!(out, "READY")?;
    out.flush()?;

    let mut line = String::new();
    loop {
        line.clear();
        if stdin.lock().read_line(&mut line)? == 0 {
            break; // EOF: client closed stdin (shutdown)
        }
        let path = line.trim();
        if path.is_empty() {
            continue;
        }
        match score_one(&target, &args.function, path, &diff_config, &mapping) {
            Ok((score, hash, breakdown)) => writeln!(
                out,
                "{score} {hash:016x} {} {} {}",
                breakdown.hard, breakdown.regswap, breakdown.stack
            )?,
            // Per-candidate failure (bad object, missing symbol): report and keep
            // serving so one bad candidate never takes the server down.
            Err(e) => writeln!(out, "ERR {}", e.to_string().replace('\n', " "))?,
        }
        out.flush()?;
    }
    Ok(())
}

fn score_one(
    target: &Object,
    function: &str,
    path: &str,
    cfg: &DiffObjConfig,
    mapping: &MappingConfig,
) -> Result<(u64, u64, ScoreBreakdown)> {
    let data = std::fs::read(path).with_context(|| format!("read {path}"))?;
    let cand = obj::read::parse(&data, cfg, DiffSide::Base).context("parse candidate")?;
    let target_idx = target
        .symbol_by_name(function)
        .with_context(|| format!("target symbol {function} not found"))?;
    let idx = cand
        .symbol_by_name(function)
        .with_context(|| format!("symbol {function} not found"))?;
    let result = diff::diff_objs(Some(target), Some(&cand), None, cfg, mapping)?;
    let target_diff = &result.left.as_ref().context("no target diff")?.symbols[target_idx];
    let sym_diff = &result.right.as_ref().context("no base diff")?.symbols[idx];
    let score = sym_diff.diff_score.map_or(0, |(s, _)| s);
    let breakdown =
        summarize_mismatches(target, target_idx, target_diff, &cand, idx, sym_diff, cfg);
    Ok((score, fn_hash(&cand, idx), breakdown))
}

#[derive(Default)]
struct ScoreBreakdown {
    hard: u64,
    regswap: u64,
    stack: u64,
}

fn summarize_mismatches(
    target_obj: &Object,
    target_symbol_idx: usize,
    target_diff: &SymbolDiff,
    base_obj: &Object,
    base_symbol_idx: usize,
    base_diff: &SymbolDiff,
    diff_config: &DiffObjConfig,
) -> ScoreBreakdown {
    let target_frame =
        frame_size(target_obj, target_symbol_idx, target_diff, diff_config).unwrap_or(0);
    let base_frame = frame_size(base_obj, base_symbol_idx, base_diff, diff_config).unwrap_or(0);
    let frame_diff = base_frame as i64 - target_frame as i64;

    let mut out = ScoreBreakdown::default();
    let mut seen_regswaps = HashSet::<(String, String)>::new();
    let rows = target_diff
        .instruction_rows
        .len()
        .max(base_diff.instruction_rows.len());
    for i in 0..rows {
        classify_row(
            target_obj,
            target_symbol_idx,
            target_diff.instruction_rows.get(i),
            base_obj,
            base_symbol_idx,
            base_diff.instruction_rows.get(i),
            frame_diff,
            diff_config,
            &mut seen_regswaps,
            &mut out,
        );
    }
    out
}

fn classify_row(
    target_obj: &Object,
    target_symbol_idx: usize,
    target_row: Option<&InstructionDiffRow>,
    base_obj: &Object,
    base_symbol_idx: usize,
    base_row: Option<&InstructionDiffRow>,
    frame_diff: i64,
    diff_config: &DiffObjConfig,
    seen_regswaps: &mut HashSet<(String, String)>,
    out: &mut ScoreBreakdown,
) {
    let kind = match (target_row, base_row) {
        (Some(tr), Some(br)) => {
            if tr.kind != InstructionDiffKind::None {
                tr.kind
            } else {
                br.kind
            }
        }
        (Some(tr), None) => tr.kind,
        (None, Some(br)) => br.kind,
        (None, None) => return,
    };
    match kind {
        InstructionDiffKind::None => {}
        InstructionDiffKind::Insert | InstructionDiffKind::Delete => out.hard += 1,
        InstructionDiffKind::Replace | InstructionDiffKind::OpMismatch => out.hard += 1,
        InstructionDiffKind::ArgMismatch => {
            let parsed = (|| {
                let tr = target_row?;
                let br = base_row?;
                let t_ref = tr.ins_ref?;
                let b_ref = br.ins_ref?;
                let t_resolved = target_obj.resolve_instruction_ref(target_symbol_idx, t_ref)?;
                let b_resolved = base_obj.resolve_instruction_ref(base_symbol_idx, b_ref)?;
                let t_parsed = target_obj
                    .arch
                    .process_instruction(t_resolved, diff_config)
                    .ok()?;
                let b_parsed = base_obj
                    .arch
                    .process_instruction(b_resolved, diff_config)
                    .ok()?;
                Some((tr, br, t_resolved, b_resolved, t_parsed, b_parsed))
            })();
            let Some((tr, br, t_resolved, b_resolved, t_parsed, b_parsed)) = parsed else {
                out.hard += 1;
                return;
            };

            if frame_diff != 0
                && !is_stwu_r1(&t_parsed)
                && !is_stwu_r1(&b_parsed)
                && is_stack_shift(&t_parsed, &b_parsed, tr, br, frame_diff)
            {
                out.stack += 1;
                return;
            }

            let references_r1 = t_parsed.args.iter().any(is_r1) || b_parsed.args.iter().any(is_r1);
            let same_resolved_addr = match (t_resolved.relocation, b_resolved.relocation) {
                (Some(t_reloc), Some(b_reloc)) => {
                    t_reloc.symbol.name == b_reloc.symbol.name
                        && t_reloc.relocation.addend == b_reloc.relocation.addend
                }
                _ => false,
            };

            let count = tr.arg_diff.len().min(br.arg_diff.len());
            let mut row_hard = false;
            let mut row_stack = false;
            let mut saw_diff = false;
            for i in 0..count {
                if tr.arg_diff[i].is_none() && br.arg_diff[i].is_none() {
                    continue;
                }
                saw_diff = true;
                let t_arg = t_parsed.args.get(i);
                let b_arg = b_parsed.args.get(i);
                if let (
                    Some(InstructionArg::Value(InstructionArgValue::Opaque(t_str))),
                    Some(InstructionArg::Value(InstructionArgValue::Opaque(b_str))),
                ) = (t_arg, b_arg)
                {
                    let mut pair = [t_str.to_string(), b_str.to_string()];
                    pair.sort();
                    let [a, b] = pair;
                    if seen_regswaps.insert((a, b)) {
                        out.regswap += 1;
                    }
                    continue;
                }
                if same_resolved_addr && numeric_arg(t_arg) && numeric_arg(b_arg) {
                    continue;
                }
                if references_r1 && (numeric_arg(t_arg) || numeric_arg(b_arg)) {
                    row_stack = true;
                } else {
                    row_hard = true;
                }
            }
            if row_hard {
                out.hard += 1;
            }
            if row_stack {
                out.stack += 1;
            }
            if !saw_diff && !row_hard && !row_stack {
                out.hard += 1;
            }
        }
    }
}

fn numeric_arg(arg: Option<&InstructionArg>) -> bool {
    matches!(
        arg,
        Some(InstructionArg::Value(
            InstructionArgValue::Signed(_) | InstructionArgValue::Unsigned(_)
        ))
    )
}

fn frame_size(
    obj: &Object,
    symbol_idx: usize,
    diff: &SymbolDiff,
    diff_config: &DiffObjConfig,
) -> Option<u64> {
    for row in &diff.instruction_rows {
        let Some(ins_ref) = row.ins_ref else { continue };
        let Some(resolved) = obj.resolve_instruction_ref(symbol_idx, ins_ref) else {
            continue;
        };
        let Ok(parsed) = obj.arch.process_instruction(resolved, diff_config) else {
            continue;
        };
        if !is_stwu_r1(&parsed) {
            continue;
        }
        for arg in &parsed.args {
            if let InstructionArg::Value(InstructionArgValue::Signed(v)) = arg
                && *v < 0
            {
                return Some((-v) as u64);
            }
        }
    }
    None
}

fn is_r1(arg: &InstructionArg) -> bool {
    matches!(
        arg,
        InstructionArg::Value(InstructionArgValue::Opaque(s)) if &**s == "r1"
    )
}

fn is_stwu_r1(parsed: &objdiff_core::obj::ParsedInstruction) -> bool {
    &*parsed.mnemonic == "stwu" && parsed.args.iter().any(is_r1)
}

fn is_stack_shift(
    t_parsed: &objdiff_core::obj::ParsedInstruction,
    b_parsed: &objdiff_core::obj::ParsedInstruction,
    tr: &InstructionDiffRow,
    br: &InstructionDiffRow,
    frame_diff: i64,
) -> bool {
    if t_parsed.mnemonic != b_parsed.mnemonic {
        return false;
    }
    if !t_parsed.args.iter().any(is_r1) {
        return false;
    }
    let count = tr
        .arg_diff
        .len()
        .min(br.arg_diff.len())
        .min(t_parsed.args.len())
        .min(b_parsed.args.len());
    let mut saw_shift = false;
    for i in 0..count {
        if tr.arg_diff[i].is_none() && br.arg_diff[i].is_none() {
            continue;
        }
        let (t, b) = match (t_parsed.args.get(i), b_parsed.args.get(i)) {
            (Some(a), Some(b)) => (a, b),
            _ => return false,
        };
        let (tv, bv) = match (t, b) {
            (
                InstructionArg::Value(InstructionArgValue::Signed(tv)),
                InstructionArg::Value(InstructionArgValue::Signed(bv)),
            ) => (*tv, *bv),
            (
                InstructionArg::Value(InstructionArgValue::Unsigned(tv)),
                InstructionArg::Value(InstructionArgValue::Unsigned(bv)),
            ) => (*tv as i64, *bv as i64),
            _ => return false,
        };
        if bv - tv != frame_diff {
            return false;
        }
        saw_shift = true;
    }
    saw_shift
}

/// FNV-1a over the function's code bytes. Deterministic across processes (so the
/// permuter's per-worker servers agree on novelty), unlike a randomized hasher.
fn fn_hash(obj: &Object, idx: usize) -> u64 {
    let sym = &obj.symbols[idx];
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    if let Some(sec_idx) = sym.section {
        if let Some(bytes) = obj.sections[sec_idx].data_range(sym.address, sym.size as usize) {
            for &b in bytes {
                h ^= b as u64;
                h = h.wrapping_mul(0x0000_0100_0000_01b3);
            }
        }
    }
    h
}
