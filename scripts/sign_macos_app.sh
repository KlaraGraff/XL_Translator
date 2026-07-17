#!/bin/bash
set -euo pipefail

APP_PATH="${1:?Usage: sign_macos_app.sh APP_PATH [SIDECAR_PATH]}"
SIDECAR_PATH="${2:-}"
SIGNING_IDENTITY="${XL_TRANSLATOR_MACOS_CODESIGN_IDENTITY:--}"

if [[ ! -d "$APP_PATH" ]]; then
  echo "Application bundle was not found: $APP_PATH" >&2
  exit 1
fi
if [[ -n "$SIDECAR_PATH" && ! -x "$SIDECAR_PATH" ]]; then
  echo "Bundled sidecar was not found or is not executable: $SIDECAR_PATH" >&2
  exit 1
fi

if [[ "$SIGNING_IDENTITY" == "-" ]]; then
  echo "[INFO] No Developer ID identity configured; applying a complete ad-hoc bundle signature."
  codesign --force --deep --sign - "$APP_PATH"
else
  if [[ -n "$SIDECAR_PATH" ]]; then
    codesign \
      --force \
      --options runtime \
      --timestamp \
      --sign "$SIGNING_IDENTITY" \
      "$SIDECAR_PATH"
  fi
  codesign \
    --force \
    --deep \
    --options runtime \
    --timestamp \
    --sign "$SIGNING_IDENTITY" \
    "$APP_PATH"
fi

# Tauri's unsigned app bundle otherwise contains only the linker's signature on
# the main executable. Gatekeeper reports that malformed bundle as "damaged".
# Always verify that Info.plist and resources are sealed before creating a dmg.
codesign --verify --deep --strict --verbose=2 "$APP_PATH"
