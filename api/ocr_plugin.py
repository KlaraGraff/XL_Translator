"""「文字识别组件」for the plugins page.

一键安装装的是识别模型（PP-OCR，中文、日文和西文都认，``core.ppocr_component_install``），
在单独的子进程里运行，NumPy / OpenCV 用「PDF 处理组件」里的那份。

以前的一键安装装的是 Tesseract（只认西文，``core.ocr_component_install``），用户也可以连接
自己装的 Tesseract（选择安装位置）。没装识别模型时原位替换照旧用它们；装了识别模型就用模型，
一键安装成功后删掉以前一键安装的 Tesseract（「更新」）。``core.ocr_engine`` 决定用哪个 Tesseract。
"""
from __future__ import annotations

import platform
import sys
from typing import Any

from api.component_install_job import InstallJob
from core import ocr_component_install as tesseract_installer
from core import ppocr_component
from core import ppocr_component_install as installer
from core import pdf_component_install as pdf_installer
from core import pdf_component
from core.pdf2docx_plugin import prefer_managed
from core.ocr_engine import clear_tesseract_path, ocr_status, save_tesseract_path

OFFICIAL_OCR_URL = "https://github.com/PaddlePaddle/PaddleOCR"


def _remove_old_tesseract() -> None:
    """识别模型装好了：以前一键安装的 Tesseract 用不到了，删掉（用户自己装的不动）。"""
    if tesseract_installer.installed_executable() is not None:
        tesseract_installer.uninstall()


def _runtime_diagnostics() -> dict[str, str]:
    return {
        "runtime_python": platform.python_version(),
        "runtime_platform": sys.platform,
        "runtime_architecture": platform.machine().lower(),
    }


def _unsupported_reason(label: str, manifest: tuple[Any, ...]) -> str:
    if manifest:
        return ""
    details = _runtime_diagnostics()
    return (f"当前运行环境不支持{label}一键安装，需要 Python 3.11 和 macOS arm64、Windows x64 或 Linux x64；"
            f"当前为 Python {details['runtime_python']} / {details['runtime_platform']} "
            f"{details['runtime_architecture']}。")


def _install_with_dependency(*, progress=None, cancel=None) -> dict[str, Any]:
    """缺少 NumPy/OpenCV 时，把 PDF 组件安装作为同一个可取消的安装链路。"""
    from api import pdf2docx_plugin

    pdf_files = pdf_installer.manifest_files()
    pdf_total = pdf_installer.download_size()
    ocr_total = installer.download_size()
    combined_total = pdf_total + ocr_total
    needs_pdf = ppocr_component.numpy_site_dir() is None
    if needs_pdf:
        if not pdf_files:
            raise installer.ComponentInstallError(_unsupported_reason("PDF 处理组件", pdf_files))
        if pdf2docx_plugin.install_progress().get("running"):
            raise installer.ComponentInstallError("PDF 处理组件正在安装，请稍候。")

        def dependency_progress(phase: str, done: int, _total: int) -> None:
            if not progress:
                return
            if phase == "download":
                progress("dependency_download", done, combined_total)
            elif phase == "extract":
                progress("dependency_extract", pdf_total, combined_total)
            elif phase == "verify":
                progress("dependency_verify", pdf_total, combined_total)

        try:
            pdf_installer.install(progress=dependency_progress, cancel=cancel)
        except pdf_installer.ComponentInstallError as exc:
            raise installer.ComponentInstallError(str(exc)) from exc
        # 让 ppocr_component.numpy_site_dir() 立即看到刚装好的目录，并清掉失败缓存。
        prefer_managed()
        pdf_component.forget_failures()

    def ocr_progress(phase: str, done: int, _total: int) -> None:
        if not progress:
            return
        if phase == "download":
            progress("download", pdf_total + done, combined_total)
        elif phase == "extract":
            progress("extract", pdf_total, combined_total)
        elif phase == "verify":
            progress("verify", pdf_total, combined_total)

    try:
        return installer.install(progress=ocr_progress, cancel=cancel)
    except installer.ComponentInstallError:
        raise


_JOB = InstallJob("文字识别组件", lambda: _install_with_dependency, lambda: installer.ComponentInstallError,
                  _remove_old_tesseract)


def install_progress() -> dict[str, Any]:
    return _JOB.progress()


def probe_status() -> dict[str, Any]:
    """组件状态：装了识别模型时 engine 是 "ppocr"；只有 Tesseract 时是 "tesseract"（只认西文，可更新）。"""
    status = ocr_status()
    models = installer.installed_info()
    ocr_files = installer.manifest_files()
    pdf_files = pdf_installer.manifest_files()
    dependency_supported = bool(pdf_files)
    needs_pdf = ppocr_component.numpy_site_dir() is None
    status["managed_installed"] = bool(models["installed"] or status.get("managed_installed"))
    status["recognition_one_click_supported"] = bool(ocr_files)
    status["dependency_one_click_supported"] = not needs_pdf or dependency_supported
    status["one_click_supported"] = bool(ocr_files) and (not needs_pdf or dependency_supported)
    status["one_click_reason"] = _unsupported_reason("文字识别组件", ocr_files)
    status["dependency_one_click_reason"] = _unsupported_reason("PDF 处理组件", pdf_files) if needs_pdf else ""
    status.update(_runtime_diagnostics())
    status["download_bytes"] = installer.download_size()
    # 识别模型用「PDF 处理组件」里的 NumPy、OpenCV：没装它时先装它
    status["needs_pdf_component"] = needs_pdf
    if status["needs_pdf_component"] and status["one_click_supported"]:
        status["one_click_reason"] = "尚未安装「PDF 处理组件」；一键安装会先自动安装它，再安装文字识别组件。"
    elif status["needs_pdf_component"] and not dependency_supported and not status["one_click_reason"]:
        status["one_click_reason"] = status["dependency_one_click_reason"]
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
    ocr_files = installer.manifest_files()
    if not ocr_files:
        raise ValueError(_unsupported_reason("文字识别组件", ocr_files))
    needs_pdf = ppocr_component.numpy_site_dir() is None
    if needs_pdf:
        pdf_files = pdf_installer.manifest_files()
        if not pdf_files:
            raise ValueError(_unsupported_reason("PDF 处理组件", pdf_files))
        from api import pdf2docx_plugin
        if pdf2docx_plugin.install_progress().get("running"):
            raise ValueError("PDF 处理组件正在安装，请稍候。")
    return _JOB.start(installer.download_size() + (pdf_installer.download_size() if needs_pdf else 0))


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
