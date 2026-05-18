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
  settings.local.json.example   personal permission allowlist — copy to
                                .claude/settings.local.json per machine
objdiff/               vendored objdiff-cli fork source (build instructions below)
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
| `check_inline_vars.py` | hook: flags inlined-function patterns |
| `check_type_erasing_casts.py` | hook: flags type-erasing casts |

`generate-db.py` and `build_match_timeline.py` are intentionally **not** vendored
here.

### .claude/skills/

`melee-decomp`, `easy-funcs`, `ground-decomp`, `item-decomp`, `decomp-progress`,
`mismatch-db`, `opseq`.

## Building objdiff-cli

`objdiff/` is a vendored copy of a fork of
[encounter/objdiff](https://github.com/encounter/objdiff):

- fork remote: `git@github.com:lukechampine/objdiff`
- branch `unix` @ `04d6290` **plus uncommitted working-tree changes** to
  `objdiff-cli/src/cmd/diff.rs` (this is why the source is vendored as a copy,
  not referenced by SHA — the modifications were never pushed)
- local additions vs upstream: unix-style diffs, percent output, `-f stack`
  and `-f two-column` modes, `d=data` mark

Requirements: Rust **1.88+** (edition 2024). Then:

```sh
cd objdiff
cargo build --release -p objdiff-cli
# binary at objdiff/target/release/objdiff-cli
```

To put it on `PATH`:

```sh
cargo install --path objdiff/objdiff-cli
# installs to ~/.cargo/bin/objdiff-cli
```

`Cargo.lock` is committed, so the build is dependency-pinned.

## Setting up decomp-permuter

`tools/decomp-permuter/` is a vendored copy of a fork of
[decomp-permuter](https://github.com/simonlindholm/decomp-permuter):

- fork remote: `https://github.com/jellejurre/decomp-permuter`
- branch `melee` @ `f3c9261` **plus local working-tree modifications** to
  `src/` (`ast_util.py`, `candidate.py`, `main.py`, `perm/parse.py`,
  `permuter.py`, `randomizer.py`, `scorer.py`) and `permuter_settings.toml`
  (this is why it is vendored as a copy, not referenced by SHA — the
  modifications were never pushed)

It uses [`uv`](https://docs.astral.sh/uv/); `uv.lock` is vendored, so:

```sh
cd tools/decomp-permuter
uv sync
```

Driven by `permute.py` / `stack_permute.py` from the melee checkout.

## Known follow-ups (not done yet)

These are tracked deliberately so the overlay is honest about what isn't
portable yet:

1. **Hardcoded `/Users/luke/...` paths.** `.claude/settings.json` hooks invoke
   `python3 /Users/luke/melee/tools/check_*.py`, and some scripts (e.g.
   `checkdiff.py`) assume a fixed objdiff-cli location. These must be
   parameterized (env var / repo-relative discovery) before this runs on
   another machine.
2. **`tools/project.py` diff.** The melee build-config changes live as a diff
   against the upstream tracked file and are not captured here; handle as a
   patch applied during setup.
3. **Overlay wiring.** Symlink / stow these dirs into the melee checkout and add
   the paths to `melee/.git/info/exclude` so PRs stay clean.
