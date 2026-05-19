#!/usr/bin/env bash
# Copy the .claude/ overlay (skills, hooks, settings.json) from this harness
# into a melee checkout. The harness is the source of truth; this is a plain
# copy, NOT a stow/symlink overlay — the tools/ scripts are run in place from
# ~/melee-harness (see README "Invoking the tools"), so only the .claude/
# bits need to physically live in the melee checkout for Claude Code to find
# them.
#
# Usage:
#   ./sync.sh                 # MELEE_ROOT or ~/melee
#   MELEE_ROOT=/path ./sync.sh
#
# Idempotent; re-run any time after editing skills/hooks/settings.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
melee="${MELEE_ROOT:-$HOME/melee}"

if [ ! -d "$melee" ]; then
    echo "error: melee checkout not found at '$melee'" >&2
    echo "       set MELEE_ROOT, e.g. MELEE_ROOT=/path/to/melee ./sync.sh" >&2
    exit 1
fi

mkdir -p "$melee/.claude"

echo "==> syncing .claude/skills"
rsync -a --delete "$here/.claude/skills/" "$melee/.claude/skills/"

echo "==> syncing .claude/hooks"
rsync -a --delete "$here/.claude/hooks/" "$melee/.claude/hooks/"

echo "==> syncing .claude/settings.json"
cp "$here/.claude/settings.json" "$melee/.claude/settings.json"

echo "done -> $melee/.claude"
