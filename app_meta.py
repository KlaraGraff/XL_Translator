"""Centralized app metadata and version helpers."""

APP_NAME = "XL Translator"
APP_VERSION = "4.0"
APP_VERSION_LABEL = f"V{APP_VERSION}"

DEFAULT_DISTRIBUTION_OUTPUT_NAME = "XL_Translator_Distribution"


def build_versioned_distribution_zip_name(
    output_name: str = DEFAULT_DISTRIBUTION_OUTPUT_NAME,
) -> str:
    """Build the version-suffixed release zip basename."""
    return f"{output_name}_{APP_VERSION}"
