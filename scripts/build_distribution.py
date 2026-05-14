from __future__ import annotations

import argparse
import platform
import stat
import shutil
import sys
import zipfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app_meta import (  # noqa: E402
    DEFAULT_DISTRIBUTION_OUTPUT_NAME,
    build_versioned_distribution_zip_name,
)

PROJECT_FILES = {
    "app.py",
    "app_meta.py",
    "config.py",
    "settings.py",
    "requirements.txt",
    "README_首次使用.txt",
}

PLATFORM_PROJECT_FILES = {
    "macos": {
        "启动应用.command",
    },
    "windows": {
        "启动应用.bat",
        "分发应用.bat",
    },
}

PROJECT_DIRS = {
    "assets",
    "core",
    "engines",
    "ui",
    "scripts",
    ".streamlit",
}

PLATFORM_PROJECT_DIRS = {
    "macos": set(),
    "windows": {
        "runtime",
    },
}

PLATFORM_SCRIPT_FILES = {
    "macos": {
        Path("scripts") / "start_macos.command",
        Path("scripts") / "launch_silent_macos.sh",
    },
    "windows": {
        Path("scripts") / "start_windows.bat",
        Path("scripts") / "launch_silent_windows.ps1",
    },
}

ADDITIONAL_ROOT_FILES = {
    Path("docs") / "CHANGELOG.md": Path("版本更新日志.md"),
}

EXCLUDE_DIRS = {
    ".git",
    ".venv",
    ".claude",
    ".cursor",
    ".idea",
    ".vscode",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "logs",
    "build",
    "dist",
    "output",
    "tmp",
}

EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".log", ".tmp", ".bak"}
SENSITIVE_NAME_PREFIXES = {".env"}
EXCLUDE_NAMES = {
    "settings.json",
    "keys.json",
    "tm.db",
    "app.log",
    ".env",
    ".env.local",
    "secrets.toml",
    "Thumbs.db",
    ".DS_Store",
}

EXECUTABLE_SUFFIXES = {".command", ".sh"}
SUPPORTED_PLATFORMS = {"macos", "windows"}


def root_dir() -> Path:
    return ROOT_DIR


def should_skip(path: Path) -> bool:
    if path.name in EXCLUDE_NAMES:
        return True
    if any(path.name.startswith(prefix) for prefix in SENSITIVE_NAME_PREFIXES):
        return True
    if path.suffix.lower() in EXCLUDE_SUFFIXES:
        return True
    return any(part in EXCLUDE_DIRS for part in path.parts)


def copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def copy_tree(src_dir: Path, dst_dir: Path) -> None:
    for item in src_dir.rglob("*"):
        rel = item.relative_to(src_dir)
        target = dst_dir / rel
        if should_skip(rel):
            continue
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif item.is_file():
            copy_file(item, target)


def current_platform_name() -> str:
    system = platform.system().lower()
    if system == "windows":
        return "windows"
    return "macos"


def normalize_platform(raw_platform: str) -> str:
    platform_name = str(raw_platform or "").strip().lower()
    if platform_name in {"current", "auto"}:
        return current_platform_name()
    if platform_name in SUPPORTED_PLATFORMS:
        return platform_name
    supported = ", ".join(sorted([*SUPPORTED_PLATFORMS, "current"]))
    raise ValueError(f"Unsupported platform={raw_platform!r}. Supported: {supported}")


def iter_project_files(target_platform: str) -> list[Path]:
    files = {Path(name) for name in PROJECT_FILES}
    files.update(Path(name) for name in PLATFORM_PROJECT_FILES[target_platform])
    files.update(PLATFORM_SCRIPT_FILES[target_platform])
    return sorted(files)


def iter_project_dirs(target_platform: str) -> list[Path]:
    dirs = {Path(name) for name in PROJECT_DIRS}
    dirs.update(Path(name) for name in PLATFORM_PROJECT_DIRS[target_platform])
    return sorted(dirs)


def ensure_windows_runtime_placeholder(dist_root: Path) -> None:
    runtime_python = dist_root / "runtime" / "python"
    runtime_python.mkdir(parents=True, exist_ok=True)
    if any(runtime_python.iterdir()):
        return
    (runtime_python / "README.txt").write_text(
        (
            "Optional bundled Python runtime directory.\n"
            "The Windows launcher can also use a system Python 3.10+ installation.\n"
        ),
        encoding="utf-8",
    )

