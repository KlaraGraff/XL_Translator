#!/bin/bash
set -u

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
RUNNER_SCRIPT="$ROOT_DIR/scripts/start_macos.command"
SILENT_LAUNCHER_SCRIPT="$ROOT_DIR/scripts/launch_silent_macos.sh"
BOOTSTRAP_MARKER="$ROOT_DIR/.venv/.bootstrap_success"
VENV_PYTHON3="$ROOT_DIR/.venv/bin/python3"
VENV_PYTHON="$ROOT_DIR/.venv/bin/python"

if [[ ! -f "$RUNNER_SCRIPT" ]]; then
  echo
  echo "[ERROR] Launcher script not found: $RUNNER_SCRIPT"
  exit 1
fi

if [[ ! -f "$BOOTSTRAP_MARKER" ]]; then
  exec /bin/bash "$RUNNER_SCRIPT"
fi

if [[ ! -x "$VENV_PYTHON3" && ! -x "$VENV_PYTHON" ]]; then
  exec /bin/bash "$RUNNER_SCRIPT"
fi

if [[ ! -f "$SILENT_LAUNCHER_SCRIPT" ]]; then
  echo
  echo "[WARN] Silent launcher helper is missing. Falling back to visible startup."
  exec /bin/bash "$RUNNER_SCRIPT"
fi

if ! /bin/bash "$SILENT_LAUNCHER_SCRIPT" "$ROOT_DIR"; then
  echo
  echo "[WARN] Silent startup failed. Falling back to visible startup."
  exec /bin/bash "$RUNNER_SCRIPT"
fi

exit 0
