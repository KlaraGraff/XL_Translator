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
REPORT_DIR="$ROOT_DIR/.runtime/package/macos-reports"
FORMAL_RELEASE="${XL_TRANSLATOR_FORMAL_RELEASE:-0}"
TEMPORARY_SIGNING="${XL_TRANSLATOR_TEMPORARY_SIGNING:-0}"

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
if [[ "$FORMAL_RELEASE" != "0" && "$FORMAL_RELEASE" != "1" ]]; then
  echo "XL_TRANSLATOR_FORMAL_RELEASE must be 0 or 1; got $FORMAL_RELEASE" >&2
  exit 1
fi
if [[ "$TEMPORARY_SIGNING" != "0" && "$TEMPORARY_SIGNING" != "1" ]]; then
  echo "XL_TRANSLATOR_TEMPORARY_SIGNING must be 0 or 1; got $TEMPORARY_SIGNING" >&2
  exit 1
fi
if [[ "$FORMAL_RELEASE" == "1" && "$TEMPORARY_SIGNING" == "1" ]]; then
  echo "Temporary signing cannot be used for a formal release." >&2
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
HOST_ARCH="$(uname -m)"
if [[ "$HOST_ARCH" != "$MACOS_ARCH" ]]; then
  echo "Native macOS release build required: host is $HOST_ARCH but MACOS_ARCH is $MACOS_ARCH" >&2
  exit 1
fi
# Rust respects MACOSX_DEPLOYMENT_TARGET on macOS.  The explicit linker flag
# makes the deployment baseline auditable even when a future toolchain changes
# its defaults.
MINOS_LINK_FLAG="-C link-arg=-mmacosx-version-min=$DEPLOYMENT_TARGET"
if [[ " ${RUSTFLAGS:-} " != *" $MINOS_LINK_FLAG "* ]]; then
  export RUSTFLAGS="${RUSTFLAGS:-} $MINOS_LINK_FLAG"
fi
"$PYTHON_BIN" - <<'PY'
import sys

if sys.version_info[:2] != (3, 11):
    raise SystemExit(
        "macOS release builds require Python 3.11; "
        f"got {sys.version.split()[0]} from {sys.executable}"
    )
PY
if [[ "$FORMAL_RELEASE" == "1" ]]; then
  if [[ -z "${XL_TRANSLATOR_MACOS_CODESIGN_IDENTITY:-}" ]]; then
    echo "Formal releases require XL_TRANSLATOR_MACOS_CODESIGN_IDENTITY." >&2
    exit 1
  fi
  if [[ -z "${XL_TRANSLATOR_MACOS_NOTARY_PROFILE:-}" ]]; then
    echo "Formal releases require XL_TRANSLATOR_MACOS_NOTARY_PROFILE." >&2
    exit 1
  fi
elif [[ -n "${XL_TRANSLATOR_MACOS_NOTARY_PROFILE:-}" && -z "${XL_TRANSLATOR_MACOS_CODESIGN_IDENTITY:-}" ]]; then
  echo "Notarization requires XL_TRANSLATOR_MACOS_CODESIGN_IDENTITY." >&2
  exit 1
fi

cd "$ROOT_DIR"
VERSION="$($PYTHON_BIN -c 'import app_meta; print(app_meta.APP_VERSION)')"
if [[ "$FORMAL_RELEASE" == "1" ]]; then
  DMG_NAME="Translator_macOS_${ARCH_LABEL}_${VERSION}.dmg"
elif [[ "$TEMPORARY_SIGNING" == "1" ]]; then
  DMG_NAME="Translator_macOS_${ARCH_LABEL}_${VERSION}_TEMP_SIGNED_TEST.dmg"
else
  DMG_NAME="Translator_macOS_${ARCH_LABEL}_${VERSION}_UNSIGNED_TEST.dmg"
fi
DMG_PATH="$DIST_DIR/$DMG_NAME"
REPORT_PATH="$REPORT_DIR/Translator_macOS_${ARCH_LABEL}_${VERSION}.json"

mkdir -p "$DIST_DIR"
"$PYTHON_BIN" scripts/build_tauri_package.py --platform macos --python "$PYTHON_BIN"

if [[ ! -d "$APP_PATH" || ! -x "$SIDECAR_PATH" ]]; then
  echo "Tauri app or bundled sidecar was not produced." >&2
  exit 1
fi

"$PYTHON_BIN" scripts/verify_macos_minimum_version.py \
  "$APP_PATH" \
  --declared "$MINIMUM_MACOS_VERSION" \
  --architecture "$MACOS_ARCH" \
  --report "$REPORT_PATH"

XL_TRANSLATOR_REQUIRE_DEVELOPER_ID="$FORMAL_RELEASE" \
  bash "$ROOT_DIR/scripts/sign_macos_app.sh" "$APP_PATH" "$SIDECAR_PATH"

