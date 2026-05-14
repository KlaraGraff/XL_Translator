#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

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

export MACOSX_DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-11.0}"

NUMPY_VERSION="${XL_TRANSLATOR_NUMPY_VERSION:-2.4.4}"
WHEELHOUSE="${XL_TRANSLATOR_WHEELHOUSE:-$ROOT_DIR/.runtime/wheelhouse/macos}"

echo "[INFO] Install macOS build dependencies"
"$PYTHON" -m pip install --upgrade pip
"$PYTHON" -m pip install -r requirements-build.txt

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "[INFO] NumPy wheel override is only needed for macOS builds."
  exit 0
fi

case "$(uname -m)" in
  arm64)
    DEFAULT_MACOS_PLATFORM="macosx_11_0_arm64"
    ;;
  x86_64)
    DEFAULT_MACOS_PLATFORM="macosx_10_9_x86_64"
    ;;
  *)
    echo "[INFO] Unsupported macOS architecture for NumPy wheel override: $(uname -m)"
    exit 0
    ;;
esac

PY_VERSION="$("$PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
PY_ABI="$("$PYTHON" -c 'import sys; print(f"cp{sys.version_info.major}{sys.version_info.minor}")')"
MACOS_PLATFORM="${XL_TRANSLATOR_MACOS_PLATFORM:-${DEFAULT_MACOS_PLATFORM}}"
NUMPY_WHEEL_PATTERN="numpy-${NUMPY_VERSION}-${PY_ABI}-${PY_ABI}-${MACOS_PLATFORM}*.whl"

mkdir -p "$WHEELHOUSE"
rm -f "$WHEELHOUSE"/numpy-*.whl

echo "[INFO] Download NumPy $NUMPY_VERSION wheel for $MACOS_PLATFORM"
"$PYTHON" -m pip download \
  --only-binary=:all: \
  --no-deps \
  --dest "$WHEELHOUSE" \
  --platform "$MACOS_PLATFORM" \
  --implementation cp \
  --python-version "$PY_VERSION" \
  --abi "$PY_ABI" \
  "numpy==$NUMPY_VERSION"

shopt -s nullglob
numpy_wheels=( "$WHEELHOUSE"/$NUMPY_WHEEL_PATTERN )
shopt -u nullglob

if (( ${#numpy_wheels[@]} != 1 )); then
  echo "Expected exactly one compatible NumPy wheel matching $NUMPY_WHEEL_PATTERN." >&2
  printf 'Found wheels:\n' >&2
  find "$WHEELHOUSE" -maxdepth 1 -name 'numpy-*.whl' -print >&2
  exit 1
fi

echo "[INFO] Force install compatible NumPy wheel: ${numpy_wheels[0]}"
"$PYTHON" -m pip install --force-reinstall --no-deps "${numpy_wheels[0]}"

"$PYTHON" - <<'PY'
import numpy
import platform

print(f"[INFO] NumPy ready: version={numpy.__version__}, arch={platform.machine()}")
PY
