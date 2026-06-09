# melee-harness

Personal decompilation harness for the [melee](https://github.com/doldecomp/melee)
decomp project.

## Invoking the tools

Every script in this repo's `tools/` (`decomp.py`, `checkdiff.py`,
`permute.py`, `infer_struct.py`, `mwcc_dump.py`, `mwcc_diagnose.py`, `fix_includes.py`,
`gen_item_state_table.py`) is run in place against a melee checkout:

```sh
# from anywhere inside the checkout, MELEE_ROOT is auto-detected:
cd ~/melee && uv run ~/melee-harness/tools/checkdiff.py <fn>

# or point at it explicitly from anywhere:
MELEE_ROOT=~/melee uv run ~/melee-harness/tools/checkdiff.py <fn>
```

The scripts resolve the melee checkout (via `tools/melee_root.py`) as
`$MELEE_ROOT`, then `$CLAUDE_PROJECT_DIR`, then by walking up from the current
directory for a `build/GALE01` marker — so when you run from inside a checkout
you can omit `MELEE_ROOT` entirely. The resolved root is always made absolute.

## Layout

```
tools/                 custom decomp scripts (run in place via MELEE_ROOT)
sync.sh                copy .claude/ overlay into a melee checkout
.claude/
  skills/              Claude Code skills (copied into melee via ./sync.sh)
  hooks/               PostToolUse hook scripts, co-located with settings.json
  settings.json        project hooks (reference $CLAUDE_PROJECT_DIR/.claude/hooks/)
objdiff/               vendored objdiff-cli fork source (build instructions below)
mwcc_debug/            mwcc_debug DLL source: patches MWCC v1.2.5n to emit
                       IR-optimizer + PPC-backend listings to pcdump.txt
                       (build instructions below)
wibo/                  vendored wibo fork source:
                       fixes the formatoperands SIGBUS and the sjiswrap
                       nested-PE crash (build instructions below)
m2c/                   vendored m2c fork (the decompiler tools/decomp.py
                       drives; adds --void-field-type / --void-var-type;
                       no setup — see below)
```

### tools/

| script | purpose |
|---|---|
| `decomp.py` | run the vendored m2c fork on a function/TU (vendored from the melee tree; m2c wired via PYTHONPATH) |
| `checkdiff.py` | fix includes + rebuild + objdiff-cli diff for a function |
| `permute.py` | source-level permuter: mutates the **real** TU via tree-sitter byte-span edits, compiles it with the exact `build.ninja` mwcc command, and scores each candidate with **objdiff itself** (a persistent `objdiff-cli score` server, objdiff-core). Candidates are ranked by mismatch class first — hard structural/instruction-selection rows, then deduped regswaps, then stack/frame rows, with objdiff's raw reloc/data-aware penalty as the final tie-breaker — and a raw score of 0 is a *true* 100% match. Findings are real diffs that `git apply` to `src/`; by default (`--apply=match`) it writes a 100% match straight back to the source and stops as soon as it finds one. Improved candidates are narrowed after the search by attempting to revert nonessential diff chunks while preserving the best score (`--no-narrow` to skip, `--narrow-passes` to tune). Best candidates report their mutation provenance; `--save-replay` writes a hash-checked JSON edit recipe, and `--replay` reconstructs/scores/applies that recipe later. Precompiles the TU's header block once (mwcc PCH) and recompiles only the mutated body per candidate (auto fidelity-gated, `--no-pch` to disable), and compiles K candidates per mwcc invocation (`--batch`, default 16) to amortize process startup with per-candidate salvage on error. `--profile` prints a per-phase timing breakdown. |
| `src_mutate.py` | tree-sitter mutation engine backing `permute.py` (reorder decls/stmts/params, commutative/add-sub/struct-ref/condition/no-op branch rewrites, scoped MWCC pragmas, volatile decls, helper extraction/manual inlining, pad var, **temp_for_expr** = extract a subexpression into a typed temporary, …); runnable standalone to preview one mutation as a diff |
| `type_oracle.py` | clang (libclang) type oracle backing `temp_for_expr`: one parse of the base TU (using `compile_commands.json` flags, so macros resolve) maps each expression's source span to its type, so the permuter can write `T tmp = expr;`. Built once per run (~100ms), then ~free per candidate |
| `ninja_compile.py` | compile one TU with its `build.ninja` mwcc command (no Ninja), incl. precompiled-header build (`build_pch`) + `-prefix` reuse; shared by `checkdiff.py` and `permute.py` |
| `infer_struct.py` | struct field inference |
| `fix_includes.py` | include fixer |
| `gen_item_state_table.py` | item state-table generator |
| `mwcc_dump.py` | dump the mwcc_debug compiler's listing for one function → `pcdump.txt` |
| `mwcc_diagnose.py` | mode-oriented mismatch diagnostics that combine checkdiff/objdiff with mwcc_dump |
| `find_stale_nonmatching_tus.py` | list `Object(NonMatching, ...)` TUs whose reported functions are all 100% matched, so `configure.py` can be flipped |

