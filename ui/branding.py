"""Branding helpers for the app icon and sidebar logo."""

from __future__ import annotations

import base64
import html
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from app_meta import APP_NAME

DEFAULT_BRAND_EMOJI = "🔤"
DEFAULT_BRAND_LABEL = APP_NAME
BRAND_ICON_RELATIVE_PATH = Path("assets") / "branding" / "app-icon.png"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
BRAND_ICON_PATH = PROJECT_ROOT / BRAND_ICON_RELATIVE_PATH


@dataclass(frozen=True)
class BrandIconAsset:
    """Resolved branding asset plus the validated PNG payload when available."""

    path: Path
    png_bytes: bytes | None = None
    error: str | None = None

    @property
    def has_custom_icon(self) -> bool:
        return self.png_bytes is not None

    @property
    def relative_path(self) -> str:
        return BRAND_ICON_RELATIVE_PATH.as_posix()

    @property
    def data_url(self) -> str | None:
        if not self.png_bytes:
            return None
        encoded = base64.b64encode(self.png_bytes).decode("ascii")
        return f"data:image/png;base64,{encoded}"


def get_brand_icon_path() -> Path:
    """Return the fixed app-icon path that users can replace directly."""
    return BRAND_ICON_PATH


def load_brand_icon_asset(icon_path: Path | None = None) -> BrandIconAsset:
    """Read and validate the user-replaceable PNG icon."""
    resolved_path = icon_path or get_brand_icon_path()
    if not resolved_path.exists():
        return BrandIconAsset(path=resolved_path, error="missing")

    try:
        png_bytes = resolved_path.read_bytes()
        with Image.open(BytesIO(png_bytes)) as image:
            image.load()
            if image.format != "PNG":
                raise ValueError(f"expected PNG, got {image.format or 'unknown'}")
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        return BrandIconAsset(path=resolved_path, error=str(exc))

    return BrandIconAsset(path=resolved_path, png_bytes=png_bytes)


def get_page_icon_config(icon_path: Path | None = None):
    """Return a Streamlit-compatible favicon config with a safe fallback."""
    asset = load_brand_icon_asset(icon_path)
    if not asset.has_custom_icon:
        return DEFAULT_BRAND_EMOJI

    with Image.open(BytesIO(asset.png_bytes)) as image:
        return image.copy()


def build_sidebar_brand_label_html(
    label: str = DEFAULT_BRAND_LABEL,
    icon_path: Path | None = None,
) -> str:
    """Build the sidebar brand label with either the custom PNG or fallback emoji."""
    asset = load_brand_icon_asset(icon_path)
    if asset.data_url:
        icon_markup = (
            f'<img class="sidebar-title__icon" src="{asset.data_url}" '
            'alt="" aria-hidden="true" />'
        )
        icon_state = "custom"
    else:
        icon_markup = (
            '<span class="sidebar-title__icon sidebar-title__icon--fallback" '
            f'aria-hidden="true">{html.escape(DEFAULT_BRAND_EMOJI)}</span>'
        )
        icon_state = "fallback"

    return (
        '<span class="sidebar-title__brand" '
        f'data-brand-icon-state="{icon_state}" '
        f'data-brand-icon-path="{html.escape(asset.relative_path, quote=True)}">'
        f"{icon_markup}"
        f'<span class="sidebar-title__text">{html.escape(label)}</span>'
        "</span>"
    )
