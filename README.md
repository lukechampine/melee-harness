# melee-harness

Personal decompilation harness for the [melee](https://github.com/doldecomp/melee)
decomp project. This repo is **not** part of the melee tree — it overlays onto a
melee checkout so that none of this tooling can leak into upstream PRs.

## Layout

```
tools/                 custom decomp scripts (overlay onto melee/tools/)
  decomp-permuter/     vendored decomp-permuter fork (setup below)
.claude/
  skills/              Claude Code skills (overlay onto melee/.claude/skills/)
  settings.json        project hooks (see "Hardcoded paths" below)
objdiff/               vendored objdiff-cli fork source (build instructions below)
mwcc_debug/            mwcc_debug DLL source: patches MWCC v1.2.5n to emit
                       IR-optimizer + PPC-backend listings to pcdump.txt
                       (build instructions below)
wibo/                  vendored wibo fork source:
                       fixes the formatoperands SIGBUS and the sjiswrap
                       nested-PE crash (build instructions below)
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
| `mwcc_dump.py` | compile a TU with the mwcc_debug compiler → `pcdump.txt` |
| `check_inline_vars.py` | hook: flags inlined-function patterns |
| `check_type_erasing_casts.py` | hook: flags type-erasing casts |

### .claude/skills/

`melee-decomp`, `easy-funcs`, `ground-decomp`, `item-decomp`, `decomp-progress`,
`mismatch-db`, `opseq`.

## Building the vendored tools

`./setup.sh` builds all three vendored tools and installs them into `./bin`
(gitignored) so the scripts resolve them locally without touching the system
`PATH`:

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

`<harness>` is located relative to the script (resolving symlinks), so this
works whether the tools run in place or are symlinked into a melee checkout.

## Setting up decomp-permuter

`tools/decomp-permuter/` is a vendored copy of a fork of
[decomp-permuter](https://github.com/jellejurre/decomp-permuter):

It uses [`uv`](https://docs.astral.sh/uv/); `uv.lock` is vendored, so:

```sh
cd tools/decomp-permuter
uv sync
```

Driven by `permute.py`.

## Building the mwcc_debug compiler + patched wibo

`mwcc_dump.py` compiles one melee TU with an instrumented MWCC and writes
`pcdump.txt` (IR-optimizer decisions + every PPC-backend pass, with symbol
names and `AFTER REGISTER COLORING` / `FINAL CODE`). The DLL and the patched
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
4. `<melee>/build/tools/wibo` — stock fallback (crashes; the script's
   Wine fallback covers it)

`<harness>` is tried both as the melee sibling (the script runs as the
melee `tools/` overlay copy) and relative to the script itself.

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

From the melee checkout, after both builds:

```sh
tools/mwcc_dump.py src/melee/it/items/itarwinglaser.c   # -> ./pcdump.txt
```

Defaults to the patched wibo with an automatic Wine fallback on SIGBUS
(`--runner wibo` / `--runner wine` to force one).
