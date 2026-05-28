"""Centralized app metadata and version helpers."""

APP_NAME = "Translator"
APP_VERSION = "6.1"
APP_VERSION_LABEL = f"V{APP_VERSION}"

APP_SAFE_NAME = "_".join(APP_NAME.split())
APP_BUNDLE_IDENTIFIER = f"com.klara-graff.{APP_SAFE_NAME.lower().replace('_', '-')}"
APP_UPDATE_USER_AGENT = f"{APP_SAFE_NAME}-Updater"

WINDOWS_PACKAGE_NAME = f"{APP_SAFE_NAME}_Windows"
WINDOWS_EXE_NAME = f"{APP_NAME}.exe"
WINDOWS_SETUP_BASENAME = f"{WINDOWS_PACKAGE_NAME}_{APP_VERSION}_Setup"

MACOS_COLLECT_NAME = APP_NAME
MACOS_APP_BUNDLE_NAME = f"{APP_NAME}.app"
MACOS_DMG_BASENAME = f"{APP_SAFE_NAME}_macOS_{APP_VERSION}"

DEFAULT_DISTRIBUTION_OUTPUT_NAME = f"{APP_SAFE_NAME}_Distribution"


def build_versioned_distribution_zip_name(
    output_name: str = DEFAULT_DISTRIBUTION_OUTPUT_NAME,
) -> str:
    """Build the version-suffixed release zip basename."""
    return f"{output_name}_{APP_VERSION}"
