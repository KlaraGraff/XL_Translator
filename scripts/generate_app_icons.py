"""Regenerate the app icons from the in-app 文A brand mark.

The Dock/taskbar icon and the mark in the window's top-left corner have to be
the same thing, so both are generated from one description instead of being
drawn twice.  The source of truth is ``.brand-mark`` in
``ui/src/styles/app.css``: a rounded square filled with a 135° gradient from
``#6b7dff`` to ``#4353cc``, carrying a white bold 文A.

Run from the repository root::

    ./.venv/bin/python3 scripts/generate_app_icons.py

It rewrites every file listed under ``bundle.icon`` in
``src-tauri/tauri.conf.json``.  ``icon.icns`` is assembled with macOS'
``iconutil`` and is therefore only refreshed when running on macOS; the PNG and
ICO outputs are produced everywhere.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parent.parent
ICONS_DIR = REPO_ROOT / "src-tauri" / "icons"

MARK_TEXT = "文A"
GRADIENT_START = (0x6B, 0x7D, 0xFF)
GRADIENT_END = (0x43, 0x53, 0xCC)

# All three ratios come from ``.brand-mark``: a 27px box with an 8px radius and
# 12px text.  Keeping them as ratios is what makes the 32px and the 1024px
# renders read as the same mark.
CORNER_RADIUS_RATIO = 8 / 27
FONT_SIZE_RATIO = 12 / 27
# The mark itself sits edge to edge inside the app window; an app icon needs a
# little air around it or it looks oversized next to its Dock neighbours.
MARGIN_RATIO = 0.06
# Corners and gradient are drawn at 4x and downsampled: the rounded corners of a
# 32px icon are otherwise visibly stepped.
SUPERSAMPLE = 4

# Candidate CJK faces, heaviest usable weight first.  The mark is font-weight
# 700, and a light 文 next to a bold A looks like a rendering bug.
FONT_CANDIDATES: tuple[tuple[str, int], ...] = (
    ("/System/Library/Fonts/PingFang.ttc", 4),
    ("/System/Library/Fonts/Hiragino Sans GB.ttc", 2),
    ("/System/Library/Fonts/Supplemental/Songti.ttc", 1),
)

PNG_OUTPUTS = {
    "32x32.png": 32,
    "128x128.png": 128,
    "128x128@2x.png": 256,
    "icon.png": 512,
}
ICO_SIZES = (16, 32, 48, 64, 128, 256)
ICNS_SIZES = (16, 32, 64, 128, 256, 512, 1024)


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for path, index in FONT_CANDIDATES:
        if not Path(path).exists():
            continue
        try:
            return ImageFont.truetype(path, size, index=index)
        except OSError:
            continue
    raise SystemExit(
        "找不到可用的中文字体，无法绘制「文A」。请在 macOS 上运行，或把可用字体加进 FONT_CANDIDATES。"
    )


def _gradient(size: int) -> Image.Image:
    """Paint the CSS ``linear-gradient(135deg, …)``: top-left to bottom-right."""
    gradient = Image.new("RGB", (size, size))
    pixels = gradient.load()
    span = max(1, (size - 1) * 2)
    for y in range(size):
        for x in range(size):
            ratio = (x + y) / span
            pixels[x, y] = tuple(
                round(start + (end - start) * ratio)
                for start, end in zip(GRADIENT_START, GRADIENT_END)
            )
    return gradient


def render_icon(size: int) -> Image.Image:
    work = size * SUPERSAMPLE
    margin = round(work * MARGIN_RATIO)
    side = work - margin * 2
    radius = round(side * CORNER_RADIUS_RATIO)

    mask = Image.new("L", (work, work), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (margin, margin, margin + side - 1, margin + side - 1),
        radius=radius,
        fill=255,
    )
    canvas = Image.new("RGBA", (work, work), (0, 0, 0, 0))
    canvas.paste(_gradient(work), (0, 0), mask)

    font = _load_font(round(side * FONT_SIZE_RATIO))
    draw = ImageDraw.Draw(canvas)
    # anchor="mm" centres on the glyph ink box, which is what visually centres
    # 文A; the text bounding box would leave it sitting low.
    draw.text(
        (work / 2, work / 2),
        MARK_TEXT,
        font=font,
        fill=(255, 255, 255, 255),
        anchor="mm",
    )
    return canvas.resize((size, size), Image.LANCZOS)


def _write_icns(destination: Path) -> bool:
    if sys.platform != "darwin" or shutil.which("iconutil") is None:
        print("跳过 icon.icns：需要 macOS 的 iconutil。")
        return False
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "icon.iconset"
        iconset.mkdir()
        for size in ICNS_SIZES:
            image = render_icon(size)
            if size <= 512:
                image.save(iconset / f"icon_{size}x{size}.png")
            if size >= 32:
                image.save(iconset / f"icon_{size // 2}x{size // 2}@2x.png")
        subprocess.run(
            ["iconutil", "-c", "icns", str(iconset), "-o", str(destination)],
            check=True,
        )
    return True


def main() -> None:
    ICONS_DIR.mkdir(parents=True, exist_ok=True)
    for name, size in PNG_OUTPUTS.items():
        render_icon(size).save(ICONS_DIR / name)
        print(f"已写出 {name}（{size}×{size}）")

    largest = render_icon(max(ICO_SIZES))
    largest.save(
        ICONS_DIR / "icon.ico",
        sizes=[(size, size) for size in ICO_SIZES],
    )
    print("已写出 icon.ico")

    if _write_icns(ICONS_DIR / "icon.icns"):
        print("已写出 icon.icns")


if __name__ == "__main__":
    main()
