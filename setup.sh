#!/usr/bin/env bash
# Build the vendored tools and install them into ./bin so the harness scripts
# resolve them locally without touching the system PATH:
#
#   bin/objdiff-cli    objdiff-cli fork      (see tools/objdiff_path.py)
#   bin/wibo           patched wibo fork     (see tools/mwcc_dump.py)
#   bin/MWDBG326.dll   mwcc_debug listing DLL (+ bin/lmgr326b.dll)
#
# Requirements (macOS / Apple Silicon, Rosetta for wibo):
#   - Rust 1.88+ (edition 2024); objdiff Cargo.lock is vendored/pinned
#   - CMake, and a non-venv Python >=3.10 for the wibo trampoline generator
#     (it self-bootstraps a `clang` venv, so it must not run inside the
#     melee .venv)
#   - build_macos.sh downloads a pinned Zig toolchain for the DLL
#
# bin/ is gitignored. Re-run any time; all three builds are incremental.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bin="$here/bin"
mkdir -p "$bin"

# Pick a Python >=3.10 that is NOT a virtualenv (Homebrew first). The wibo
# trampoline generator bootstraps its own `clang` venv only when it isn't
# already running inside one, so the melee .venv must be avoided.
pick_python() {
    local brew_py cand p ok
    if command -v brew >/dev/null 2>&1; then
        brew_py="$(brew --prefix 2>/dev/null)/bin/python3"
        [ -x "$brew_py" ] && { echo "$brew_py"; return 0; }
    fi
    for cand in python3.13 python3.12 python3.11 python3.10 python3; do
        p="$(command -v "$cand" 2>/dev/null)" || continue
        case "$p" in *"/.venv/"*|*"/venv/"*) continue ;; esac
        ok="$("$p" -c 'import sys;print(sys.version_info[:2]>=(3,10))' 2>/dev/null)" || continue
        [ "$ok" = "True" ] && { echo "$p"; return 0; }
    done
    return 1
}

# --- objdiff-cli ------------------------------------------------------------
echo "==> Building objdiff-cli (release)..."
cargo build --release -p objdiff-cli --manifest-path "$here/objdiff/Cargo.toml"
cp "$here/objdiff/target/release/objdiff-cli" "$bin/objdiff-cli"

# --- patched wibo -----------------------------------------------------------
echo "==> Building patched wibo (release-macos)..."
if ! py="$(pick_python)"; then
    echo "error: no non-venv Python >=3.10 found for the wibo build" >&2
    exit 1
fi
echo "    using Python: $py"
# Unset VIRTUAL_ENV/PYTHONHOME so gen_trampolines.py bootstraps its clang venv.
# Run from the wibo dir so --preset finds CMakePresets.json.
(
    cd "$here/wibo"
    env -u VIRTUAL_ENV -u PYTHONHOME cmake --preset release-macos \
        -DPython3_EXECUTABLE="$py"
    env -u VIRTUAL_ENV -u PYTHONHOME cmake --build --preset release-macos
)
cp "$here/wibo/build/release/wibo" "$bin/wibo"

# --- mwcc_debug listing DLL -------------------------------------------------
echo "==> Building mwcc_debug DLL..."
( cd "$here/mwcc_debug" && ./build_macos.sh )
cp "$here/mwcc_debug/MWDBG326.dll" "$bin/MWDBG326.dll"
cp "$here/mwcc_debug/lmgr326b.dll" "$bin/lmgr326b.dll"

# --- summary ----------------------------------------------------------------
echo
echo "Installed into $bin :"
"$bin/objdiff-cli" --version
"$bin/wibo" --version 2>&1 | head -1
ls -1 "$bin/MWDBG326.dll" "$bin/lmgr326b.dll"
echo
echo "Next (per melee checkout): patch the compiler so wibo loads the DLL:"
echo "  python3 mwcc_debug/patch_mwcceppc_for_wibo.py \\"
echo "      <melee>/build/compilers/GC/1.2.5n/mwcceppc.exe \\"
echo "      <melee>/build/compilers/GC/1.2.5n/mwcceppc_debug.exe \\"
echo "      --dll $bin/MWDBG326.dll"
