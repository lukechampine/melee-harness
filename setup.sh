#!/usr/bin/env bash
# Build the vendored objdiff-cli fork and install it into ./bin so the tools
# resolve it locally without touching the system PATH (see tools/objdiff_path.py).
#
# Requires Rust 1.88+ (edition 2024). Cargo.lock is vendored, so the build is
# dependency-pinned.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Building objdiff-cli (release)..."
cargo build --release -p objdiff-cli --manifest-path "$here/objdiff/Cargo.toml"

mkdir -p "$here/bin"
cp "$here/objdiff/target/release/objdiff-cli" "$here/bin/objdiff-cli"

echo "Installed: $here/bin/objdiff-cli"
"$here/bin/objdiff-cli" --version
