# melee-harness

Personal decompilation harness for the [melee](https://github.com/doldecomp/melee)
decomp project.

## Invoking the tools

Every script in this repo's `tools/` (`checkdiff.py`, `stack_permute.py`,
`permute.py`, `infer_struct.py`, `mwcc_dump.py`, `fix_includes.py`,
`gen_item_state_table.py`) is run in place with `MELEE_ROOT` pointing at the
melee checkout:

```sh
MELEE_ROOT=~/melee uv run --project ~/melee-harness ~/melee-harness/tools/checkdiff.py <fn>
```

The scripts resolve the melee checkout as `$MELEE_ROOT`, then
`$CLAUDE_PROJECT_DIR`, then (last resort) the script's parent dir — so set
`MELEE_ROOT` explicitly for any manual invocation.

## Layout

```
tools/                 custom decomp scripts (run in place via MELEE_ROOT)
sync.sh                copy .claude/ overlay into a melee checkout
permuter_settings.toml melee-specific decomp-permuter config; permute.py
                       passes it via --settings
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
decomp-permuter/       vendored decomp-permuter fork (setup below)
```

### tools/

| script | purpose |
|---|---|
| `checkdiff.py` | stack-frame autofix + rebuild + objdiff-cli diff for a function |
| `stack_permute.py` | stack-ordering permuter |
| `permute.py` | wrapper around the vendored decomp-permuter |
| `infer_struct.py` | struct field inference |
| `fix_includes.py` | include fixer |
| `gen_item_state_table.py` | item state-table generator |
| `mwcc_dump.py` | dump the mwcc_debug compiler's listing for one function → `pcdump.txt` |

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
| `objdiff-cli` | `objdiff/` (fork of [encounter/objdiff](https://github.com/encounter/objdiff): unix diffs, percent output, `-f stack`/`-f two-column`, `d=data`) | Rust **1.88+** (edition 2024); `Cargo.lock` pinned |
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

## Setting up decomp-permuter

`decomp-permuter/` is a vendored copy of a fork of
[decomp-permuter](https://github.com/jellejurre/decomp-permuter):

It uses [`uv`](https://docs.astral.sh/uv/); `uv.lock` is vendored, so:

```sh
cd decomp-permuter
uv sync
```

Driven by `permute.py`, which runs the permuter's `import.py` with
`--settings <harness>/permuter_settings.toml` (the melee-specific config —
`compiler_type`, `asm_pattern`, etc.). It lives at the harness root rather
than inside the vendored fork, so the fork stays a clean upstream copy and
no `permuter_settings.toml` is needed in the melee checkout.

## Building the mwcc_debug compiler + patched wibo

`mwcc_dump.py` takes a **function name**, resolves its TU via
`build/GALE01/report.json` (the same lookup `checkdiff.py` uses), compiles
that TU with an instrumented MWCC, then truncates `pcdump.txt` to just that
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

This writes a `pcdump.txt` into the melee checkout. If the function isn't in
`pcdump.txt` (inlined away, or a wrong name), the full dump is left in place and
the names that *are* present are listed.

Defaults to the patched wibo with an automatic Wine fallback on SIGBUS
(`--runner wibo` / `--runner wine` to force one).
