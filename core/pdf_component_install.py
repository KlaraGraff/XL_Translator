"""「PDF 处理组件」的一键安装：从 PyPI 下载固定版本的 PyMuPDF、pdf2docx 及其依赖，
逐个核对 SHA-256 后解压到应用数据目录，试加载通过才算装好。

装好的目录（``plugins/pdf-component/<MANIFEST_VERSION>-<随机>/site-packages``）只给
``core.pdf_component`` 用：PyMuPDF 在翻译引擎里按需加载；PDF 转 Word 在单独的子进程里跑
pdf2docx，NumPy、OpenCV 这些大库不进主进程。

文件清单按平台写死（Python 3.11，与安装包里的翻译引擎一致）。升级组件时改清单并把
``MANIFEST_VERSION`` 加一；旧版本目录在新版装好后删掉。

本模块在 ``core`` 下，不能从 ``api`` 导入。
"""
from __future__ import annotations

import json
import platform
import shutil
import sys
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from loguru import logger

from core.app_paths import get_app_data_dir
from core.component_download import (
    ComponentDownloadError,
    RemoteFile,
    download_files,
    extract_wheel,
    file_matches,
    pypi_file,
    replace_file,
)

MANIFEST_VERSION = "1"

_PDF2DOCX = pypi_file(
    "pdf2docx-0.5.13-py3-none-any.whl",
    "71/4d/4041fddff079a2cd0c87612afe74a16d2cf032b2aa73ecddbc6bca2a04fe/pdf2docx-0.5.13-py3-none-any.whl",
    123863, "a293e9e78d89b12a4a43fcefba1346de220681c3daf20b8a7d3e1fce77f0fe97")
_FONTTOOLS = pypi_file(
    "fonttools-4.66.1-py3-none-any.whl",
    "f6/10/d45b74135d5d642cb3a4fb0a957c1613ef93de4c8548671dfc3a5bf38299/fonttools-4.66.1-py3-none-any.whl",
    1202222, "7234ae9e28db64273fbbfa72caebd0a97e3bdba6b05064114741b9539ef339d0")

# pdf2docx 0.5.13 的依赖：PyMuPDF、python-docx（安装包里已有）、fontTools、NumPy、
# OpenCV（无界面版）。fire 只在它的命令行入口里用，我们直接调它的 Converter，不装。
# OpenCV 4.10 / NumPy 2.2 是还支持 macOS 12 和 Python 3.11 的版本。
MANIFEST: dict[str, tuple[RemoteFile, ...]] = {
    "macos-arm64": (
        pypi_file(
            "pymupdf-1.28.2-cp310-abi3-macosx_11_0_arm64.whl",
            "fa/01/3591f781b417b382a8487a2356e927acfe858b1043bab0ec47f6805bb109/pymupdf-1.28.2-cp310-abi3-macosx_11_0_arm64.whl",
            23875605, "7113846b35dbf0a033f088e4f4fb543dabeb4b0b12c112966a1ca1ee2d5eacae"),
        _PDF2DOCX,
        pypi_file(
            "opencv_python_headless-4.10.0.84-cp37-abi3-macosx_11_0_arm64.whl",
            "1c/9b/583c8d9259f6fc19413f83fd18dd8e6cbc8eefb0b4dc6da52dd151fe3272/opencv_python_headless-4.10.0.84-cp37-abi3-macosx_11_0_arm64.whl",
            54835657, "a4f4bcb07d8f8a7704d9c8564c224c8b064c63f430e95b61ac0bffaa374d330e"),
        pypi_file(
            "numpy-2.2.6-cp311-cp311-macosx_11_0_arm64.whl",
            "b3/2b/64e1affc7972decb74c9e29e5649fac940514910960ba25cd9af4488b66c/numpy-2.2.6-cp311-cp311-macosx_11_0_arm64.whl",
            14406743, "c820a93b0255bc360f53eca31a0e676fd1101f673dda8da93454a12e23fc5f7a"),
        _FONTTOOLS,
    ),
    "windows-x64": (
        pypi_file(
            "pymupdf-1.28.2-cp310-abi3-win_amd64.whl",
            "4a/61/d563bbccba262f9dd6d2d35ccb72593648184d886188efb12d9ce8f34dd6/pymupdf-1.28.2-cp310-abi3-win_amd64.whl",
            19826532, "ebd244918798502d7b4504c90410d1711a4d7675a32584ca30f1bab419ecbffe"),
        _PDF2DOCX,
        pypi_file(
            "opencv_python_headless-4.10.0.84-cp37-abi3-win_amd64.whl",
            "26/d0/22f68eb23eea053a31655960f133c0be9726c6a881547e6e9e7e2a946c4f/opencv_python_headless-4.10.0.84-cp37-abi3-win_amd64.whl",
            38754031, "afcf28bd1209dd58810d33defb622b325d3cbe49dcd7a43a902982c33e5fad05"),
        pypi_file(
            "numpy-2.2.6-cp311-cp311-win_amd64.whl",
            "31/0a/f354fb7176b81747d870f7991dc763e157a934c717b67b58456bc63da3df/numpy-2.2.6-cp311-cp311-win_amd64.whl",
            12907455, "e8213002e427c69c45a52bbd94163084025f533a55a59d6f9c5b820774ef3303"),
        _FONTTOOLS,
    ),
    # 不发安装包，只给在 Linux 上开发、测试的人用。
    "linux-x64": (
        pypi_file(
            "pymupdf-1.28.2-cp310-abi3-manylinux_2_28_x86_64.whl",
            "c7/06/dace3e27af26690cb20bead80dbac42941b0841eb689b8aabbd67dde16f0/pymupdf-1.28.2-cp310-abi3-manylinux_2_28_x86_64.whl",
            25762500, "397d6715c1f0df7548a92d0afd8ce370fc48fa47aeefac16be2bc04a16a8227f"),
        _PDF2DOCX,
        pypi_file(
            "opencv_python_headless-4.10.0.84-cp37-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.whl",
            "d1/09/248f86a404567303cdf120e4a301f389b68e3b18e5c0cc428de327da609c/opencv_python_headless-4.10.0.84-cp37-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.whl",
            49858781, "377d08a7e48a1405b5e84afcbe4798464ce7ee17081c1c23619c8b398ff18295"),
        pypi_file(
            "numpy-2.2.6-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl",
            "b3/dd/2238b898e51bd6d389b7389ffb20d7f4c10066d80351187ec8e303a5a475/numpy-2.2.6-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl",
            16821570, "ba10f8411898fc418a521833e014a77d3ca01c15b0c6cdcce6a0d2897e6dbbdf"),
        _FONTTOOLS,
    ),
}