# Notarize and staple the .app itself, not only the DMG that carries it.
# An in-app update replaces the bundle directly — no DMG is ever involved —
# so a ticket stapled only to the DMG would leave the updated app depending on
# an online Gatekeeper check at every first launch. Stapling here also means
# the app inside the DMG arrives pre-stapled.
if [[ -n "${XL_TRANSLATOR_MACOS_NOTARY_PROFILE:-}" ]]; then
  APP_NOTARY_ZIP="$ROOT_DIR/.runtime/package/macos-app-notarize.zip"
  mkdir -p "$(dirname "$APP_NOTARY_ZIP")"
  rm -f "$APP_NOTARY_ZIP"
  # ditto, not zip: it is the only archiver that preserves the bundle's
  # symlinks and resource forks intact enough for notarization.
  ditto -c -k --keepParent "$APP_PATH" "$APP_NOTARY_ZIP"
  xcrun notarytool submit "$APP_NOTARY_ZIP" --keychain-profile "$XL_TRANSLATOR_MACOS_NOTARY_PROFILE" --wait
  xcrun stapler staple "$APP_PATH"
  xcrun stapler validate "$APP_PATH"
  rm -f "$APP_NOTARY_ZIP"
fi

# The in-app update archive: a plain tarball of the finished .app, signed with
# the project's minisign key. The updater downloads it over HTTPS but trusts it
# only because of this signature, so it must be produced from exactly the
# bundle a user would otherwise install from the DMG — same signing, same
# notarization, same stapled ticket.
UPDATER_ARCHIVE_NAME="Translator_macOS_${ARCH_LABEL}_${VERSION}.app.tar.gz"
UPDATER_ARCHIVE_PATH="$DIST_DIR/$UPDATER_ARCHIVE_NAME"
# Only a formal release may produce one. A temporary/unsigned test build is
# ad-hoc signed and never notarized, and its update archive would carry the
# production name — un-drafting such a release would hand every installed copy
# an unnotarized bundle with no download step in between. The CI workflow
# already withholds the key outside a formal release; this is the second lock,
# for anyone running the script by hand with the key in their environment.
if [[ -n "${TAURI_SIGNING_PRIVATE_KEY:-}" && "$FORMAL_RELEASE" != "1" ]]; then
  echo "[WARN] Not a formal release: skipping the macOS in-app update archive even though a signing key is present." >&2
elif [[ -n "${TAURI_SIGNING_PRIVATE_KEY:-}" ]]; then
  TAURI_CLI="$ROOT_DIR/ui/node_modules/.bin/tauri"
  if [[ ! -x "$TAURI_CLI" ]]; then
    echo "Tauri CLI is unavailable; run npm ci in ui/ before packaging." >&2
    exit 1
  fi
  rm -f "$UPDATER_ARCHIVE_PATH" "$UPDATER_ARCHIVE_PATH.sig"
  # COPYFILE_DISABLE keeps macOS from smuggling AppleDouble "._" companions
  # into the tarball; the updater would unpack them next to the real bundle.
  COPYFILE_DISABLE=1 tar -czf "$UPDATER_ARCHIVE_PATH" \
    -C "$(dirname "$APP_PATH")" "$(basename "$APP_PATH")"
  # The key and its password come from the environment. Redirecting stdin from
  # /dev/null turns a mis-wired secret into an immediate failure instead of a
  # CI job that hangs on an interactive password prompt.
  "$TAURI_CLI" signer sign "$UPDATER_ARCHIVE_PATH" < /dev/null
  if [[ ! -s "$UPDATER_ARCHIVE_PATH.sig" ]]; then
    echo "The macOS update archive was not signed: $UPDATER_ARCHIVE_PATH.sig is missing or empty" >&2
    exit 1
  fi
  if [[ -n "${GITHUB_ENV:-}" ]]; then
    {
      echo "MACOS_UPDATER_ARCHIVE=dist/$UPDATER_ARCHIVE_NAME"
      echo "MACOS_UPDATER_SIGNATURE=dist/$UPDATER_ARCHIVE_NAME.sig"
    } >> "$GITHUB_ENV"
  fi
  echo "[INFO] macOS update archive: $UPDATER_ARCHIVE_PATH"
else
  echo "[WARN] TAURI_SIGNING_PRIVATE_KEY is unset: skipping the macOS in-app update archive." >&2
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
if [[ "$FORMAL_RELEASE" == "1" ]]; then
  # A notarized, stapled release must be accepted by the local Gatekeeper
  # policy before its checksum is uploaded as an official asset.
  spctl --assess --type open --context context:primary-signature --verbose=4 "$DMG_PATH"
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
    echo "MACOS_RELEASE_REPORT=.runtime/package/macos-reports/$(basename "$REPORT_PATH")"
  } >> "$GITHUB_ENV"
fi
echo "[INFO] macOS dmg (${SIZE_MB}MB): $DMG_PATH"
echo "[INFO] macOS binary report: $REPORT_PATH"
