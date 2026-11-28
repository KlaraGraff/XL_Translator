"""扩展页的「PDF 组件」大类：「全部安装」按顺序装上缺的几项（maple 2026-11-27）。

三项：PDF 处理组件（PyMuPDF + pdf2docx）、段落重排组件（BabelDOC）、文字识别组件（PP-OCR）。
后两项要用第一项里的 NumPy、OpenCV、PyMuPDF，所以第一项先装，它装不上就停下；后两项各装各的，
一项没装好不连累另一项，最后把没装好的列出来。每一项展开后也能单独装、重试、卸载
（api/pdf2docx_plugin.py、api/babeldoc_plugin.py、api/ocr_plugin.py），已下载的文件留着，重试接着用。
"""
from __future__ import annotations

import threading
from typing import Any

from loguru import logger

from api.component_install_job import InstallJob
from core import babeldoc_component_install as babeldoc_installer
from core import pdf_component_install as pdf_installer
from core import ppocr_component_install as ocr_installer

ITEM_PDF = "pdf"
ITEM_BABELDOC = "babeldoc"
ITEM_OCR = "ocr"
ITEM_ORDER = (ITEM_PDF, ITEM_BABELDOC, ITEM_OCR)
ITEM_NAMES = {ITEM_PDF: "PDF 处理组件", ITEM_BABELDOC: "段落重排组件", ITEM_OCR: "文字识别组件"}


class BundleInstallError(RuntimeError):
    """「全部安装」有一项没装好；消息是给用户看的一句话。"""


_RESULTS_LOCK = threading.Lock()
# 这一轮「全部安装」每一项的结果：item → {"status": pending|running|done|failed|cancelled, "error": ...}
_RESULTS: dict[str, dict[str, str]] = {}


def _set_result(item: str, status: str, error: str = "") -> None:
    with _RESULTS_LOCK:
        _RESULTS[item] = {"status": status, "error": error}


def _item_missing(item: str) -> bool:
    if item == ITEM_PDF:
        from core.pdf_component import active_converter
        from core.ppocr_component import numpy_site_dir

        info = pdf_installer.installed_info()
        return active_converter() is None or numpy_site_dir() is None or bool(info.get("outdated"))
    if item == ITEM_BABELDOC:
        info = babeldoc_installer.installed_info()
        return not info["installed"] or bool(info.get("outdated"))
    info = ocr_installer.installed_info()
    return not info["installed"] or bool(info.get("outdated"))


def _item_files(item: str) -> tuple[Any, ...]:
    return {ITEM_PDF: pdf_installer.manifest_files, ITEM_BABELDOC: babeldoc_installer.manifest_files,
            ITEM_OCR: ocr_installer.manifest_files}[item]()


def _item_size(item: str) -> int:
    return sum(int(getattr(file, "size", 0) or 0) for file in _item_files(item))


def missing_items() -> list[str]:
    return [item for item in ITEM_ORDER if _item_missing(item)]


def _install_item(item: str, *, progress, cancel) -> None:
    if item == ITEM_PDF:
        from core.pdf2docx_plugin import prefer_managed
        from core.pdf_component import forget_failures

        pdf_installer.install(progress=progress, cancel=cancel)
        prefer_managed()
        forget_failures()
    elif item == ITEM_BABELDOC:
        babeldoc_installer.install(progress=progress, cancel=cancel)
    else:
        from api.ocr_plugin import _remove_old_tesseract

        ocr_installer.install(progress=progress, cancel=cancel)
        _remove_old_tesseract()


def _install_all(*, progress=None, cancel: threading.Event | None = None) -> dict[str, Any]:
    plan = missing_items()
    with _RESULTS_LOCK:
        _RESULTS.clear()
        _RESULTS.update({item: {"status": "pending", "error": ""} for item in plan})
    sizes = {item: _item_size(item) for item in plan}
    total = sum(sizes.values())
    base = 0
    failed: list[str] = []
    for item in plan:
        if cancel is not None and cancel.is_set():
            _set_result(item, "cancelled")
            continue
        _set_result(item, "running")

        def item_progress(phase: str, done: int, _total: int, *, item: str = item, base: int = base) -> None:
            if progress is not None:
                progress(f"{item}:{phase}", base + done if phase == "download" else base + sizes[item], total)

        try:
            _install_item(item, progress=item_progress, cancel=cancel)
        except Exception as exc:  # noqa: BLE001 - each item has its own error type; all become a message
            cancelled = cancel is not None and cancel.is_set()
            message = str(exc) or "组件安装失败。"
            if not cancelled and not isinstance(exc, RuntimeError):
                logger.exception(f"[PDF 组件] {ITEM_NAMES[item]}安装出错")
                message = "组件安装失败。"
            _set_result(item, "cancelled" if cancelled else "failed", message)
            if cancelled:
                raise BundleInstallError("已取消安装。") from exc
            if item == ITEM_PDF:
                # 另外两项要用它里面的程序库，装不上就不往下装了。
                for rest in plan[1:]:
                    _set_result(rest, "pending")
                raise BundleInstallError(f"PDF 处理组件没有装好：{message}") from exc
            failed.append(item)
        else:
            _set_result(item, "done")
        base += sizes[item]
    if failed:
        names = "、".join(ITEM_NAMES[item] for item in failed)
        raise BundleInstallError(f"{names}没有装好，其余组件已装好。展开后可以单独重试。")
    return status()


_JOB = InstallJob("PDF 组件", lambda: _install_all, lambda: BundleInstallError)


def running() -> bool:
    return _JOB.running()


def install_progress() -> dict[str, Any]:
    with _RESULTS_LOCK:
        items = {item: dict(value) for item, value in _RESULTS.items()}
    return {**_JOB.progress(), "items": items}


def status() -> dict[str, Any]:
    missing = missing_items()
    unsupported = [item for item in missing if not _item_files(item)]
    return {
        "missing": missing,
        "download_bytes": sum(_item_size(item) for item in missing),
        "one_click_supported": not unsupported,
        "unsupported": unsupported,
        "install": install_progress(),
    }


def _item_running() -> str:
    from api import babeldoc_plugin, ocr_plugin, pdf2docx_plugin

    for name, module in (("PDF 处理组件", pdf2docx_plugin), ("段落重排组件", babeldoc_plugin),
                         ("文字识别组件", ocr_plugin)):
        if module.install_progress().get("running"):
            return name
    return ""


def start_install() -> dict[str, Any]:
    """后台按顺序装上缺的几项；进度用 install_progress() 查。"""
    busy = _item_running()
    if busy:
        raise ValueError(f"{busy}正在安装，请稍候。")
    missing = missing_items()
    if not missing:
        raise ValueError("PDF 组件都已安装。")
    unsupported = [item for item in missing if not _item_files(item)]
    if unsupported:
        names = "、".join(ITEM_NAMES[item] for item in unsupported)
        raise ValueError(f"当前系统不支持一键安装{names}。")
    return {**_JOB.start(sum(_item_size(item) for item in missing)), "items": {}}


def cancel_install() -> dict[str, Any]:
    _JOB.cancel()
    return install_progress()