# PyMuPDF 的 Windows 版要用 VC++ 运行库里的 msvcp140.dll，wheel 和翻译引擎都没带：没装过
# 「Visual C++ 可再发行程序包」的电脑上加载不了。NumPy 的 wheel 里带了一份（只改了文件名），
# 核对后拷到 pymupdf 目录里，Windows 加载 PyMuPDF 时先在它自己的目录里找。
RUNTIME_COPIES: dict[str, tuple[tuple[str, str, int, str], ...]] = {
    "windows-x64": (
        ("numpy.libs/msvcp140-263139962577ecda4cd9469ca360a746.dll", "pymupdf/msvcp140.dll",
         575056, "a4c2229bdc2a2a630acdc095b4d86008e5c3e3bc7773174354f3da4f5beb9cde"),
    ),
}

ProgressFn = Callable[[str, int, int], None]  # (phase, done, total)


class ComponentInstallError(RuntimeError):
    """一键安装没有成功；消息是给用户看的一句话。"""


def platform_key() -> str:
    machine = platform.machine().lower()
    if sys.version_info[:2] != (3, 11):
        return ""  # 清单里的 NumPy 只对 Python 3.11
    if sys.platform == "darwin" and machine == "arm64":
        return "macos-arm64"
    if sys.platform == "win32" and machine in ("amd64", "x86_64"):
        return "windows-x64"
    if sys.platform.startswith("linux") and machine in ("x86_64", "amd64"):
        return "linux-x64"
    return ""


def _copy_runtime(site_dir: Path) -> None:
    for source, target, size, sha256 in RUNTIME_COPIES.get(platform_key(), ()):
        path = site_dir / source
        if not file_matches(path, size, sha256):
            logger.warning(f"[PDF 处理组件] 运行库 {source} 缺失或核对未通过")
            raise ComponentInstallError("组件文件不完整。")
        shutil.copyfile(path, site_dir / target)


def manifest_files() -> tuple[RemoteFile, ...]:
    return MANIFEST.get(platform_key(), ())


def download_size() -> int:
    return sum(item.size for item in manifest_files())


def component_root() -> Path:
    return get_app_data_dir() / "plugins" / "pdf-component"


def _record_path() -> Path:
    return component_root() / "installed.json"


