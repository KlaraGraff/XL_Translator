"""Tesseract OCR discovery and configuration.

Mirrors the discovery/config pattern of ``api.cad_plugin`` for the ODA
converter, but this module lives under ``core`` and must never import from
``api`` — it has no install lifecycle of its own, only "find the user's
Tesseract install, remember where it is, report whether it works".
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

from core.app_paths import get_app_data_dir

_EXECUTABLE_NAMES = ("tesseract", "tesseract.exe")
# 拉丁字母语系；装了其中任意一个就认为「能认拉丁字母」，供 UI 提示用。
_LATIN_LANGUAGES = {"eng", "fra", "spa", "deu", "ita", "por"}
_SUBPROCESS_TIMEOUT = 10.0


def _app_data_dir() -> Path:
    return get_app_data_dir()


_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def default_ocr_config_path() -> Path:
    """文字识别组件的配置文件，单独放一个目录：安装 CAD 插件包会整目录替换
    ``plugins/cad-support``，放在那里会被一起清掉。"""
    return _app_data_dir() / "plugins" / "ocr-support" / "ocr_plugin.json"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _find_in_folder(folder: Path) -> Path | None:
    for name in _EXECUTABLE_NAMES:
        for candidate in (folder / name, folder / "bin" / name):
            if candidate.is_file():
                return candidate
    return None


def _known_locations() -> list[Path]:
    import platform

    system = platform.system()
    if system == "Darwin":
        return [Path("/opt/homebrew/bin/tesseract"), Path("/usr/local/bin/tesseract")]
    if system == "Windows":
        paths = [
            Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
            Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
        ]
        local_app_data = str(os.environ.get("LOCALAPPDATA") or "").strip()
        if local_app_data:
            paths.append(Path(local_app_data) / "Programs" / "Tesseract-OCR" / "tesseract.exe")
        return paths
    return [Path("/usr/bin/tesseract")]


def tesseract_version(path: Path) -> str | None:
    """Run ``tesseract --version``; never raises, returns None on any failure."""
    try:
        result = subprocess.run(
            [str(path), "--version"],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
            check=False,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    first_line = (result.stdout or result.stderr or "").splitlines()[:1]
    if not first_line:
        return None
    match = re.search(r"(\d+(?:\.\d+){0,3})", first_line[0])
    return match.group(1) if match else None


def tesseract_languages(path: Path) -> list[str]:
    """Run ``tesseract --list-langs``; never raises, returns [] on any failure."""
    try:
        result = subprocess.run(
            [str(path), "--list-langs"],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
            check=False,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    # 第一行是 "List of available languages (N):" 这类标题，不是语言代码。
    return [line for line in lines if not line.lower().startswith("list of")]


def _validate_tesseract(path: Path) -> bool:
    try:
        # Homebrew 和多数 Linux 包管理器把 tesseract 装成指向真实文件的链接，跟随它。
        if path.is_symlink():
            path = path.resolve(strict=True)
        if not path.is_file() or path.name not in _EXECUTABLE_NAMES:
            return False
    except OSError:
        return False
    return tesseract_version(path) is not None


def _configured_raw(config_path: Path | None) -> Path | None:
    cfg = config_path or default_ocr_config_path()
    try:
        value = json.loads(cfg.read_text(encoding="utf-8")).get("path", "")
    except (OSError, ValueError, TypeError):
        return None
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if path.is_dir():
        found = _find_in_folder(path)
        if found is not None:
            path = found
    return path


def _configured_path(config_path: Path | None) -> Path | None:
    raw = _configured_raw(config_path)
    if raw is None or not _validate_tesseract(raw):
        return None
    try:
        return raw.resolve(strict=True)
    except OSError:
        return None


def resolve_tesseract(config_path: Path | None = None) -> Path | None:
    """config 文件里用户选过的路径 > PATH 里的 tesseract > 常见安装位置。"""
    configured = _configured_path(config_path)
    if configured is not None:
        return configured
    found = shutil.which("tesseract")
    if found:
        candidate = Path(found)
        if _validate_tesseract(candidate):
            try:
                return candidate.resolve(strict=True)
            except OSError:
                pass
    for candidate in _known_locations():
        if _validate_tesseract(candidate):
            try:
                return candidate.resolve(strict=True)
            except OSError:
                continue
    return None


def ocr_status(config_path: Path | None = None) -> dict[str, Any]:
    raw = _configured_raw(config_path)
    if raw is not None and not _validate_tesseract(raw):
        # 之前保存过一条路径，但它现在用不了了——跟「从没配置过」区分开，
        # 这样界面能提示「请重新选择」而不是「从未检测到」。
        return {
            "state": "invalid",
            "path": str(raw),
            "version": None,
            "languages": [],
            "has_latin": False,
            "message": "之前连接的文字识别组件已不可用，请重新选择安装位置。",
        }
    candidate = resolve_tesseract(config_path)
    if candidate is None:
        return {
            "state": "missing",
            "path": "",
            "version": None,
            "languages": [],
            "has_latin": False,
            "message": "未检测到文字识别组件（Tesseract OCR）。",
        }
    version = tesseract_version(candidate)
    languages = tesseract_languages(candidate)
    has_latin = any(language in _LATIN_LANGUAGES for language in languages)
    return {
        "state": "connected",
        "path": str(candidate),
        "version": version,
        "languages": languages,
        "has_latin": has_latin,
        "message": "文字识别组件已连接。",
    }


def save_tesseract_path(path: str, config_path: Path | None = None) -> dict[str, Any]:
    candidate = Path(path).expanduser()
    if candidate.is_dir():
        found = _find_in_folder(candidate)
        if found is not None:
            candidate = found
    try:
        candidate = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValueError("找不到所选的安装位置。") from exc
    if not _validate_tesseract(candidate):
        raise ValueError("所选程序不是可用的文字识别组件（Tesseract OCR）。")
    cfg = config_path or default_ocr_config_path()
    _atomic_write(cfg, json.dumps({"path": str(candidate)}, ensure_ascii=False, indent=2))
    return ocr_status(cfg)


def clear_tesseract_path(config_path: Path | None = None) -> None:
    cfg = config_path or default_ocr_config_path()
    cfg.unlink(missing_ok=True)
