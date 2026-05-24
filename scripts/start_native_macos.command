#!/bin/bash
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_PYTHON3="$PROJECT_ROOT/.venv/bin/python3"
VENV_PYTHON="$PROJECT_ROOT/.venv/bin/python"
BOOTSTRAP_MARKER="$PROJECT_ROOT/.venv/.bootstrap_success"
MIN_PYTHON_MAJOR=3
MIN_PYTHON_MINOR=10

is_supported_python() {
  local candidate="$1"

  [[ -x "$candidate" ]] || return 1
  "$candidate" -c "import sys; raise SystemExit(0 if sys.version_info >= (${MIN_PYTHON_MAJOR}, ${MIN_PYTHON_MINOR}) else 1)" >/dev/null 2>&1
}

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

find_bootstrap_python() {
  local candidate=""

  if [[ -n "${PRODUCT_TRANSLATE_BOOTSTRAP_PYTHON:-}" ]] && is_supported_python "${PRODUCT_TRANSLATE_BOOTSTRAP_PYTHON}"; then
    echo "${PRODUCT_TRANSLATE_BOOTSTRAP_PYTHON}"
    return 0
  fi

  for candidate in \
    /opt/homebrew/bin/python3 \
    /opt/homebrew/bin/python3.13 \
    /opt/homebrew/bin/python3.12 \
    /opt/homebrew/bin/python3.11 \
    /opt/homebrew/bin/python3.10 \
    /usr/local/bin/python3 \
    /usr/local/bin/python3.13 \
    /usr/local/bin/python3.12 \
    /usr/local/bin/python3.11 \
    /usr/local/bin/python3.10 \
    "$(command -v python3 2>/dev/null || true)" \
    "$(command -v python3.13 2>/dev/null || true)" \
    "$(command -v python3.12 2>/dev/null || true)" \
    "$(command -v python3.11 2>/dev/null || true)" \
    "$(command -v python3.10 2>/dev/null || true)" \
    /usr/bin/python3
  do
    if [[ -n "$candidate" ]] && is_supported_python "$candidate"; then
      echo "$candidate"
      return 0
    fi
  done

  return 1
}

ensure_project_venv() {
  local bootstrap_python=""

  if ! bootstrap_python="$(find_bootstrap_python)"; then
    echo "[ERROR] Python 3.10+ was not found on this Mac."
    echo "[ERROR] Install a newer python3 first, or set PRODUCT_TRANSLATE_BOOTSTRAP_PYTHON."
    read -r -p "Press Enter to close..." _
    exit 1
  fi

  echo "[INFO] Creating project virtual environment with: $bootstrap_python"
  "$bootstrap_python" -m venv "$PROJECT_ROOT/.venv"
  if [[ "$?" -ne 0 ]]; then
    echo
    echo "[ERROR] Failed to create project virtual environment."
    read -r -p "Press Enter to close..." _
    exit 1
  fi
}

if ! PYTHON_BIN="$(find_project_python)"; then
  echo "[INFO] Project virtual environment is not ready. Creating it for the native app..."
  ensure_project_venv
  if ! PYTHON_BIN="$(find_project_python)"; then
    echo
    echo "[ERROR] Project virtual environment is still unavailable."
    read -r -p "Press Enter to close..." _
    exit 1
  fi
fi

if [[ ! -f "$BOOTSTRAP_MARKER" ]] || ! "$PYTHON_BIN" -c "import PySide6, anthropic, dashscope, docx, dotenv, httpx, loguru, openai, openpyxl, pandas, psutil, pydantic, rich, tenacity, xlrd, zhipuai" >/dev/null 2>&1; then
  echo "[INFO] Installing native app dependencies"
  "$PYTHON_BIN" -m pip install -r "$PROJECT_ROOT/requirements.txt"
  if [[ "$?" -ne 0 ]]; then
    echo
    echo "[ERROR] Failed to install native UI dependencies."
    read -r -p "Press Enter to close..." _
    exit 1
  fi
fi

mkdir -p "$(dirname "$BOOTSTRAP_MARKER")"
touch "$BOOTSTRAP_MARKER"

cd "$PROJECT_ROOT" || exit 1
"$PYTHON_BIN" "$PROJECT_ROOT/scripts/launch_native.py"
EXIT_CODE=$?

if [[ "$EXIT_CODE" -ne 0 ]]; then
  echo
  echo "[ERROR] Native launch failed with exit code: $EXIT_CODE"
  read -r -p "Press Enter to close..." _
fi

exit "$EXIT_CODE"
