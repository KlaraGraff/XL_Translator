"""「文字识别组件」(Tesseract OCR) for the plugins page.

Two ways to get it, same as the PDF component:
* 一键安装: ``core.ocr_component_install`` downloads pinned conda-forge builds
  and tessdata_fast language files, checks them and unpacks them under the app
  data directory.
* 选择安装位置: remember a Tesseract the user installed themselves.

``core.ocr_engine`` decides which one is used.
"""
from __future__ import annotations

from typing import Any

from api.component_install_job import InstallJob
from core import ocr_component_install as installer
from core.ocr_engine import clear_tesseract_path, ocr_status, save_tesseract_path

OFFICIAL_OCR_URL = "https://github.com/tesseract-ocr/tesseract"

# 一键装好就用装好的：之前选过的安装位置不再优先。
_JOB = InstallJob("文字识别组件", lambda: installer.install, lambda: installer.ComponentInstallError,
                  clear_tesseract_path)


def install_progress() -> dict[str, Any]:
    return _JOB.progress()


def probe_status() -> dict[str, Any]:
    return {**ocr_status(), "install": install_progress()}


def connect_tesseract(path: str) -> dict[str, Any]:
    save_tesseract_path(path)
    return probe_status()


def start_install() -> dict[str, Any]:
    """后台开始一键安装；进度用 install_progress() 查。已在安装时不重复开始。"""
    if installer.manifest() is None:
        raise ValueError("当前系统不支持一键安装，可自行安装 Tesseract OCR 后选择它的安装位置。")
    return _JOB.start(installer.download_size())


def cancel_install() -> dict[str, Any]:
    return _JOB.cancel()


def uninstall(target: str = "") -> dict[str, Any]:
    """卸载一键安装的组件，或忘掉用户选过的安装位置（绝不删用户自己的安装）。"""
    if not target:
        target = str(ocr_status().get("source") or "manual")
    if target == "managed":
        if _JOB.running():
            raise ValueError("组件正在安装，请稍候。")
        installer.uninstall()
    else:
        clear_tesseract_path()
    return probe_status()
