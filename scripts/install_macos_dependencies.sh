#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CONSTRAINTS_PATH="$ROOT_DIR/constraints-release-py311.txt"

resolve_python() {
  if [[ -n "${PYTHON_BIN:-}" && -x "${PYTHON_BIN:-}" ]]; then
    echo "$PYTHON_BIN"
    return 0
  fi

  for candidate in \
    "$ROOT_DIR/.venv/bin/python3" \
    "$ROOT_DIR/.venv/bin/python" \
    "$(command -v python3 2>/dev/null || true)" \
    "$(command -v python 2>/dev/null || true)"
  do
    if [[ -n "$candidate" && -x "$candidate" ]]; then
      echo "$candidate"
      return 0
    fi
  done

  echo "Python was not found. Install Python 3.11 or set PYTHON_BIN." >&2
  return 1
}

PYTHON="$(resolve_python)"
cd "$ROOT_DIR"

if [[ "${XL_TRANSLATOR_ALLOW_UNSUPPORTED_PYTHON:-}" != "1" ]]; then
  "$PYTHON" - <<'PY'
import sys

if sys.version_info[:2] != (3, 11):
    raise SystemExit(
        "Python 3.11 is required for release builds; "
        f"got {sys.version.split()[0]} from {sys.executable}. "
        "Set PYTHON_BIN to a Python 3.11 interpreter."
    )
PY
fi

echo "[INFO] Install macOS build dependencies"
"$PYTHON" -m pip install \
  --upgrade \
  --constraint "$CONSTRAINTS_PATH" \
  pip
"$PYTHON" -m pip install \
  --constraint "$CONSTRAINTS_PATH" \
  --requirement requirements-build.txt
"$PYTHON" scripts/verify_release_dependencies.py \
  --constraints "$CONSTRAINTS_PATH"
