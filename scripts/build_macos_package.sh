#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/.venv/bin/python3}"
DIST_DIR="$ROOT_DIR/dist"
APP_PATH="$ROOT_DIR/src-tauri/target/release/bundle/macos/Translator.app"
SIDECAR_PATH="$APP_PATH/Contents/Resources/sidecar/translator-sidecar/translator-sidecar"
STAGING_DIR="$ROOT_DIR/.runtime/package/macos-dmg"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python was not found: $PYTHON_BIN" >&2
  exit 1
fi
if [[ -n "${XL_TRANSLATOR_MACOS_NOTARY_PROFILE:-}" && -z "${XL_TRANSLATOR_MACOS_CODESIGN_IDENTITY:-}" ]]; then
  echo "Notarization requires XL_TRANSLATOR_MACOS_CODESIGN_IDENTITY." >&2
  exit 1
fi

cd "$ROOT_DIR"
VERSION="$($PYTHON_BIN -c 'import app_meta; print(app_meta.APP_VERSION)')"
DMG_NAME="Translator_macOS_${VERSION}.dmg"
DMG_PATH="$DIST_DIR/$DMG_NAME"

mkdir -p "$DIST_DIR"
"$PYTHON_BIN" scripts/build_tauri_package.py --platform macos --python "$PYTHON_BIN"

if [[ ! -d "$APP_PATH" || ! -x "$SIDECAR_PATH" ]]; then
  echo "Tauri app or bundled sidecar was not produced." >&2
  exit 1
fi

if [[ -n "${XL_TRANSLATOR_MACOS_CODESIGN_IDENTITY:-}" ]]; then
  codesign --force --options runtime --timestamp --sign "$XL_TRANSLATOR_MACOS_CODESIGN_IDENTITY" "$SIDECAR_PATH"
  codesign --force --deep --options runtime --timestamp --sign "$XL_TRANSLATOR_MACOS_CODESIGN_IDENTITY" "$APP_PATH"
  codesign --verify --deep --strict --verbose=2 "$APP_PATH"
else
  echo "[INFO] No Developer ID identity configured; producing an unsigned verification build."
fi

rm -rf "$STAGING_DIR"
rm -f "$DMG_PATH" "$DMG_PATH.sha256"
mkdir -p "$STAGING_DIR"
cp -R "$APP_PATH" "$STAGING_DIR/"
ln -s /Applications "$STAGING_DIR/Applications"
hdiutil create -volname Translator -srcfolder "$STAGING_DIR" -ov -format UDZO "$DMG_PATH"

if [[ -n "${XL_TRANSLATOR_MACOS_CODESIGN_IDENTITY:-}" ]]; then
  codesign --force --timestamp --sign "$XL_TRANSLATOR_MACOS_CODESIGN_IDENTITY" "$DMG_PATH"
fi
if [[ -n "${XL_TRANSLATOR_MACOS_NOTARY_PROFILE:-}" ]]; then
  xcrun notarytool submit "$DMG_PATH" --keychain-profile "$XL_TRANSLATOR_MACOS_NOTARY_PROFILE" --wait
  xcrun stapler staple "$DMG_PATH"
  xcrun stapler validate "$DMG_PATH"
fi

SIZE_MB="$(du -sm "$DMG_PATH" | awk '{print $1}')"
if [[ "$SIZE_MB" -gt 80 ]]; then
  echo "[ERROR] Installer is ${SIZE_MB}MB, exceeding the 80MB escalation threshold." >&2
  exit 2
fi
shasum -a 256 "$DMG_PATH" | sed "s|$DMG_PATH|$DMG_NAME|" > "$DMG_PATH.sha256"
if [[ -n "${GITHUB_ENV:-}" ]]; then
  {
    echo "MACOS_DMG=dist/$DMG_NAME"
    echo "MACOS_DMG_SHA256=dist/$DMG_NAME.sha256"
  } >> "$GITHUB_ENV"
fi
echo "[INFO] macOS dmg (${SIZE_MB}MB): $DMG_PATH"