def _read_record() -> dict[str, Any]:
    try:
        data = json.loads(_record_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def installed_site_dir() -> Path | None:
    """一键安装装好的 site-packages；没装或文件已缺失时返回 None。"""
    record = _read_record()
    relative = str(record.get("site_dir") or "").strip()
    if not relative:
        return None
    site_dir = component_root() / relative
    required = (site_dir / "pymupdf" / "__init__.py", site_dir / "pdf2docx" / "__init__.py")
    return site_dir if all(path.is_file() for path in required) else None


def installed_info() -> dict[str, Any]:
    record = _read_record()
    site_dir = installed_site_dir()
    return {
        "installed": site_dir is not None,
        "site_dir": str(site_dir or ""),
        "version": str(record.get("version") or ""),
        "pymupdf": str(record.get("pymupdf") or ""),
        "outdated": site_dir is not None and str(record.get("version") or "") != MANIFEST_VERSION,
    }


def _write_record(data: dict[str, Any]) -> None:
    path = _record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        replace_file(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


_INSTALL_LOCK = threading.Lock()


def _http_client():
    """测试替换成假的下载源；平时由 download_files 自己建、自己关。"""
    return None


def install(*, progress: ProgressFn | None = None, cancel: threading.Event | None = None) -> dict[str, Any]:
    """下载、核对、解压、试加载（含一次 PDF 转 Word），全部通过才换上新目录。"""
    from core.pdf_component import probe_in_subprocess

    files = manifest_files()
    if not files:
        raise ComponentInstallError("当前系统不支持一键安装。")
    if not _INSTALL_LOCK.acquire(blocking=False):
        raise ComponentInstallError("组件正在安装，请稍候。")
    root = component_root()
    downloads = root / ".downloads"
    # 每次装进一个新目录：Windows 上正被翻译引擎加载着的旧库删不掉、也挪不动，不能原地覆盖。
    # 直接解压到最终目录、不再整个改名：杀毒软件还在扫刚写出的库时，Windows 不让改名。
    # 试运行通过后写下记录才算换上；没写记录的目录不会被用到，下次安装或卸载时删掉。
    final_dir = root / f"{MANIFEST_VERSION}-{uuid.uuid4().hex[:8]}"
    installed = False
    try:

        def on_bytes(done: int, total: int) -> None:
            if progress:
                progress("download", done, total)

        try:
            wheels = download_files(files, downloads, progress=on_bytes, cancel=cancel, client=_http_client())
        except ComponentDownloadError as exc:
            raise ComponentInstallError(str(exc)) from exc
        site_dir = final_dir / "site-packages"
        site_dir.mkdir(parents=True, exist_ok=True)
        for index, wheel in enumerate(wheels, start=1):
            if progress:
                progress("extract", index - 1, len(wheels))
            try:
                extract_wheel(wheel, site_dir)
            except ComponentDownloadError as exc:
                raise ComponentInstallError(str(exc)) from exc
        _copy_runtime(site_dir)
        if progress:
            progress("verify", 0, 1)
        ok, version, detail = probe_in_subprocess(site_dir, convert=True)
        if not ok:
            logger.warning(f"[PDF 处理组件] 一键安装后试运行失败：{detail}")
            raise ComponentInstallError("组件已下载，但无法在本机运行。")
        old_dirs = [path for path in root.iterdir() if path.is_dir() and path not in (final_dir, downloads)]
        _write_record({
            "version": MANIFEST_VERSION,
            "site_dir": f"{final_dir.name}/site-packages",
            "pymupdf": version,
            "platform": platform_key(),
        })
        installed = True
        # 旧目录尽量删，删不掉的下次安装或卸载时再删。
        for old in old_dirs:
            _discard(old)
        shutil.rmtree(downloads, ignore_errors=True)
        if progress:
            progress("done", 1, 1)
        return installed_info()
    finally:
        if not installed:
            shutil.rmtree(final_dir, ignore_errors=True)
        _INSTALL_LOCK.release()


def _discard(path: Path) -> None:
    trash = path.with_name(f".trash-{uuid.uuid4().hex[:8]}")
    try:
        path.replace(trash)
    except OSError:
        trash = path
    shutil.rmtree(trash, ignore_errors=True)


def uninstall() -> None:
    """删掉一键安装的组件。已加载进翻译引擎的 PyMuPDF 要等软件重启才会释放。"""
    root = component_root()
    _record_path().unlink(missing_ok=True)
    if not root.is_dir():
        return
    for path in list(root.iterdir()):
        if path.is_dir():
            _discard(path)
