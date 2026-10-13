"""PDF 转 Word 组件（开源 pdf2docx 命令行工具）的发现、配置和调用。

Translator 不再自带 pdf2docx：用户自己装好（例如 ``pip install pdf2docx``），
在「插件」页连接它，PDF「自动组合」里能提取文字的页才会先转成 Word 再翻译。
没有连接时 ``core.pdf_word_route.word_route_available()`` 返回 False，这些页改用
保底方式。

和 ``core.ocr_engine`` 同一套做法：配置文件里用户选过的路径 > PATH 里的
``pdf2docx``。本模块在 ``core`` 下，不能从 ``api`` 导入。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

from core.app_paths import get_app_data_dir

_EXECUTABLE_NAMES = ("pdf2docx", "pdf2docx.exe")
_PROBE_TIMEOUT = 20.0
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


class Pdf2DocxPluginError(RuntimeError):
    """转换没有产出可用的 .docx（程序报错、超时或没写出文件）。"""


def default_pdf2docx_config_path() -> Path:
    """单独放一个目录：安装 CAD 插件包会整目录替换 ``plugins/cad-support``。"""
    return get_app_data_dir() / "plugins" / "pdf2docx-support" / "plugin.json"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _find_in_folder(folder: Path) -> Path | None:
    # pip 装在虚拟环境里时程序在 bin/（macOS、Linux）或 Scripts\（Windows）下。
    for name in _EXECUTABLE_NAMES:
        for candidate in (folder / name, folder / "bin" / name, folder / "Scripts" / name):
            if candidate.is_file():
                return candidate
    return None


def probe_pdf2docx(path: Path) -> bool:
    """Run ``pdf2docx --help`` and check it lists the ``convert`` command; never raises.

    pdf2docx 的命令行基于 python-fire，帮助文字写在 stderr 上，两路输出都看。
    """
    try:
        result = subprocess.run(
            [str(path), "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_PROBE_TIMEOUT,
            check=False,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    if result.returncode != 0:
        return False
    output = f"{result.stdout or ''}\n{result.stderr or ''}"
    return "pdf2docx" in output and "convert" in output


def _validate_pdf2docx(path: Path) -> bool:
    try:
        # pipx、Homebrew 常把程序装成指向真实文件的链接，跟随它。
        if path.is_symlink():
            path = path.resolve(strict=True)
        if not path.is_file() or path.name.lower() not in _EXECUTABLE_NAMES:
            return False
    except OSError:
        return False
    return probe_pdf2docx(path)


def _read_config(config_path: Path | None) -> dict[str, Any]:
    cfg = config_path or default_pdf2docx_config_path()
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _disconnected(config_path: Path | None) -> bool:
    """用户在插件页点过「断开」：连 PATH 里的 pdf2docx 也不用，直到重新选择安装位置。"""
    return bool(_read_config(config_path).get("disconnected"))


def _configured_raw(config_path: Path | None) -> Path | None:
    value = _read_config(config_path).get("path", "")
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
    if raw is None or not _validate_pdf2docx(raw):
        return None
    try:
        return raw.resolve(strict=True)
    except OSError:
        return None


def resolve_pdf2docx(config_path: Path | None = None) -> Path | None:
    """config 文件里用户选过的路径 > PATH 里的 pdf2docx；用户断开过就都不用。"""
    if _disconnected(config_path):
        return None
    configured = _configured_path(config_path)
    if configured is not None:
        return configured
    found = shutil.which("pdf2docx")
    if found:
        candidate = Path(found)
        if _validate_pdf2docx(candidate):
            try:
                return candidate.resolve(strict=True)
            except OSError:
                pass
    return None


def pdf2docx_status(config_path: Path | None = None) -> dict[str, Any]:
    raw = _configured_raw(config_path)
    if raw is not None and not _validate_pdf2docx(raw):
        # 之前保存过一条路径，但它现在用不了了——跟「从没配置过」区分开。
        return {
            "state": "invalid",
            "path": str(raw),
            "message": "之前连接的 PDF 转 Word 组件已不可用，请重新选择安装位置。",
        }
    if _disconnected(config_path):
        return {
            "state": "missing",
            "path": "",
            "message": "PDF 转 Word 组件已断开。",
        }
    candidate = resolve_pdf2docx(config_path)
    if candidate is None:
        return {
            "state": "missing",
            "path": "",
            "message": "未检测到 PDF 转 Word 组件（pdf2docx）。",
        }
    return {
        "state": "connected",
        "path": str(candidate),
        "message": "PDF 转 Word 组件已连接。",
    }


def save_pdf2docx_path(path: str, config_path: Path | None = None) -> dict[str, Any]:
    candidate = Path(path).expanduser()
    if candidate.is_dir():
        found = _find_in_folder(candidate)
        if found is not None:
            candidate = found
    try:
        candidate = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValueError("找不到所选的安装位置。") from exc
    if not _validate_pdf2docx(candidate):
        raise ValueError("所选程序不是可用的 PDF 转 Word 组件（pdf2docx）。")
    cfg = config_path or default_pdf2docx_config_path()
    _atomic_write(cfg, json.dumps({"path": str(candidate)}, ensure_ascii=False, indent=2))
    return pdf2docx_status(cfg)


def clear_pdf2docx_path(config_path: Path | None = None) -> None:
    """断开：忘掉保存的路径，并记下「不要用」，PATH 里装着的 pdf2docx 也不再自动连上。
    只改 Translator 自己的配置，绝不删除用户装的 pdf2docx。"""
    cfg = config_path or default_pdf2docx_config_path()
    _atomic_write(cfg, json.dumps({"path": "", "disconnected": True}, ensure_ascii=False, indent=2))


def _tail(text: str, limit: int = 600) -> str:
    lines = [line for line in (text or "").splitlines() if line.strip() and not line.lstrip().startswith(("INFO", "[INFO]"))]
    return "\n".join(lines)[-limit:]


def convert_with_plugin(exe: Path, pdf_path: Path, docx_path: Path, timeout: float) -> None:
    """Run ``<exe> convert <pdf> --docx_file=<docx>``; raise Pdf2DocxPluginError on failure.

    pdf2docx 每页在 stdout/stderr 打一行 INFO 进度，这些输出只在失败时摘一段写进
    错误信息。它在转换出错时可能照样以 0 退出，所以还要看输出文件是否真的写出来。
    """
    docx_path.parent.mkdir(parents=True, exist_ok=True)
    docx_path.unlink(missing_ok=True)
    try:
        result = subprocess.run(
            [str(exe), "convert", str(pdf_path), f"--docx_file={docx_path}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as exc:
        docx_path.unlink(missing_ok=True)
        raise Pdf2DocxPluginError(f"pdf2docx timed out after {timeout:.0f}s") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise Pdf2DocxPluginError(f"pdf2docx could not start: {exc}") from exc
    detail = _tail(f"{result.stderr or ''}\n{result.stdout or ''}")
    if result.returncode != 0:
        docx_path.unlink(missing_ok=True)
        raise Pdf2DocxPluginError(f"pdf2docx exited with code {result.returncode}: {detail}")
    try:
        produced = docx_path.is_file() and docx_path.stat().st_size > 0
    except OSError:
        produced = False
    if not produced:
        raise Pdf2DocxPluginError(f"pdf2docx did not write {docx_path.name}: {detail}")
