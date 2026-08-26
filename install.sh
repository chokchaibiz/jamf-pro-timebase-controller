#!/usr/bin/env bash
set -euo pipefail

# Backward-compatible entry point. The maintained installer lives in
# install-program.sh so its purpose is unambiguous in release bundles.
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$SRC_DIR/install-program.sh" "$@"
