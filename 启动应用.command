#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec /bin/bash "$ROOT_DIR/scripts/start_tauri_macos.command"