def should_mark_executable(rel_path: Path) -> bool:
    return rel_path.suffix.lower() in EXECUTABLE_SUFFIXES


def zip_mode_for(rel_path: Path, is_dir: bool) -> int:
    permissions = 0o755 if (is_dir or should_mark_executable(rel_path)) else 0o644
    file_type = stat.S_IFDIR if is_dir else stat.S_IFREG
    return file_type | permissions


def build_distribution_zip(source_dir: Path, output_zip: Path) -> Path:
    output_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for item in sorted(source_dir.rglob("*")):
            rel = item.relative_to(source_dir)
            arcname = rel.as_posix()
            info_name = f"{arcname}/" if item.is_dir() else arcname
            info = zipfile.ZipInfo.from_file(item, arcname=info_name)
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = zip_mode_for(rel, item.is_dir()) << 16

            if item.is_dir():
                zf.writestr(info, "")
                continue

            with item.open("rb") as f:
                zf.writestr(info, f.read())
    return output_zip


def build_distribution(
    output_name: str,
    make_zip: bool,
    skip_rebuild: bool = False,
    version_zip: bool = False,
    target_platform: str = "current",
) -> Path:
    root = root_dir()
    target_platform = normalize_platform(target_platform)
    dist_dir = root / "dist"
    dist_root = dist_dir / output_name

    dist_dir.mkdir(parents=True, exist_ok=True)

    # ── 分发目录生成 ──────────────────────────────────────────
    if skip_rebuild and dist_root.exists():
        print(f"[INFO] 分发目录已存在，跳过重建: {dist_root}")
    else:
        if dist_root.exists():
            shutil.rmtree(dist_root)

        dist_root.mkdir(parents=True, exist_ok=True)

        for rel_path in iter_project_files(target_platform):
            src = root / rel_path
            if src.exists() and src.is_file():
                copy_file(src, dist_root / rel_path)

        for dirname in iter_project_dirs(target_platform):
            src_dir = root / dirname
            if src_dir.exists() and src_dir.is_dir():
                copy_tree(src_dir, dist_root / dirname)

        for src_rel, dst_rel in ADDITIONAL_ROOT_FILES.items():
            src = root / src_rel
            if src.exists() and src.is_file():
                copy_file(src, dist_root / dst_rel)

        if target_platform == "windows":
            ensure_windows_runtime_placeholder(dist_root)
        print(f"[INFO] 分发目录已生成: {dist_root}")

    # ── 压缩包生成 ────────────────────────────────────────────
    if make_zip:
        if version_zip:
            zip_name = build_versioned_distribution_zip_name(output_name)
            old_zip = dist_dir / f"{zip_name}.zip"
            if old_zip.exists():
                old_zip.unlink()
        else:
            zip_name = output_name
            # 非版本后缀模式：清理同名旧 zip
            old_zip = dist_dir / f"{output_name}.zip"
            if old_zip.exists():
                old_zip.unlink()

        zip_path = dist_dir / f"{zip_name}.zip"
        archive = build_distribution_zip(dist_root, zip_path)
        print(f"[INFO] 已生成 zip: {archive}")

    return dist_root


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="构建项目分发目录")
    parser.add_argument(
        "--platform",
        choices=sorted([*SUPPORTED_PLATFORMS, "current"]),
        default="current",
        help="目标平台：current / macos / windows",
    )
    parser.add_argument(
        "--output-name",
        default=DEFAULT_DISTRIBUTION_OUTPUT_NAME,
        help="分发目录名称（生成在 dist 下）",
    )
    parser.add_argument(
        "--zip",
        action="store_true",
        help="同时生成 zip 压缩包",
    )
    parser.add_argument(
        "--skip-rebuild",
        action="store_true",
        help="若分发目录已存在则跳过重建",
    )
    parser.add_argument(
        "--version-zip",
        action="store_true",
        help="在 zip 文件名中加入当前版本号（避免覆盖历史版本）",
    )
    parser.add_argument(
        "--timestamp-zip",
        action="store_true",
        dest="legacy_version_zip",
        help=argparse.SUPPRESS,
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    build_distribution(
        output_name=args.output_name,
        make_zip=args.zip,
        skip_rebuild=args.skip_rebuild,
        version_zip=(args.version_zip or args.legacy_version_zip),
        target_platform=args.platform,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
