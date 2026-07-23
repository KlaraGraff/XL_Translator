#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/.venv/bin/python3}"
MINIMUM_MACOS_VERSION="${MACOS_MINIMUM_SYSTEM_VERSION:-12.0}"
DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-12.0}"
MACOS_ARCH="${MACOS_ARCH:-$(uname -m)}"
DIST_DIR="$ROOT_DIR/dist"
APP_PATH="$ROOT_DIR/src-tauri/target/release/bundle/macos/Translator.app"
SIDECAR_PATH="$APP_PATH/Contents/Resources/sidecar/translator-sidecar/translator-sidecar"
STAGING_DIR="$ROOT_DIR/.runtime/package/macos-dmg"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python was not found: $PYTHON_BIN" >&2
  exit 1
fi
if [[ "$MINIMUM_MACOS_VERSION" != "12.0" ]]; then
  echo "macOS release builds must declare minimumSystemVersion 12.0; got $MINIMUM_MACOS_VERSION" >&2
  exit 1
fi
if [[ "$DEPLOYMENT_TARGET" != "12.0" ]]; then
  echo "macOS release builds require MACOSX_DEPLOYMENT_TARGET=12.0; got $DEPLOYMENT_TARGET" >&2
  exit 1
fi
export MACOSX_DEPLOYMENT_TARGET="$DEPLOYMENT_TARGET"
case "$MACOS_ARCH" in
  arm64)
    ARCH_LABEL="arm64"
    ;;
  x86_64|x64)
    MACOS_ARCH="x86_64"
    ARCH_LABEL="x64"
    ;;
  *)
    echo "Unsupported MACOS_ARCH: $MACOS_ARCH (expected arm64 or x86_64)" >&2
    exit 1
    ;;
esac
"$PYTHON_BIN" - <<'PY'
import sys

if sys.version_info[:2] != (3, 11):
    raise SystemExit(
        "macOS release builds require Python 3.11; "
        f"got {sys.version.split()[0]} from {sys.executable}"
    )
PY
if [[ -n "${XL_TRANSLATOR_MACOS_NOTARY_PROFILE:-}" && -z "${XL_TRANSLATOR_MACOS_CODESIGN_IDENTITY:-}" ]]; then
  echo "Notarization requires XL_TRANSLATOR_MACOS_CODESIGN_IDENTITY." >&2
  exit 1
fi

cd "$ROOT_DIR"
VERSION="$($PYTHON_BIN -c 'import app_meta; print(app_meta.APP_VERSION)')"
DMG_NAME="Translator_macOS_${ARCH_LABEL}_${VERSION}.dmg"
DMG_PATH="$DIST_DIR/$DMG_NAME"

mkdir -p "$DIST_DIR"
"$PYTHON_BIN" scripts/build_tauri_package.py --platform macos --python "$PYTHON_BIN"

if [[ ! -d "$APP_PATH" || ! -x "$SIDECAR_PATH" ]]; then
  echo "Tauri app or bundled sidecar was not produced." >&2
  exit 1
fi

"$PYTHON_BIN" scripts/verify_macos_minimum_version.py \
  "$APP_PATH" \
  --declared "$MINIMUM_MACOS_VERSION" \
  --architecture "$MACOS_ARCH"

bash "$ROOT_DIR/scripts/sign_macos_app.sh" "$APP_PATH" "$SIDECAR_PATH"

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
