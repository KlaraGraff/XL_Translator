#!/bin/bash
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_PYTHON3="$PROJECT_ROOT/.venv/bin/python3"
VENV_PYTHON="$PROJECT_ROOT/.venv/bin/python"

find_project_python() {
  if [[ -x "$VENV_PYTHON3" ]]; then
    echo "$VENV_PYTHON3"
    return 0
  fi
  if [[ -x "$VENV_PYTHON" ]]; then
    echo "$VENV_PYTHON"
    return 0
  fi
  return 1
}

if ! PYTHON_BIN="$(find_project_python)"; then
  echo "[INFO] Project virtual environment is not ready. Running standard bootstrap first..."
  /bin/bash "$PROJECT_ROOT/scripts/start_macos.command"
  if ! PYTHON_BIN="$(find_project_python)"; then
    echo
    echo "[ERROR] Project virtual environment is still unavailable."
    read -r -p "Press Enter to close..." _
    exit 1
  fi
fi

if ! "$PYTHON_BIN" -c "import PySide6" >/dev/null 2>&1; then
  echo "[INFO] Installing native UI dependency: PySide6-Essentials"
  "$PYTHON_BIN" -m pip install -r "$PROJECT_ROOT/requirements.txt"
  if [[ "$?" -ne 0 ]]; then
    echo
    echo "[ERROR] Failed to install native UI dependencies."
    read -r -p "Press Enter to close..." _
    exit 1
  fi
fi

cd "$PROJECT_ROOT" || exit 1
"$PYTHON_BIN" "$PROJECT_ROOT/scripts/launch_native.py"
EXIT_CODE=$?

if [[ "$EXIT_CODE" -ne 0 ]]; then
  echo
  echo "[ERROR] Native launch failed with exit code: $EXIT_CODE"
  read -r -p "Press Enter to close..." _
fi

exit "$EXIT_CODE"

