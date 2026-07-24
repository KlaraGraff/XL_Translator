"""Build a platform-native Tauri bundle with its frozen Python sidecar."""

from __future__ import annotations

import argparse
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC_TAURI = ROOT / "src-tauri"
UI = ROOT / "ui"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_tauri_sidecar import build_sidecar  # noqa: E402


def target_platform(raw: str) -> str:
    value = raw.strip().lower()
    if value == "current":
        value = "macos" if platform.system() == "Darwin" else "unsupported"
    if value != "macos":
        raise ValueError("the new release pipeline supports native macOS builds only")
    if platform.system() != "Darwin":
        raise RuntimeError("a macOS bundle must be built on a native macOS host")
    return "macos"


def tauri_cli() -> list[str]:
    executable = UI / "node_modules" / ".bin" / (
        "tauri.cmd" if sys.platform == "win32" else "tauri"
    )
    if not executable.is_file():
        raise FileNotFoundError(
            "Tauri CLI is unavailable. Run `npm ci` in ui/ before packaging."
        )
    if sys.platform == "win32":
        return ["cmd", "/c", str(executable)]
    return [str(executable)]


def build_package(*, selected_platform: str, python: Path, skip_sidecar: bool = False) -> None:
    if not skip_sidecar:
        build_sidecar(python=python)
    subprocess.run(
        [*tauri_cli(), "build", "--bundles", "app"],
        cwd=SRC_TAURI,
        check=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", default="current")
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--skip-sidecar", action="store_true")
    args = parser.parse_args()
    build_package(
        selected_platform=target_platform(args.platform),
        # Do not resolve the virtualenv interpreter symlink: that discards the
        # venv context when the build helper invokes PyInstaller.
        python=args.python.absolute(),
        skip_sidecar=args.skip_sidecar,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
