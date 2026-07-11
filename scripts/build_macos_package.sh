#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DIST_DIR="$ROOT_DIR/dist"
SPEC_PATH="$ROOT_DIR/packaging/macos/app_macos.spec"
STAGING_DIR="$ROOT_DIR/.runtime/package/macos-dmg"
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

DEFAULT_MACOS_MINIMUM_SYSTEM_VERSION="$(
  "$PYTHON" -c "import app_meta; print(app_meta.MACOS_MINIMUM_SYSTEM_VERSION)"
)"
export XL_TRANSLATOR_MACOS_MINIMUM_SYSTEM_VERSION="${XL_TRANSLATOR_MACOS_MINIMUM_SYSTEM_VERSION:-$DEFAULT_MACOS_MINIMUM_SYSTEM_VERSION}"
export MACOSX_DEPLOYMENT_TARGET="$XL_TRANSLATOR_MACOS_MINIMUM_SYSTEM_VERSION"

mkdir -p "$DIST_DIR"
export PYINSTALLER_CONFIG_DIR="$ROOT_DIR/.runtime/pyinstaller-config"
mkdir -p "$PYINSTALLER_CONFIG_DIR"

echo "[INFO] Verify build dependencies"
"$PYTHON" -c "import PyInstaller, PIL; from PySide6 import QtWidgets; print('ok')"
"$PYTHON" scripts/verify_release_dependencies.py \
  --constraints "$CONSTRAINTS_PATH"

VERSION="$("$PYTHON" -c "import app_meta; print(app_meta.APP_VERSION)")"
if [[ -z "$VERSION" ]]; then
  echo "APP_VERSION could not be resolved." >&2
  exit 1
fi
"$PYTHON" scripts/check_changelog_version.py --version "$VERSION"
APP_NAME="$("$PYTHON" -c "import app_meta; print(app_meta.APP_NAME)")"
MACOS_APP_BUNDLE_NAME="$("$PYTHON" -c "import app_meta; print(app_meta.MACOS_APP_BUNDLE_NAME)")"
MACOS_COLLECT_NAME="$("$PYTHON" -c "import app_meta; print(app_meta.MACOS_COLLECT_NAME)")"
MACOS_DMG_BASENAME="$("$PYTHON" -c "import app_meta; print(app_meta.MACOS_DMG_BASENAME)")"
if [[ -z "$APP_NAME" || -z "$MACOS_APP_BUNDLE_NAME" || -z "$MACOS_COLLECT_NAME" || -z "$MACOS_DMG_BASENAME" ]]; then
  echo "App packaging metadata could not be resolved." >&2
  exit 1
fi

echo "[INFO] Prepare macOS icon"
"$PYTHON" scripts/prepare_icons.py --macos

APP_PATH="$DIST_DIR/$MACOS_APP_BUNDLE_NAME"
COLLECT_PATH="$DIST_DIR/$MACOS_COLLECT_NAME"
DMG_NAME="${MACOS_DMG_BASENAME}.dmg"
DMG_PATH="$DIST_DIR/$DMG_NAME"
CHECKSUM_PATH="$DMG_PATH.sha256"

SPEC_BUILD_NAME="$(basename "$SPEC_PATH" .spec)"
rm -rf "$ROOT_DIR/build/$SPEC_BUILD_NAME" "$APP_PATH" "$COLLECT_PATH" "$STAGING_DIR"
rm -f "$DMG_PATH" "$CHECKSUM_PATH"

echo "[INFO] Build macOS app bundle"
"$PYTHON" -m PyInstaller --noconfirm "$SPEC_PATH"

if [[ ! -d "$APP_PATH" ]]; then
  echo "Expected app bundle was not produced: $APP_PATH" >&2
  exit 1
fi

echo "[INFO] Verify frozen executable startup"
"$PYTHON" scripts/run_frozen_smoke.py \
  "$APP_PATH/Contents/MacOS/$APP_NAME" \
  --timeout 60

echo "[INFO] Verify declared macOS minimum version"
"$PYTHON" scripts/verify_macos_minimum_version.py \
  "$APP_PATH" \
  --declared "$XL_TRANSLATOR_MACOS_MINIMUM_SYSTEM_VERSION"

if [[ -n "${XL_TRANSLATOR_MACOS_CODESIGN_IDENTITY:-}" ]]; then
  echo "[INFO] Sign macOS app bundle"
  codesign_args=(
    --force
    --deep
    --options runtime
    --timestamp
    --sign "$XL_TRANSLATOR_MACOS_CODESIGN_IDENTITY"
  )
  if [[ -n "${XL_TRANSLATOR_MACOS_ENTITLEMENTS:-}" ]]; then
    codesign_args+=(--entitlements "$XL_TRANSLATOR_MACOS_ENTITLEMENTS")
  fi
  codesign "${codesign_args[@]}" "$APP_PATH"
  codesign --verify --deep --strict --verbose=2 "$APP_PATH"
else
  echo "[INFO] No Developer ID identity configured; app has no trusted release signature."
fi

echo "[INFO] Build macOS dmg"
mkdir -p "$STAGING_DIR"
cp -R "$APP_PATH" "$STAGING_DIR/"
ln -s /Applications "$STAGING_DIR/Applications"
hdiutil create \
  -volname "$APP_NAME" \
  -srcfolder "$STAGING_DIR" \
  -ov \
  -format UDZO \
  "$DMG_PATH"

if [[ -n "${XL_TRANSLATOR_MACOS_CODESIGN_IDENTITY:-}" ]]; then
  echo "[INFO] Sign macOS dmg"
  codesign \
    --force \
    --timestamp \
    --sign "$XL_TRANSLATOR_MACOS_CODESIGN_IDENTITY" \
    "$DMG_PATH"
  codesign --verify --strict --verbose=2 "$DMG_PATH"
fi

if [[ -n "${XL_TRANSLATOR_MACOS_NOTARY_PROFILE:-}" ]]; then
  if [[ -z "${XL_TRANSLATOR_MACOS_CODESIGN_IDENTITY:-}" ]]; then
    echo "Notarization requires XL_TRANSLATOR_MACOS_CODESIGN_IDENTITY." >&2
    exit 1
  fi
  echo "[INFO] Submit dmg for notarization"
  xcrun notarytool submit \
    "$DMG_PATH" \
    --keychain-profile "$XL_TRANSLATOR_MACOS_NOTARY_PROFILE" \
    --wait
  xcrun stapler staple "$DMG_PATH"
  xcrun stapler validate "$DMG_PATH"
else
  echo "[INFO] No notarization profile configured; dmg is not notarized."
fi

shasum -a 256 "$DMG_PATH" | sed "s|$DMG_PATH|$DMG_NAME|" > "$CHECKSUM_PATH"

if [[ -n "${GITHUB_ENV:-}" ]]; then
  {
    echo "MACOS_DMG=dist/$DMG_NAME"
    echo "MACOS_DMG_SHA256=dist/$DMG_NAME.sha256"
  } >> "$GITHUB_ENV"
fi

echo "[INFO] macOS dmg: $DMG_PATH"
echo "[INFO] SHA256: $CHECKSUM_PATH"
