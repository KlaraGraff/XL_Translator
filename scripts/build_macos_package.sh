#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DIST_DIR="$ROOT_DIR/dist"
SPEC_PATH="$ROOT_DIR/packaging/macos/XL_Translator_macOS.spec"
STAGING_DIR="$ROOT_DIR/.runtime/package/macos-dmg"

export MACOSX_DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-11.0}"

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

mkdir -p "$DIST_DIR"
export PYINSTALLER_CONFIG_DIR="$ROOT_DIR/.runtime/pyinstaller-config"
mkdir -p "$PYINSTALLER_CONFIG_DIR"

echo "[INFO] Verify build dependencies"
"$PYTHON" -c "import PyInstaller, PIL, webview; print('ok')"
"$PYTHON" -c "import numpy; print(f'numpy {numpy.__version__}: {numpy.__file__}')"

VERSION="$("$PYTHON" -c "import app_meta; print(app_meta.APP_VERSION)")"
if [[ -z "$VERSION" ]]; then
  echo "APP_VERSION could not be resolved." >&2
  exit 1
fi

echo "[INFO] Prepare macOS icon"
"$PYTHON" scripts/prepare_icons.py --macos

APP_PATH="$DIST_DIR/XL Translator.app"
COLLECT_PATH="$DIST_DIR/XL Translator"
DMG_NAME="XL_Translator_macOS_${VERSION}.dmg"
DMG_PATH="$DIST_DIR/$DMG_NAME"
CHECKSUM_PATH="$DMG_PATH.sha256"

rm -rf "$ROOT_DIR/build/XL_Translator_macOS" "$APP_PATH" "$COLLECT_PATH" "$STAGING_DIR"
rm -f "$DMG_PATH" "$CHECKSUM_PATH"

echo "[INFO] Build macOS app bundle"
"$PYTHON" -m PyInstaller --noconfirm "$SPEC_PATH"

if [[ ! -d "$APP_PATH" ]]; then
  echo "Expected app bundle was not produced: $APP_PATH" >&2
  exit 1
fi

if [[ "$(uname -s)" == "Darwin" ]]; then
  shopt -s nullglob
  numpy_extensions=( "$APP_PATH"/Contents/Frameworks/numpy/_core/_multiarray_umath.cpython-*-darwin.so )
  shopt -u nullglob

  if (( ${#numpy_extensions[@]} == 0 )); then
    echo "[WARN] NumPy extension was not found in the app bundle; skipping Monterey compatibility check."
  else
    for numpy_extension in "${numpy_extensions[@]}"; do
      if nm -u "$numpy_extension" 2>/dev/null | grep -q 'NEWLAPACK.*ILP64'; then
        echo "Incompatible NumPy wheel detected: $numpy_extension" >&2
        echo "The app would fail on macOS Monterey because it references NEWLAPACK ILP64 Accelerate symbols." >&2
        echo "Run scripts/install_macos_dependencies.sh before packaging to force a Monterey-compatible NumPy wheel." >&2
        exit 1
      fi
    done
    echo "[INFO] NumPy Monterey compatibility check passed"
  fi
fi

echo "[INFO] Build macOS dmg"
mkdir -p "$STAGING_DIR"
cp -R "$APP_PATH" "$STAGING_DIR/"
ln -s /Applications "$STAGING_DIR/Applications"
hdiutil create \
  -volname "XL Translator" \
  -srcfolder "$STAGING_DIR" \
  -ov \
  -format UDZO \
  "$DMG_PATH"

shasum -a 256 "$DMG_PATH" | sed "s|$DMG_PATH|$DMG_NAME|" > "$CHECKSUM_PATH"

if [[ -n "${GITHUB_ENV:-}" ]]; then
  {
    echo "MACOS_DMG=dist/$DMG_NAME"
    echo "MACOS_DMG_SHA256=dist/$DMG_NAME.sha256"
  } >> "$GITHUB_ENV"
fi

echo "[INFO] macOS dmg: $DMG_PATH"
echo "[INFO] SHA256: $CHECKSUM_PATH"
