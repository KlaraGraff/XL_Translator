"""「PDF 处理组件」(pdf2docx + the PyMuPDF it brings) for the plugins page.

Two ways to get it, same as maple asked for:
* 一键安装: ``core.pdf_component_install`` downloads pinned wheels from PyPI,
  checks their SHA-256 and unpacks them under the app data directory.
* 选择安装位置: connect a pdf2docx the user installed themselves
  (``core.pdf2docx_plugin``); PyMuPDF is then loaded from that environment.

``core.pdf_component`` decides which one is used and loads PyMuPDF for
in-place replacement and page classification.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from loguru import logger

from core import pdf_component_install as installer
from core.pdf2docx_plugin import clear_pdf2docx_path, pdf2docx_status, prefer_managed, save_pdf2docx_path
from core.pdf_component import (
    SOURCE_MANAGED,
    SOURCE_MANUAL,
    active_converter,
    component_state,
    forget_failures,
)

OFFICIAL_PDF2DOCX_URL = "https://github.com/ArtifexSoftware/pdf2docx"

_JOB_LOCK = threading.Lock()
_JOB: dict[str, Any] = {"running": False, "phase": "", "done": 0, "total": 0, "error": "", "cancelled": False,
                        "finished": 0.0}
_CANCEL = threading.Event()


def install_progress() -> dict[str, Any]:
    with _JOB_LOCK:
        return dict(_JOB)


def _set_job(**values: Any) -> None:
    with _JOB_LOCK:
        _JOB.update(values)


def probe_status() -> dict[str, Any]:
    manual = pdf2docx_status()
    managed = installer.installed_info()
    active = active_converter()
    if active is not None:
        source, where = active
        state = "connected"
        path = str(where)
        message = "PDF 处理组件已安装。" if source == SOURCE_MANAGED else "PDF 处理组件已连接。"
    else:
        source = ""
        state = "invalid" if manual.get("state") == "invalid" else "missing"
        path = str(manual.get("path") or "")
        message = str(manual.get("message") or "")
    status: dict[str, Any] = {
        "state": state,
        "source": source,
        "path": path,
        "message": message,
        "managed_installed": bool(managed.get("installed")),
        "manual_path": str(manual.get("path") or "") if manual.get("state") == "connected" else "",
        "one_click_supported": bool(installer.manifest_files()),
        "download_bytes": installer.download_size(),
        "install": install_progress(),
        "inplace": False,
        "inplace_message": "",
        "pymupdf_version": "",
    }
    if state == "connected":
        component = component_state(refresh=True)
        status.update(
            inplace=component.available,
            inplace_message=component.message,
            pymupdf_version=component.version,
        )
    return status


def connect_pdf2docx(path: str) -> dict[str, Any]:
    save_pdf2docx_path(path)
    forget_failures()
    return probe_status()


def _run_install() -> None:
    def progress(phase: str, done: int, total: int) -> None:
        _set_job(phase=phase, done=done, total=total)

    try:
        installer.install(progress=progress, cancel=_CANCEL)
        prefer_managed()
        forget_failures()
        _set_job(running=False, phase="done", error="", finished=time.time())
    except installer.ComponentInstallError as exc:
        _set_job(running=False, phase="failed", error=str(exc), cancelled=_CANCEL.is_set(), finished=time.time())
    except Exception:  # noqa: BLE001 - every failure becomes a message on the page
        logger.exception("[PDF 处理组件] 一键安装失败")
        _set_job(running=False, phase="failed", error="组件安装失败。", cancelled=False, finished=time.time())


def start_install() -> dict[str, Any]:
    """后台开始一键安装；进度用 install_progress() 查。已在安装时不重复开始。"""
    if not installer.manifest_files():
        raise ValueError("当前系统不支持一键安装，可自行安装 pdf2docx 后选择它的安装位置。")
    with _JOB_LOCK:
        if _JOB["running"]:
            return dict(_JOB)
        _CANCEL.clear()
        _JOB.update(running=True, phase="download", done=0, total=installer.download_size(), error="",
                    cancelled=False, finished=0.0)
    threading.Thread(target=_run_install, name="pdf-component-install", daemon=True).start()
    return install_progress()


def cancel_install() -> dict[str, Any]:
    _CANCEL.set()
    return install_progress()


def uninstall(target: str = "") -> dict[str, Any]:
    """卸载一键安装的组件，或断开用户自己装的 pdf2docx（绝不删用户的安装）。"""
    if not target:
        active = active_converter()
        target = active[0] if active else SOURCE_MANUAL
    if target == SOURCE_MANAGED:
        if install_progress().get("running"):
            raise ValueError("组件正在安装，请稍候。")
        installer.uninstall()
    else:
        clear_pdf2docx_path()
    forget_failures()
    return probe_status()
