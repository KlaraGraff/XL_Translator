"""中文、日文线条字识别（PP-OCR 模型 + ONNX Runtime）的一键安装。

识别模型是 PaddleOCR 的 PP-OCRv6 small（检测、识别各一个，Apache-2.0），取自 RapidOCR
3.10.0 的 wheel（里面打着这两个模型，PyPI 和国内镜像都有）；运行模型用 ONNX Runtime
1.19.2（MIT，还支持 macOS 12 的最后一版）。都固定版本、整包核对 SHA-256，模型再逐个核对。
NumPy 和 OpenCV 用「PDF 处理组件」里的那份（原位替换本来就要它），不再下载一份。

装好的目录（``plugins/ppocr-component/<MANIFEST_VERSION>-<随机>/``）：
``site-packages/onnxruntime/``、``models/``（两个模型和它们的许可证）。识别在单独的子进程
里跑（``core.ppocr_component``）。

本模块在 ``core`` 下，不能从 ``api`` 导入。
"""
from __future__ import annotations

import hashlib
import json
import platform
import shutil
import sys
import threading
import uuid
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from loguru import logger

from core.app_paths import get_app_data_dir
from core.component_download import (
    ComponentDownloadError,
    RemoteFile,
    conda_file,
    download_files,
    extract_conda,
    extract_wheel,
    pypi_file,
    replace_file,
)

MANIFEST_VERSION = "1"
ONNXRUNTIME_VERSION = "1.19.2"
MODEL_NAME = "PP-OCRv6 small"

DET_MODEL = "PP-OCRv6_det_small.onnx"
REC_MODEL = "PP-OCRv6_rec_small.onnx"

_RAPIDOCR = pypi_file(
    "rapidocr-3.10.0-py3-none-any.whl",
    "59/07/203065283bba0c5e9c10af3602dc25ab3e59ee54dc2d1d22c8faeabf5ed8/rapidocr-3.10.0-py3-none-any.whl",
    27288134, "2fc34e26cd0f48514804a94f8832283039c7334076bff60768b1fed9ad068050")

# wheel 里的文件 -> models/ 下的名字、大小、SHA-256。程序代码不用（识别的前后处理在
# core.ppocr_engine），只取模型和许可证。
MODEL_FILES: tuple[tuple[str, str, int, str], ...] = (
    ("rapidocr/models/PP-OCRv6_det_small.onnx", DET_MODEL,
     9929594, "090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f"),
    ("rapidocr/models/PP-OCRv6_rec_small.onnx", REC_MODEL,
     21234383, "6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884"),
)
LICENSE_FILES: tuple[tuple[str, str], ...] = (
    ("rapidocr-3.10.0.dist-info/licenses/LICENSE", "LICENSE"),
    ("rapidocr-3.10.0.dist-info/licenses/MODEL_LICENSES.md", "MODEL_LICENSES.md"),
)

MANIFEST: dict[str, tuple[RemoteFile, ...]] = {
    "macos-arm64": (
        pypi_file(
            "onnxruntime-1.19.2-cp311-cp311-macosx_11_0_universal2.whl",
            "f0/ff/77bee5df55f034ee81d2e1bc58b2b8511b9c54f06ce6566cb562c5d95aa5/onnxruntime-1.19.2-cp311-cp311-macosx_11_0_universal2.whl",
            16779187, "d863e8acdc7232d705d49e41087e10b274c42f09e259016a46f32c34e06dc4fd"),
        _RAPIDOCR,
    ),
    "windows-x64": (
        pypi_file(
            "onnxruntime-1.19.2-cp311-cp311-win_amd64.whl",
            "1e/f5/9d995a685f97508b3254f17015b4a78641b0625e79480a7aed7a7a105d7c/onnxruntime-1.19.2-cp311-cp311-win_amd64.whl",
            11085695, "1c3e5d415b78337fa0b1b75291e9ea9fb2a4c1f148eb5811e7212fed02cfffa8"),
        _RAPIDOCR,
        conda_file("win-64", "vc14_runtime-14.51.36247-habf1de7_41.conda",
                   767955, "4e4cb599cdc41bf2109d1464c127b5bcbddf548ce3e322e612afb691338b48f8"),
    ),
    # 不发安装包，只给在 Linux 上开发、测试的人用。
    "linux-x64": (
        pypi_file(
            "onnxruntime-1.19.2-cp311-cp311-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl",
            "60/ce/be4152da5c1030ab5a159a4a792ed9abad6ba498d79ef0aeba593ff7b5bf/onnxruntime-1.19.2-cp311-cp311-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl",
            13167809, "a36511dc07c5c964b916697e42e366fa43c48cdb3d3503578d78cef30417cb84"),
        _RAPIDOCR,
    ),
}

# ONNX Runtime 的 Windows 版要用 VC++ 运行库（msvcp140.dll、msvcp140_1.dll 等），wheel 和
# 翻译引擎都没带齐：没装过「Visual C++ 可再发行程序包」的电脑上加载不了。取 conda-forge 的
# 运行库包（整包已核对）放进 onnxruntime/capi，Windows 加载它时先在它自己的目录里找。
_WINDOWS_RUNTIME = ("msvcp140.dll", "msvcp140_1.dll", "vcruntime140.dll", "vcruntime140_1.dll")

ProgressFn = Callable[[str, int, int], None]  # (phase, done, total)


class ComponentInstallError(RuntimeError):
    """一键安装没有成功；消息是给用户看的一句话。"""


