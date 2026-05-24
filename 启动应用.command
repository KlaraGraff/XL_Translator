#!/bin/bash
set -u

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
RUNNER_SCRIPT="$ROOT_DIR/scripts/start_native_macos.command"

if [[ ! -f "$RUNNER_SCRIPT" ]]; then
  echo
  echo "[ERROR] Launcher script not found: $RUNNER_SCRIPT"
  exit 1
fi

exec /bin/bash "$RUNNER_SCRIPT"
