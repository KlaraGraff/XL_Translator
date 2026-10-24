"""「文字识别组件」for the plugins page.

一键安装装的是识别模型（PP-OCR，中文、日文和西文都认，``core.ppocr_component_install``），
在单独的子进程里运行，NumPy / OpenCV 用「PDF 处理组件」里的那份。

以前的一键安装装的是 Tesseract（只认西文，``core.ocr_component_install``），用户也可以连接
自己装的 Tesseract（选择安装位置）。没装识别模型时原位替换照旧用它们；装了识别模型就用模型，
一键安装成功后删掉以前一键安装的 Tesseract（「更新」）。``core.ocr_engine`` 决定用哪个 Tesseract。
"""
from __future__ import annotations

from typing import Any

from api.component_install_job import InstallJob
from core import ocr_component_install as tesseract_installer
from core import ppocr_component
from core import ppocr_component_install as installer
from core.ocr_engine import clear_tesseract_path, ocr_status, save_tesseract_path

OFFICIAL_OCR_URL = "https://github.com/PaddlePaddle/PaddleOCR"


def _remove_old_tesseract() -> None:
    """识别模型装好了：以前一键安装的 Tesseract 用不到了，删掉（用户自己装的不动）。"""
    if tesseract_installer.installed_executable() is not None:
        tesseract_installer.uninstall()


_JOB = InstallJob("文字识别组件", lambda: installer.install, lambda: installer.ComponentInstallError,
                  _remove_old_tesseract)


def install_progress() -> dict[str, Any]:
    return _JOB.progress()


def probe_status() -> dict[str, Any]:
    """组件状态：装了识别模型时 engine 是 "ppocr"；只有 Tesseract 时是 "tesseract"（只认西文，可更新）。"""
    status = ocr_status()
    models = installer.installed_info()
    status["managed_installed"] = bool(models["installed"] or status.get("managed_installed"))
    status["one_click_supported"] = bool(installer.manifest_files())
    status["download_bytes"] = installer.download_size()
    # 识别模型用「PDF 处理组件」里的 NumPy、OpenCV：没装它时先装它
    status["needs_pdf_component"] = ppocr_component.numpy_site_dir() is None
    if models["installed"]:
        status.update(state="connected", source="managed", engine="ppocr", path=models["path"],
                      version=models["models"], message="文字识别组件已安装。")
    else:
        status["engine"] = "tesseract" if status.get("state") == "connected" else ""
    status["install"] = install_progress()
    return status


def connect_tesseract(path: str) -> dict[str, Any]:
    save_tesseract_path(path)
    return probe_status()


def start_install() -> dict[str, Any]:
    """后台开始一键安装；进度用 install_progress() 查。已在安装时不重复开始。"""
    if not installer.manifest_files():
        raise ValueError("当前系统不支持一键安装。")
    if ppocr_component.numpy_site_dir() is None:
        raise ValueError("请先安装「PDF 处理组件」。")
    return _JOB.start(installer.download_size())


def cancel_install() -> dict[str, Any]:
    return _JOB.cancel()


def uninstall(target: str = "") -> dict[str, Any]:
    """卸载一键安装的组件，或忘掉用户选过的安装位置（绝不删用户自己的安装）。"""
    if not target:
        target = str(probe_status().get("source") or "manual")
    if target == "managed":
        if _JOB.running():
            raise ValueError("组件正在安装，请稍候。")
        installer.uninstall()
        tesseract_installer.uninstall()
    else:
        clear_tesseract_path()
    return probe_status()
