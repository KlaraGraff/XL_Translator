"""Centralized app metadata and version helpers."""

APP_NAME = "Translator"
APP_VERSION = "9.3.1"
APP_VERSION_LABEL = f"V{APP_VERSION}"

APP_SAFE_NAME = "_".join(APP_NAME.split())
APP_BUNDLE_IDENTIFIER = f"com.klara-graff.{APP_SAFE_NAME.lower().replace('_', '-')}"
APP_UPDATE_USER_AGENT = f"{APP_SAFE_NAME}-Updater"

MACOS_COLLECT_NAME = APP_NAME
MACOS_APP_BUNDLE_NAME = f"{APP_NAME}.app"
MACOS_MINIMUM_SYSTEM_VERSION = "12.0"
WINDOWS_MINIMUM_SYSTEM_VERSION = "10.0"


def macos_dmg_basename(architecture: str) -> str:
    """Return a native macOS release asset name for one supported architecture."""
    normalized = str(architecture or "").strip().lower()
    if normalized not in {"arm64", "x86_64"}:
        raise ValueError("macOS DMG architecture must be arm64 or x86_64")
    asset_architecture = "x64" if normalized == "x86_64" else normalized
    return f"{APP_SAFE_NAME}_macOS_{asset_architecture}_{APP_VERSION}"


def windows_installer_basename() -> str:
    """Return the native Windows x64 release asset name (no extension)."""
    return f"{APP_SAFE_NAME}_Windows_x64_{APP_VERSION}_Setup"


# What the in-app updater downloads differs per platform, and not by choice —
# it is what the Tauri v2 updater plugin knows how to consume:
#   * macOS: a tarball of the .app. The DMG needs a person to drag it, so the
#     updater gets its own archive published alongside.
#   * Windows: the NSIS setup .exe itself, run unattended. It is "self
#     contained" in Tauri v2 terms — there is no separate archive, and asking
#     the bundler for one produces nothing (see build_tauri_package.py).
# Both are signed with the updater's minisign key; the signature rides along as
# a sibling `.sig` file.
UPDATER_MANIFEST_NAME = "latest.json"


def macos_updater_archive_name(architecture: str) -> str:
    """Return the macOS in-app update archive name (a tarball of the .app)."""
    return f"{macos_dmg_basename(architecture)}.app.tar.gz"


def windows_updater_artifact_name() -> str:
    """Return the Windows in-app update artifact name — the installer itself."""
    return f"{windows_installer_basename()}.exe"


DEFAULT_DISTRIBUTION_OUTPUT_NAME = f"{APP_SAFE_NAME}_Distribution"


def build_versioned_distribution_zip_name(
    output_name: str = DEFAULT_DISTRIBUTION_OUTPUT_NAME,
) -> str:
    """Build the version-suffixed release zip basename."""
    return f"{output_name}_{APP_VERSION}"
