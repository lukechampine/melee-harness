use std::{
    io::{BufRead, Write},
    path::Path,
};

use anyhow::{Context, Result};
use argp::FromArgs;
use objdiff_core::{
    diff::{self, DiffObjConfig, DiffSide, FunctionRelocDiffs, MappingConfig},
    obj::{self, Object},
};

#[derive(FromArgs, PartialEq, Debug)]
/// Persistent scoring server for the source permuter. Parses the target object
/// once, then reads candidate object-file paths (one per line) on stdin and
/// writes each one's `diff_score <hash>` for a single function on stdout.
/// `diff_score` is objdiff's raw penalty (0 = perfect match); `hash` is a
/// deterministic fingerprint of the function's code bytes (for novelty/dedup).
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
    let mut diff_config =
        DiffObjConfig { function_reloc_diffs: FunctionRelocDiffs::DataValue, ..Default::default() };
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
            Ok((score, hash)) => writeln!(out, "{score} {hash:016x}")?,
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
) -> Result<(u64, u64)> {
    let data = std::fs::read(path).with_context(|| format!("read {path}"))?;
    let cand = obj::read::parse(&data, cfg, DiffSide::Base).context("parse candidate")?;
    let idx =
        cand.symbol_by_name(function).with_context(|| format!("symbol {function} not found"))?;
    let result = diff::diff_objs(Some(target), Some(&cand), None, cfg, mapping)?;
    let sym_diff = &result.right.as_ref().context("no base diff")?.symbols[idx];
    let score = sym_diff.diff_score.map_or(0, |(s, _)| s);
    Ok((score, fn_hash(&cand, idx)))
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
