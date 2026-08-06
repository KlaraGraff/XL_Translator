"""Build a platform-native Tauri bundle with its frozen Python sidecar."""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC_TAURI = ROOT / "src-tauri"
UI = ROOT / "ui"
DIST_DIR = ROOT / "dist"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_tauri_sidecar import build_sidecar  # noqa: E402

SUPPORTED_PLATFORMS = {"macos", "windows"}


def target_platform(raw: str) -> str:
    value = raw.strip().lower()
    if value == "current":
        system = platform.system()
        if system == "Darwin":
            value = "macos"
        elif system == "Windows":
            value = "windows"
        else:
            value = "unsupported"
    if value not in SUPPORTED_PLATFORMS:
        raise ValueError(
            "the release pipeline supports native macOS and Windows builds only"
        )
    system = platform.system()
    if value == "macos" and system != "Darwin":
        raise RuntimeError("a macOS bundle must be built on a native macOS host")
    if value == "windows" and system != "Windows":
        raise RuntimeError("a Windows bundle must be built on a native Windows host")
    return value


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


def package_windows_installer(*, dist_dir: Path = DIST_DIR) -> Path:
    """Rename the Tauri-built NSIS installer and write its sha256 sidecar.

    Windows has no local codesigning/notarization step in this pipeline, so
    (unlike macOS's separate shell packaging script) this one function does
    all of the post-build packaging: locate the single NSIS `.exe` Tauri
    produced, move it into `dist/` under the release asset name, and write a
    `<digest>  <filename>` sha256 file next to it.
    """
    from app_meta import windows_installer_basename

    nsis_dir = SRC_TAURI / "target" / "release" / "bundle" / "nsis"
    candidates = sorted(nsis_dir.glob("*.exe"))
    if not candidates:
        raise FileNotFoundError(f"No NSIS installer was produced under {nsis_dir}")
    if len(candidates) > 1:
        names = ", ".join(candidate.name for candidate in candidates)
        raise RuntimeError(f"Expected exactly one NSIS installer, found: {names}")
    built_installer = candidates[0]

    dist_dir.mkdir(parents=True, exist_ok=True)
    installer_name = f"{windows_installer_basename()}.exe"
    installer_path = dist_dir / installer_name
    if installer_path.exists():
        installer_path.unlink()
    shutil.copyfile(built_installer, installer_path)

    digest = hashlib.sha256(installer_path.read_bytes()).hexdigest()
    sha256_path = dist_dir / f"{installer_name}.sha256"
    sha256_path.write_text(f"{digest}  {installer_name}\n", encoding="utf-8")

    github_env = os.environ.get("GITHUB_ENV")
    if github_env:
        with open(github_env, "a", encoding="utf-8") as handle:
            handle.write(f"WINDOWS_INSTALLER=dist/{installer_name}\n")
            handle.write(f"WINDOWS_INSTALLER_SHA256=dist/{installer_name}.sha256\n")

    return installer_path


def build_package(*, selected_platform: str, python: Path, skip_sidecar: bool = False) -> None:
    if not skip_sidecar:
        build_sidecar(python=python)
    if selected_platform == "macos":
        subprocess.run(
            [*tauri_cli(), "build", "--bundles", "app"],
            cwd=SRC_TAURI,
            check=True,
        )
    elif selected_platform == "windows":
        subprocess.run(
            [*tauri_cli(), "build", "--bundles", "nsis"],
            cwd=SRC_TAURI,
            check=True,
        )
        package_windows_installer()
    else:
        raise ValueError(f"unsupported platform: {selected_platform}")


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
