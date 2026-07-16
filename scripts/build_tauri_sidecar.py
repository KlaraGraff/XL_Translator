"""Build the no-Qt PyInstaller onedir sidecar used by the Tauri installer."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "packaging" / "sidecar" / "translator_sidecar.spec"
RESOURCE_PARENT = ROOT / "src-tauri" / "resources" / "sidecar"


def sidecar_executable(sidecar_dir: Path) -> Path:
    name = "translator-sidecar.exe" if sys.platform == "win32" else "translator-sidecar"
    return sidecar_dir / name


def build_sidecar(*, python: Path, output_parent: Path = RESOURCE_PARENT) -> Path:
    if not SPEC_PATH.is_file():
        raise FileNotFoundError(f"Missing PyInstaller spec: {SPEC_PATH}")

    target_dir = output_parent / "translator-sidecar"
    if target_dir.exists():
        shutil.rmtree(target_dir)
    output_parent.mkdir(parents=True, exist_ok=True)

    work_path = ROOT / ".runtime" / "package" / "pyinstaller-sidecar-work"
    command = [
        str(python),
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath",
        str(output_parent),
        "--workpath",
        str(work_path),
        str(SPEC_PATH),
    ]
    subprocess.run(command, cwd=ROOT, check=True)
    executable = sidecar_executable(target_dir)
    if not executable.is_file():
        raise RuntimeError(f"PyInstaller did not produce the sidecar executable: {executable}")
    return executable


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--output-parent", type=Path, default=RESOURCE_PARENT)
    args = parser.parse_args()
    # Keep the virtualenv executable path intact.  Resolving its symlink turns
    # it into the base interpreter and makes PyInstaller unavailable.
    executable = build_sidecar(python=args.python.absolute(), output_parent=args.output_parent.resolve())
    print(executable)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