def platform_key() -> str:
    machine = platform.machine().lower()
    if sys.version_info[:2] != (3, 11):
        return ""  # 清单里的 ONNX Runtime 只对 Python 3.11
    if sys.platform == "darwin" and machine == "arm64":
        return "macos-arm64"
    if sys.platform == "win32" and machine in ("amd64", "x86_64"):
        return "windows-x64"
    if sys.platform.startswith("linux") and machine in ("x86_64", "amd64"):
        return "linux-x64"
    return ""


def manifest_files() -> tuple[RemoteFile, ...]:
    return MANIFEST.get(platform_key(), ())


def download_size() -> int:
    return sum(item.size for item in manifest_files())


def component_root() -> Path:
    return get_app_data_dir() / "plugins" / "ppocr-component"


def _record_path() -> Path:
    return component_root() / "installed.json"


def _read_record() -> dict[str, Any]:
    try:
        data = json.loads(_record_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def installed_dirs() -> tuple[Path, Path] | None:
    """一键安装装好的 (site-packages, models)；没装或文件已缺失时返回 None。"""
    relative = str(_read_record().get("dir") or "").strip()
    if not relative or "/" in relative or "\\" in relative or relative.startswith("."):
        return None
    base = component_root() / relative
    site_dir, models = base / "site-packages", base / "models"
    required = (site_dir / "onnxruntime" / "__init__.py", models / DET_MODEL, models / REC_MODEL)
    return (site_dir, models) if all(path.is_file() for path in required) else None


def installed_info() -> dict[str, Any]:
    record = _read_record()
    dirs = installed_dirs()
    return {
        "installed": dirs is not None,
        "path": str(dirs[1].parent if dirs else ""),
        "version": str(record.get("version") or ""),
        "onnxruntime": str(record.get("onnxruntime") or ""),
        "models": str(record.get("models") or ""),
        "outdated": dirs is not None and str(record.get("version") or "") != MANIFEST_VERSION,
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


def _extract_models(wheel: Path, models: Path) -> None:
    """从 RapidOCR 的 wheel 里取出模型（逐个核对）和许可证。"""
    models.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(wheel) as archive:
            for member, name, size, sha256 in MODEL_FILES:
                data = archive.read(member)
                if len(data) != size or hashlib.sha256(data).hexdigest() != sha256:
                    logger.warning(f"[文字识别组件] {member} 核对未通过")
                    raise ComponentDownloadError("组件文件核对未通过。")
                (models / name).write_bytes(data)
            for member, name in LICENSE_FILES:
                (models / name).write_bytes(archive.read(member))
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        logger.warning(f"[文字识别组件] 解压 {wheel.name} 失败：{exc!r}")
        raise ComponentDownloadError("组件文件解压失败。") from exc


_INSTALL_LOCK = threading.Lock()


def _http_client():
    """测试替换成假的下载源；平时由 download_files 自己建、自己关。"""
    return None


def install(*, progress: ProgressFn | None = None, cancel: threading.Event | None = None) -> dict[str, Any]:
    """下载、核对、解压、试识别一行字，全部通过才换上新目录。"""
    from core.ppocr_component import numpy_site_dir, probe_in_subprocess

    files = manifest_files()
    if not files:
        raise ComponentInstallError("当前系统不支持一键安装。")
    numpy_site = numpy_site_dir()
    if numpy_site is None:
        raise ComponentInstallError("请先安装「PDF 处理组件」。")
    if not _INSTALL_LOCK.acquire(blocking=False):
        raise ComponentInstallError("组件正在安装，请稍候。")
    root = component_root()
    downloads = root / ".downloads"
    # 直接解压到最终目录、不再整个改名（杀毒软件还在扫刚写出的库时，Windows 不让改名）；
    # 试识别通过后写下记录才算换上，没写记录的目录下次安装或卸载时删掉。
    final_dir = root / f"{MANIFEST_VERSION}-{uuid.uuid4().hex[:8]}"
    installed = False
    try:

        def on_bytes(done: int, total: int) -> None:
            if progress:
                progress("download", done, total)

        try:
            paths = download_files(files, downloads, progress=on_bytes, cancel=cancel, client=_http_client())
            site_dir, models = final_dir / "site-packages", final_dir / "models"
            site_dir.mkdir(parents=True, exist_ok=True)
            if progress:
                progress("extract", 0, 2)
            extract_wheel(paths[0], site_dir)
            if progress:
                progress("extract", 1, 2)
            _extract_models(paths[1], models)
            if len(paths) > 2:
                extract_conda(paths[2], site_dir / "onnxruntime" / "capi", {name: name for name in _WINDOWS_RUNTIME})
        except ComponentDownloadError as exc:
            raise ComponentInstallError(str(exc)) from exc
        if progress:
            progress("verify", 0, 1)
        ok, version, detail = probe_in_subprocess(site_dir, models, numpy_site)
        if not ok:
            logger.warning(f"[文字识别组件] 一键安装后试识别失败：{detail}")
            raise ComponentInstallError("组件已下载，但无法在本机运行。")
        old_dirs = [path for path in root.iterdir() if path.is_dir() and path not in (final_dir, downloads)]
        _write_record({
            "version": MANIFEST_VERSION,
            "dir": final_dir.name,
            "onnxruntime": version,
            "models": MODEL_NAME,
            "platform": platform_key(),
        })
        installed = True
        # 旧目录尽量删，正被识别进程用着、删不掉的，下次安装或卸载时再删。
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
    """删掉一键安装的识别模型和 ONNX Runtime（正在识别的进程结束后才删得掉的，下次再删）。"""
    from core.ppocr_component import shutdown_reader

    shutdown_reader()
    root = component_root()
    _record_path().unlink(missing_ok=True)
    if not root.is_dir():
        return
    for path in list(root.iterdir()):
        if path.is_dir():
            _discard(path)
