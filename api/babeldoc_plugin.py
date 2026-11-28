"""「段落重排组件」（BabelDOC）for the plugins page: one item of the 「PDF 组件」 group.

一键安装见 ``core.babeldoc_component_install``。它用「PDF 处理组件」里的 NumPy、OpenCV、PyMuPDF：
没装那个时先装它（与文字识别组件一样，同一个可取消的安装链路）。没有「选择安装位置」：它要一整套
和翻译引擎版本对得上的程序库，再加几百兆字体和版面模型，自己装几乎对不上；装不上时重试即可，
可提取文字的页照旧转 Word。

装好后，PDF「智能混合」里可提取文字的页就先用它（core/pdf_babeldoc_pages.py），不用另改设置。
"""
from __future__ import annotations

from typing import Any

from api.component_install_job import InstallJob
from core import babeldoc_component_install as installer
from core import pdf_component_install as pdf_installer

OFFICIAL_BABELDOC_URL = "https://github.com/funstory-ai/BabelDOC"


def _pdf_component_missing() -> bool:
    from core.babeldoc_component import numpy_site_dir

    return numpy_site_dir() is None


def install_pdf_dependency(*, progress=None, cancel=None, base: int = 0, total: int = 0) -> None:
    """没有「PDF 处理组件」时先装它（NumPy、OpenCV、PyMuPDF 用它的）。进度记作 dependency_*。"""
    from api import pdf2docx_plugin
    from core.pdf2docx_plugin import prefer_managed
    from core.pdf_component import forget_failures

    if pdf2docx_plugin.install_progress().get("running"):
        raise installer.ComponentInstallError("PDF 处理组件正在安装，请稍候。")
    pdf_total = pdf_installer.download_size()

    def dependency_progress(phase: str, done: int, _total: int) -> None:
        if progress is None:
            return
        if phase == "download":
            progress("dependency_download", base + done, total)
        elif phase in ("extract", "verify"):
            progress(f"dependency_{phase}", base + pdf_total, total)

    try:
        pdf_installer.install(progress=dependency_progress, cancel=cancel)
    except pdf_installer.ComponentInstallError as exc:
        raise installer.ComponentInstallError(str(exc)) from exc
    prefer_managed()
    forget_failures()


def _install_with_dependency(*, progress=None, cancel=None) -> dict[str, Any]:
    needs_pdf = _pdf_component_missing()
    pdf_total = pdf_installer.download_size() if needs_pdf else 0
    total = pdf_total + installer.download_size()
    if needs_pdf:
        install_pdf_dependency(progress=progress, cancel=cancel, total=total)

    def own_progress(phase: str, done: int, _total: int) -> None:
        if progress is None:
            return
        progress(phase, pdf_total + done if phase == "download" else pdf_total, total)

    return installer.install(progress=own_progress, cancel=cancel)


_JOB = InstallJob("段落重排组件", lambda: _install_with_dependency, lambda: installer.ComponentInstallError)


def install_progress() -> dict[str, Any]:
    return _JOB.progress()


def running() -> bool:
    return _JOB.running()


def _unsupported_reason() -> str:
    if installer.wheel_files():
        return ""
    import platform
    import sys

    return (f"当前运行环境不支持段落重排组件一键安装，需要 Python 3.11 和 macOS arm64、Windows x64 或 Linux x64；"
            f"当前为 Python {platform.python_version()} / {sys.platform} {platform.machine().lower()}。")


def probe_status() -> dict[str, Any]:
    info = installer.installed_info()
    needs_pdf = _pdf_component_missing()
    reason = _unsupported_reason()
    if not reason and needs_pdf and not pdf_installer.manifest_files():
        reason = "当前系统不能自动安装 PDF 处理组件，段落重排组件要用它里面的程序库。"
    return {
        "state": "connected" if info["installed"] else "missing",
        "installed": bool(info["installed"]),
        "version": str(info.get("babeldoc") or ""),
        "outdated": bool(info.get("outdated")),
        "path": str(info.get("path") or ""),
        # 装着，但「PDF 处理组件」后来被卸载了：段落重排用不了
        "needs_pdf_component": needs_pdf,
        "one_click_supported": not reason,
        "one_click_reason": reason,
        "download_bytes": installer.download_size(),
        "dependency_bytes": pdf_installer.download_size() if needs_pdf else 0,
        "install": install_progress(),
    }


def start_install() -> dict[str, Any]:
    """后台开始一键安装；进度用 install_progress() 查。已在安装时不重复开始。"""
    from api import pdf_components_plugin

    reason = _unsupported_reason()
    if reason:
        raise ValueError(reason)
    if pdf_components_plugin.running():
        raise ValueError("「PDF 组件」正在安装，请稍候。")
    needs_pdf = _pdf_component_missing()
    if needs_pdf:
        if not pdf_installer.manifest_files():
            raise ValueError("当前系统不能自动安装 PDF 处理组件，段落重排组件要用它里面的程序库。")
        from api import pdf2docx_plugin

        if pdf2docx_plugin.install_progress().get("running"):
            raise ValueError("PDF 处理组件正在安装，请稍候。")
    return _JOB.start(installer.download_size() + (pdf_installer.download_size() if needs_pdf else 0))


def cancel_install() -> dict[str, Any]:
    return _JOB.cancel()


def uninstall() -> dict[str, Any]:
    """删掉一键安装的段落重排组件。选了段落重排的文字页之后照旧转 Word。"""
    if _JOB.running():
        raise ValueError("组件正在安装，请稍候。")
    installer.uninstall()
    return probe_status()
