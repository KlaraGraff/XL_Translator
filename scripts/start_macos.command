#!/bin/bash
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MIN_PYTHON_MAJOR=3
MIN_PYTHON_MINOR=10

is_supported_python() {
  local candidate="$1"

  [[ -x "$candidate" ]] || return 1

  "$candidate" -c "import sys; raise SystemExit(0 if sys.version_info >= (${MIN_PYTHON_MAJOR}, ${MIN_PYTHON_MINOR}) else 1)" >/dev/null 2>&1
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
    "$PROJECT_ROOT/.venv/bin/python3" \
    "$PROJECT_ROOT/.venv/bin/python" \
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

if ! PYTHON_BIN="$(find_bootstrap_python)"; then
  echo "[ERROR] Python 3.10+ was not found on this Mac."
  echo "[ERROR] Install a newer python3 first, or set PRODUCT_TRANSLATE_BOOTSTRAP_PYTHON."
  read -r -p "Press Enter to close..." _
  exit 1
fi

"$PYTHON_BIN" "$PROJECT_ROOT/scripts/launcher.py"
EXIT_CODE=$?

if [[ "$EXIT_CODE" -ne 0 ]]; then
  echo
  echo "[ERROR] Launch failed with exit code: $EXIT_CODE"
  read -r -p "Press Enter to close..." _
fi

exit "$EXIT_CODE"
