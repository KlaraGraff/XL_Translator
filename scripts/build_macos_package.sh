#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DIST_DIR="$ROOT_DIR/dist"
SPEC_PATH="$ROOT_DIR/packaging/macos/XL_Translator_macOS.spec"
STAGING_DIR="$ROOT_DIR/.runtime/package/macos-dmg"

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
mkdir -p "$DIST_DIR"
export PYINSTALLER_CONFIG_DIR="$ROOT_DIR/.runtime/pyinstaller-config"
mkdir -p "$PYINSTALLER_CONFIG_DIR"

echo "[INFO] Verify build dependencies"
"$PYTHON" -c "import PyInstaller, PIL; print('ok')"

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
