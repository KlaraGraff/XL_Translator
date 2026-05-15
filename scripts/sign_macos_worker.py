"""Ad-hoc sign the PyInstaller worker bundle before Tauri packages it.

macOS Gatekeeper validates executable code nested inside an app bundle. The
Python worker is shipped as Tauri resources, so we sign its Mach-O files before
Tauri copies them into the final .app and signs the outer application bundle.
"""
from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WORKER_DIR = ROOT / "src-tauri" / "resources" / "workers" / "xl-translator-worker"

MACHO_MAGICS = {
    b"\xfe\xed\xfa\xce",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
    b"\xca\xfe\xba\xbf",
    b"\xbf\xba\xfe\xca",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ad-hoc sign macOS worker Mach-O files.")
    parser.add_argument("--worker-dir", type=Path, default=DEFAULT_WORKER_DIR)
    parser.add_argument("--verify", action="store_true", help="Verify every signed file.")
    return parser.parse_args()


def is_macho(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        with path.open("rb") as file:
            return file.read(4) in MACHO_MAGICS
    except OSError:
        return False


def find_macho_files(root: Path) -> list[Path]:
    paths: list[Path] = []
    seen: set[Path] = set()
    for path in root.rglob("*"):
        if not is_macho(path):
            continue
        real_path = path.resolve()
        if real_path in seen:
            continue
        seen.add(real_path)
        paths.append(path)
    return sorted(paths, key=lambda path: len(path.parts), reverse=True)


def find_framework_dirs(root: Path) -> list[Path]:
    frameworks = [
        path
        for path in root.rglob("*.framework")
        if path.is_dir() and not path.is_symlink()
    ]
    return sorted(frameworks, key=lambda path: len(path.parts), reverse=True)


def validate_framework_symlinks(framework: Path) -> None:
    versions = framework / "Versions"
    if not versions.exists():
        return

    current = versions / "Current"
    main_binary = framework / framework.stem
    resources = framework / "Resources"
    broken_paths = [
        path
        for path in (current, main_binary, resources)
        if path.exists() and not path.is_symlink()
    ]
    if broken_paths:
        relative_paths = ", ".join(str(path.relative_to(framework)) for path in broken_paths)
        raise RuntimeError(
            f"{framework} has expanded framework links ({relative_paths}). "
            "Rebuild the worker with scripts/build_tauri_worker.py so macOS "
            "framework symlinks are preserved."
        )


def run(command: list[str]) -> None:
    print("[INFO]", " ".join(command))
    subprocess.run(command, check=True)


def main() -> int:
    args = parse_args()
    if platform.system() != "Darwin":
        print("[INFO] macOS code signing skipped on non-Darwin platform.")
        return 0
    if shutil.which("codesign") is None:
        raise FileNotFoundError("codesign was not found on PATH.")

    worker_dir = args.worker_dir.resolve()
    if not worker_dir.exists():
        raise FileNotFoundError(f"Worker bundle was not found: {worker_dir}")

    macho_files = find_macho_files(worker_dir)
    if not macho_files:
        raise FileNotFoundError(f"No Mach-O files found in worker bundle: {worker_dir}")
    framework_dirs = find_framework_dirs(worker_dir)
    for framework in framework_dirs:
        validate_framework_symlinks(framework)

    for path in macho_files:
        run(["codesign", "--force", "--sign", "-", "--timestamp=none", str(path)])
        if args.verify:
            run(["codesign", "--verify", "--verbose=2", str(path)])
    for framework in framework_dirs:
        run(["codesign", "--force", "--sign", "-", "--timestamp=none", str(framework)])
        if args.verify:
            run(["codesign", "--verify", "--verbose=2", str(framework)])

    print(
        f"[INFO] Signed {len(macho_files)} Mach-O files and "
        f"{len(framework_dirs)} frameworks in {worker_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
