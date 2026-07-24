#!/bin/bash
set -euo pipefail

APP_PATH="${1:?Usage: sign_macos_app.sh APP_PATH [SIDECAR_PATH]}"
SIDECAR_PATH="${2:-}"
SIGNING_IDENTITY="${XL_TRANSLATOR_MACOS_CODESIGN_IDENTITY:--}"
REQUIRE_DEVELOPER_ID="${XL_TRANSLATOR_REQUIRE_DEVELOPER_ID:-0}"
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENTITLEMENTS_PATH="${XL_TRANSLATOR_MACOS_ENTITLEMENTS:-$ROOT_DIR/packaging/macos/translator.entitlements}"
ADHOC_ENTITLEMENTS_PATH="$ROOT_DIR/packaging/macos/translator-adhoc.entitlements"

if [[ ! -d "$APP_PATH" ]]; then
  echo "Application bundle was not found: $APP_PATH" >&2
  exit 1
fi
if [[ -n "$SIDECAR_PATH" && ! -x "$SIDECAR_PATH" ]]; then
  echo "Bundled sidecar was not found or is not executable: $SIDECAR_PATH" >&2
  exit 1
fi
if [[ ! -f "$ENTITLEMENTS_PATH" ]]; then
  echo "Apple Events entitlements file was not found: $ENTITLEMENTS_PATH" >&2
  exit 1
fi
if [[ "$SIGNING_IDENTITY" == "-" && ! -f "$ADHOC_ENTITLEMENTS_PATH" ]]; then
  echo "Ad-hoc entitlements file was not found: $ADHOC_ENTITLEMENTS_PATH" >&2
  exit 1
fi
if [[ "$REQUIRE_DEVELOPER_ID" != "0" && "$REQUIRE_DEVELOPER_ID" != "1" ]]; then
  echo "XL_TRANSLATOR_REQUIRE_DEVELOPER_ID must be 0 or 1." >&2
  exit 1
fi
if [[ "$REQUIRE_DEVELOPER_ID" == "1" && "$SIGNING_IDENTITY" == "-" ]]; then
  echo "Formal releases require a Developer ID Application signing identity." >&2
  exit 1
fi

if [[ "$SIGNING_IDENTITY" == "-" ]]; then
  echo "[INFO] No Developer ID identity configured; applying a complete ad-hoc test signature."
  ENTITLEMENTS_PATH="$ADHOC_ENTITLEMENTS_PATH"
fi
SIGN_OPTIONS=(--force --options runtime --entitlements "$ENTITLEMENTS_PATH")
NESTED_SIGN_OPTIONS=(--force --options runtime)
if [[ "$SIGNING_IDENTITY" != "-" ]]; then
  SIGN_OPTIONS+=(--timestamp)
  NESTED_SIGN_OPTIONS+=(--timestamp)
fi
if [[ -n "$SIDECAR_PATH" ]]; then
  SIDECAR_DIR="$(dirname "$SIDECAR_PATH")"
  while IFS= read -r -d '' NESTED_PATH; do
    if [[ "$NESTED_PATH" != "$SIDECAR_PATH" ]] \
      && /usr/bin/file -b "$NESTED_PATH" | grep -q '^Mach-O'; then
      codesign \
        "${NESTED_SIGN_OPTIONS[@]}" \
        --sign "$SIGNING_IDENTITY" \
        "$NESTED_PATH"
    fi
  done < <(find "$SIDECAR_DIR" -type f -print0)
  codesign \
    "${SIGN_OPTIONS[@]}" \
    --sign "$SIGNING_IDENTITY" \
    "$SIDECAR_PATH"
fi
codesign \
  --deep \
  "${SIGN_OPTIONS[@]}" \
  --sign "$SIGNING_IDENTITY" \
  "$APP_PATH"

# Tauri's unsigned app bundle otherwise contains only the linker's signature on
# the main executable. Gatekeeper reports that malformed bundle as "damaged".
# Always verify that Info.plist and resources are sealed before creating a dmg.
codesign --verify --deep --strict --verbose=2 "$APP_PATH"
if [[ "$REQUIRE_DEVELOPER_ID" == "1" ]]; then
  SIGNATURE_DETAILS="$(codesign -dv --verbose=4 "$APP_PATH" 2>&1)"
  if ! grep -Fq "Authority=Developer ID Application:" <<<"$SIGNATURE_DETAILS"; then
    echo "The formal release app is not signed by a Developer ID Application certificate." >&2
    exit 1
  fi
  if ! grep -Fq "runtime" <<<"$SIGNATURE_DETAILS"; then
    echo "The formal release app is missing the Hardened Runtime signature option." >&2
    exit 1
  fi
  for signed_target in "$APP_PATH" "$SIDECAR_PATH"; do
    [[ -n "$signed_target" ]] || continue
    entitlements_file="$(mktemp)"
    codesign -d --entitlements :- "$signed_target" > "$entitlements_file" 2>/dev/null
    entitlement_value="$(/usr/libexec/PlistBuddy -c 'Print :com.apple.security.automation.apple-events' "$entitlements_file" 2>/dev/null || true)"
    rm -f "$entitlements_file"
    if [[ "$entitlement_value" != "true" ]]; then
      echo "The formal release target is missing Apple Events automation entitlement: $signed_target" >&2
      exit 1
    fi
  done
fi
