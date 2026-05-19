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

## Building objdiff-cli

`objdiff/` is a vendored copy of a fork of
[encounter/objdiff](https://github.com/encounter/objdiff):

- local additions vs upstream: unix-style diffs, percent output, `-f stack`
  and `-f two-column` modes, `d=data` mark

Requirements: Rust **1.88+** (edition 2024). Build and install it locally:

```sh
./setup.sh
```

This runs a release build and installs the binary to `./bin/objdiff-cli`.

The tools resolve the binary via `tools/objdiff_path.py`, in this order:

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
names and `AFTER REGISTER COLORING` / `FINAL CODE`). Two pieces must be built
first; both are macOS (Apple Silicon, via Rosetta) and vendored here as
source because the fixes live as uncommitted working-tree changes.

### 1. The mwcc_debug DLL + patched compiler

`mwcc_debug/` builds `MWDBG326.dll`, a replacement for the MWCC v1.2.5n
license-manager stub that flips on the compiler's dormant `debuglisting`
output and calls its own `formatoperands` to dump every basic block.

```sh
cd mwcc_debug
./build_macos.sh          # downloads a pinned Zig toolchain into tools/,
                          # emits lmgr326b.dll and MWDBG326.dll
# Patch a copy of the melee compiler so wibo loads the debug DLL
# (wibo shims LMGR326B.dll, so the import is renamed to MWDBG326.dll):
uv run patch_mwcceppc_for_wibo.py \
    <melee>/build/compilers/GC/1.2.5n/mwcceppc.exe \
    <melee>/build/compilers/GC/1.2.5n/mwcceppc_debug.exe
```

This leaves `mwcceppc_debug.exe` + `MWDBG326.dll` in the melee checkout's
`build/compilers/GC/1.2.5n/`. `mwcc_dump.py` invokes `mwcceppc_debug.exe`;
keep the unpatched `mwcceppc.exe` so the normal melee build is unaffected.

### 2. The patched wibo

`wibo/` is a vendored copy of a fork of
[decompals/wibo](https://github.com/decompals/wibo).

- `macros.S`: rewrites the `LJMP64` 32↔64-bit trampoline to build the far
  return on the stack instead of a shared writable `.data` slot — fixes the
  deterministic `formatoperands` SIGBUS on `@NNN` scratch temps
- `loader.cpp`/`main.cpp`/`modules.h`: relocate a nested PE off its
  preferred image base — fixes the `sjiswrap.exe → mwcceppc.exe` crash

Build it:

```sh
cd wibo
env -u VIRTUAL_ENV cmake --preset release-macos \
    -DPython3_EXECUTABLE="$(brew --prefix)/bin/python3"
env -u VIRTUAL_ENV cmake --build --preset release-macos
# -> wibo/build/release/wibo
```

`mwcc_dump.py` resolves the wibo binary in this order:

1. `$MWCC_WIBO` — explicit override
2. `<melee>/../melee-harness/wibo/build/release/wibo` — sibling layout (the
   normal case: the script runs as the melee overlay copy)
3. `<harness>/wibo/build/release/wibo` — run in place from the harness
4. `<melee>/build/tools/wibo` — stock fallback (crashes; the script's
   Wine fallback covers it)

### Usage

From the melee checkout, after both builds:

```sh
tools/mwcc_dump.py src/melee/it/items/itarwinglaser.c   # -> ./pcdump.txt
```

Defaults to the patched wibo with an automatic Wine fallback on SIGBUS
(`--runner wibo` / `--runner wine` to force one).