### .claude/hooks/

PostToolUse hook scripts, kept next to `settings.json` so the `.claude/`
overlay is self-contained (no dependency on `tools/`). `settings.json`
invokes them as `uv run "$CLAUDE_PROJECT_DIR/.claude/hooks/<script>"`.

| script | purpose |
|---|---|
| `check_inline_vars.py` | flags inlined-function patterns in the edited function |
| `check_type_erasing_casts.py` | flags type-erasing casts / m2c residue in an edit |

### .claude/skills/

`melee-decomp`, `ground-decomp`, `item-decomp`, `decomp-progress`, `opseq`.

### sync.sh

Copies the `.claude/` overlay (skills, hooks, `settings.json`) from this repo
into a melee checkout.

```sh
MELEE_ROOT=~/melee ./sync.sh   # defaults to ~/melee if MELEE_ROOT unset
```

## Building the vendored tools

`./setup.sh` builds all three vendored tools and installs them into `./bin` so
the scripts resolve them locally without touching the system `PATH`:

| `bin/` artifact | source | needs |
|---|---|---|
| `objdiff-cli` | `objdiff/` (fork of [encounter/objdiff](https://github.com/encounter/objdiff): unix diffs, percent output, `-f stack`/`-f two-column`, `d=data`, and a `score` server subcommand `permute.py` uses to score candidates) | Rust **1.88+** (edition 2024); `Cargo.lock` pinned |
| `wibo` | `wibo/` (patched fork — see below) | CMake; a non-venv Python ≥3.10 |
| `MWDBG326.dll` (+ `lmgr326b.dll`) | `mwcc_debug/` (see below) | downloads a pinned Zig toolchain |

```sh
./setup.sh
```

Re-run any time; all three builds are incremental. The per-melee compiler
patch (below) is a separate step `setup.sh` prints at the end.

`objdiff-cli` is resolved via `tools/objdiff_path.py`, in this order:

1. `$OBJDIFF_CLI` — explicit override
2. `<harness>/bin/objdiff-cli` — what `./setup.sh` installs
3. `<harness>/objdiff/target/release/objdiff-cli` — raw cargo output
4. `objdiff-cli` on `PATH` — last-resort fallback

`<harness>` is located relative to the script, so this works wherever the
tools run in place from.

## Setting up m2c

`m2c/` is a vendored copy of a fork of
[m2c](https://github.com/matt-kempster/m2c) (fork:
[lukechampine/m2c](https://github.com/lukechampine/m2c), branch `vibing`,
four commits past upstream `f201e88`). The custom commits add the
`--void-var-type` and `--void-field-type` flags and single-expression
struct copying — the `--void-field-type` / `--void-var-type` invocations
the `melee-decomp` / `ground-decomp` / `item-decomp` skills rely on come
from this fork, so stock upstream m2c is **not** a substitute.

**There is no setup step.** m2c is driven by `tools/decomp.py`, which is
also vendored here (it was the melee tree's `tools/decomp.py`, rewired for
the harness). It runs in place like the other scripts:

```sh
MELEE_ROOT=~/melee uv run --project ~/melee-harness \
    ~/melee-harness/tools/decomp.py <function|tu> [m2c args...]
```

`decomp.py` invokes m2c with `<harness>/m2c` prepended to `PYTHONPATH`, so
`-m m2c.main` (and its bundled `m2c_pycparser`) always resolve to the
vendored fork — no install, and no dependency on the melee `.venv`. Its
two third-party deps are declared via PEP 723 inline metadata so `uv run`
provisions them automatically: `pyelftools` (function → obj/asm lookup)
and `pcpp` (the melee tree's `tools/m2ctx/m2ctx.py --preprocessor`, which
`decomp.py` shells out to for `build/ctx.c`). `m2ctx` stays in the melee
tree — it is pure stdlib and self-locating; `decomp.py` runs it with
`cwd` pinned to the melee root so pcpp's relative include dirs resolve.

Pulling harness changes that touch `m2c/` takes effect immediately; there
is nothing to reinstall. Verify the vendored fork is what runs with:

```sh
PYTHONPATH=<harness>/m2c uv run --project <harness> \
    python -c "import m2c; print(m2c.__file__)"
# -> <harness>/m2c/m2c/__init__.py
```

## Building the mwcc_debug compiler + patched wibo

`mwcc_dump.py` takes a **function name**, resolves its TU via
`build/GALE01/report.json` (the same lookup `checkdiff.py` uses), compiles
that TU with an instrumented MWCC from a unique working directory under
`build/mwcc-dump/`, then truncates that run's `pcdump.txt` to just that
function's section (IR-optimizer decisions + every PPC-backend pass, with
symbol names and `AFTER REGISTER COLORING` / `FINAL CODE`) so the output
concerns only that function. The DLL and the patched
wibo are built by `./setup.sh` (above) into `bin/`; both are macOS (Apple
Silicon, via Rosetta) and vendored as source because the fixes live as
uncommitted working-tree changes.

### The mwcc_debug DLL

`mwcc_debug/` (built via `build_macos.sh`) produces `MWDBG326.dll`, a
replacement for the MWCC v1.2.5n license-manager stub that flips on the
compiler's dormant `debuglisting` output and calls its own `formatoperands`
to dump every basic block.

### The patched wibo

`wibo/` is a vendored copy of a fork of
[decompals/wibo](https://github.com/decompals/wibo):

- `macros.S`: rewrites the `LJMP64` 32↔64-bit trampoline to build the far
  return on the stack instead of a shared writable `.data` slot — fixes the
  deterministic `formatoperands` SIGBUS on `@NNN` scratch temps
- `loader.cpp`/`main.cpp`/`modules.h`: relocate a nested PE off its
  preferred image base — fixes the `sjiswrap.exe → mwcceppc.exe` crash

`mwcc_dump.py` resolves the wibo binary in this order:

1. `$MWCC_WIBO` — explicit override
2. `<harness>/bin/wibo` — what `./setup.sh` installs
3. `<harness>/wibo/build/release/wibo` — raw cmake output
4. `<melee>/build/tools/wibo` — stock fallback

`<harness>` is located relative to the script itself.

### Patch the compiler (per melee checkout)

`./setup.sh` cannot touch the melee tree, so after it runs, point the debug
DLL at a copy of the melee compiler (wibo shims `LMGR326B.dll`, so the
import is renamed to `MWDBG326.dll`):

```sh
uv run mwcc_debug/patch_mwcceppc_for_wibo.py \
    <melee>/build/compilers/GC/1.2.5n/mwcceppc.exe \
    <melee>/build/compilers/GC/1.2.5n/mwcceppc_debug.exe \
    --dll bin/MWDBG326.dll
```

`mwcc_dump.py` invokes `mwcceppc_debug.exe`; the unpatched `mwcceppc.exe`
stays in place so the normal melee build is unaffected.

### Usage

```sh
MELEE_ROOT=~/melee uv run --project ~/melee-harness ~/melee-harness/tools/mwcc_dump.py it_802E70BC
```

This writes a unique `build/mwcc-dump/<function>-*/pcdump.txt` in the melee
checkout and prints the exact path. If the function isn't in `pcdump.txt`
(inlined away, or a wrong name), the full dump is left in place and the names
that *are* present are listed.

Defaults to the patched wibo with an automatic Wine fallback on SIGBUS
(`--runner wibo` / `--runner wine` to force one).

For stack-heavy objdiff summaries, run the first diagnostic mode:

```sh
MELEE_ROOT=~/melee uv run --project ~/melee-harness ~/melee-harness/tools/mwcc_diagnose.py stack it_8026CD50
```

`mwcc_diagnose.py stack` runs checkdiff's temporary compile/diff path, lists
the target/current `r1` offset deltas, then adds
mwcc_dump's current-C frame and stack-slot summary with guidance for likely
local ordering, aggregate, padding, or hidden-temp causes.
