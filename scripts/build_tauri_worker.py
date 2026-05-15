from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = ROOT / "packaging" / "tauri" / "XL_Translator_Worker.spec"
DIST_WORKER_DIR = ROOT / "dist" / "xl-translator-worker"
RESOURCE_WORKERS_DIR = ROOT / "src-tauri" / "resources" / "workers"
RESOURCE_WORKER_DIR = RESOURCE_WORKERS_DIR / "xl-translator-worker"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the Python worker used by Tauri.")
    parser.add_argument("--python", default=sys.executable)
    return parser.parse_args()


def run(command: list[str]) -> None:
    print("[INFO]", " ".join(command))
    subprocess.run(command, cwd=ROOT, check=True)


def main() -> int:
    args = parse_args()
    os.environ.setdefault(
        "PYINSTALLER_CONFIG_DIR",
        str(ROOT / ".runtime" / "pyinstaller-config"),
    )
    Path(os.environ["PYINSTALLER_CONFIG_DIR"]).mkdir(parents=True, exist_ok=True)

    if RESOURCE_WORKER_DIR.exists():
        shutil.rmtree(RESOURCE_WORKER_DIR)
    if DIST_WORKER_DIR.exists():
        shutil.rmtree(DIST_WORKER_DIR)

    run([args.python, "-m", "PyInstaller", "--noconfirm", str(SPEC_PATH)])

    if not DIST_WORKER_DIR.exists():
        raise FileNotFoundError(f"Expected worker bundle was not produced: {DIST_WORKER_DIR}")

    RESOURCE_WORKERS_DIR.mkdir(parents=True, exist_ok=True)
    preserve_symlinks = sys.platform == "darwin"
    shutil.copytree(
        DIST_WORKER_DIR,
        RESOURCE_WORKER_DIR,
        symlinks=preserve_symlinks,
    )
    print(f"[INFO] Worker resource ready: {RESOURCE_WORKER_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
