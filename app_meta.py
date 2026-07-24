"""Centralized app metadata and version helpers."""

APP_NAME = "Translator"
APP_VERSION = "8.0.0"
APP_VERSION_LABEL = f"V{APP_VERSION}"

APP_SAFE_NAME = "_".join(APP_NAME.split())
APP_BUNDLE_IDENTIFIER = f"com.klara-graff.{APP_SAFE_NAME.lower().replace('_', '-')}"
APP_UPDATE_USER_AGENT = f"{APP_SAFE_NAME}-Updater"

MACOS_COLLECT_NAME = APP_NAME
MACOS_APP_BUNDLE_NAME = f"{APP_NAME}.app"
MACOS_MINIMUM_SYSTEM_VERSION = "12.0"


def macos_dmg_basename(architecture: str) -> str:
    """Return a native macOS release asset name for one supported architecture."""
    normalized = str(architecture or "").strip().lower()
    if normalized not in {"arm64", "x86_64"}:
        raise ValueError("macOS DMG architecture must be arm64 or x86_64")
    asset_architecture = "x64" if normalized == "x86_64" else normalized
    return f"{APP_SAFE_NAME}_macOS_{asset_architecture}_{APP_VERSION}"

DEFAULT_DISTRIBUTION_OUTPUT_NAME = f"{APP_SAFE_NAME}_Distribution"


def build_versioned_distribution_zip_name(
    output_name: str = DEFAULT_DISTRIBUTION_OUTPUT_NAME,
) -> str:
    """Build the version-suffixed release zip basename."""
    return f"{output_name}_{APP_VERSION}"
