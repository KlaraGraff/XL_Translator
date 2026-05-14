from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image


ROOT_DIR = Path(__file__).resolve().parent.parent
SOURCE_ICON = ROOT_DIR / "assets" / "branding" / "app-icon.png"
WINDOWS_ICON = ROOT_DIR / "packaging" / "windows" / "assets" / "app-icon.ico"
MACOS_ICON = ROOT_DIR / "packaging" / "macos" / "assets" / "app-icon.icns"

WINDOWS_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
MACOS_SIZES = [(16, 16), (32, 32), (64, 64), (128, 128), (256, 256), (512, 512), (1024, 1024)]


def load_source_icon(source: Path) -> Image.Image:
    if not source.exists():
        raise FileNotFoundError(f"Missing source icon: {source}")
    with Image.open(source) as image:
        return image.convert("RGBA")


def save_windows_icon(image: Image.Image, output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output, format="ICO", sizes=WINDOWS_SIZES)
    return output


def save_macos_icon(image: Image.Image, output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output, format="ICNS", sizes=MACOS_SIZES)
    return output


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare installer icon assets.")
    parser.add_argument("--source", type=Path, default=SOURCE_ICON)
    parser.add_argument("--windows-output", type=Path, default=WINDOWS_ICON)
    parser.add_argument("--macos-output", type=Path, default=MACOS_ICON)
    parser.add_argument("--windows", action="store_true", help="Only generate the Windows .ico file.")
    parser.add_argument("--macos", action="store_true", help="Only generate the macOS .icns file.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    image = load_source_icon(args.source)
    build_windows = args.windows or not args.macos
    build_macos = args.macos or not args.windows

    if build_windows:
        output = save_windows_icon(image, args.windows_output)
        print(f"[INFO] Wrote Windows icon: {output}")
    if build_macos:
        output = save_macos_icon(image, args.macos_output)
        print(f"[INFO] Wrote macOS icon: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
