#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
export TRANSLATOR_APP_DATA_DIR="${TRANSLATOR_APP_DATA_DIR:-$ROOT_DIR/.runtime/tauri-dev-app-data}"
# Refresh the isolated manual-test configuration from the installed app.
"$ROOT_DIR/.venv/bin/python3" "$ROOT_DIR/agent/testing/seed_user_config.py" \
  --target "$TRANSLATOR_APP_DATA_DIR"
cd "$ROOT_DIR/src-tauri"
exec "$ROOT_DIR/ui/node_modules/.bin/tauri" dev
