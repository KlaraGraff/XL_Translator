#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
export TRANSLATOR_APP_DATA_DIR="${TRANSLATOR_APP_DATA_DIR:-$ROOT_DIR/.runtime/tauri-dev-app-data}"
cd "$ROOT_DIR/src-tauri"
exec "$ROOT_DIR/ui/node_modules/.bin/tauri" dev
