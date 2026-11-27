"""「段落重排组件（BabelDOC）」的一键安装。

BabelDOC（AGPL-3.0，PDFMathTranslate 2.0 的内核）在原 PDF 上按段落重排译文：公式、图、表格线
原样搬回，页数不变。我们不改它的源码，固定 0.7.1，按清单从 PyPI 下载它和它的全部依赖（每个
wheel 都核对大小和 SHA-256），再按 BabelDOC 自己写死的清单下载字体、CMap、版面模型和 tiktoken
词表（同样核对），装完在子进程里真排一页 PDF，通过了才算装好。

NumPy、OpenCV、PyMuPDF 用「PDF 处理组件」里的那份（版本对得上），不再下载一份；没装它时插件页
先自动装它（api/babeldoc_plugin.py）。

装好的目录（``plugins/babeldoc-component/<MANIFEST_VERSION>-<随机>/``）：
- ``site-packages/``：BabelDOC 和依赖；
- ``home/.cache/babeldoc/``：BabelDOC 的资源缓存（它按 ``Path.home()`` 找，子进程里把 HOME
  指到这里，不往用户主目录里写东西）。

BabelDOC 只在单独的子进程里跑（``core.babeldoc_component``）：原生库出问题时崩的是子进程。

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
from core.babeldoc_component_manifest import ASSETS, BABELDOC_VERSION, WHEELS
from core.component_download import (
    ComponentDownloadError,
    RemoteFile,
    download_files,
    extract_wheel,
    replace_file,
)

COMPONENT_NAME = "段落重排组件"
MANIFEST_VERSION = "1"
CACHE_PARTS = (".cache", "babeldoc")
# 解压后的程序包比 wheel 大两三倍；再留些余量给试排和翻译时 BabelDOC 写的缓存。
_DISK_FACTOR = 3.2

ProgressFn = Callable[[str, int, int], None]  # (phase, done, total)


class ComponentInstallError(RuntimeError):
    """一键安装没有成功；消息是给用户看的一句话。"""


def platform_key() -> str:
    machine = platform.machine().lower()
    if sys.version_info[:2] != (3, 11):
        return ""  # 清单里的 wheel 只对 Python 3.11（与翻译引擎安装包一致）
    if sys.platform == "darwin" and machine == "arm64":
        return "macos-arm64"
    if sys.platform == "win32" and machine in ("amd64", "x86_64"):
        return "windows-x64"
    if sys.platform.startswith("linux") and machine in ("x86_64", "amd64"):
        return "linux-x64"
    return ""


def wheel_files() -> tuple[RemoteFile, ...]:
    return WHEELS.get(platform_key(), ())


def asset_files() -> list[tuple[str, RemoteFile]]:
    """(缓存里的子目录, 文件)。资源不分平台。"""
    return [(folder, RemoteFile(name, tuple(urls), size, sha256))
            for folder, name, size, sha256, _sha3, urls in ASSETS]


def manifest_files() -> tuple[RemoteFile, ...]:
    """本机一键安装要下载的全部文件；当前系统不支持时为空。"""
    wheels = wheel_files()
    return (*wheels, *(item for _folder, item in asset_files())) if wheels else ()


def download_size() -> int:
    return sum(item.size for item in manifest_files())


def component_root() -> Path:
    return get_app_data_dir() / "plugins" / "babeldoc-component"


def _record_path() -> Path:
    return component_root() / "installed.json"


def _read_record() -> dict[str, Any]:
    path = _record_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        logger.debug(f"[{COMPONENT_NAME}] 没有可用的安装记录：{exc!r}")
        return {}
    return data if isinstance(data, dict) else {}


def cache_dir(home: Path) -> Path:
    return home.joinpath(*CACHE_PARTS)


def installed_dirs() -> tuple[Path, Path] | None:
    """一键安装装好的 (site-packages, home)；没装或文件已缺失时返回 None。"""
    relative = str(_read_record().get("dir") or "").strip()
    if not relative or "/" in relative or "\\" in relative or relative.startswith("."):
        return None
    base = component_root() / relative
    site_dir, home = base / "site-packages", base / "home"
    required = [site_dir / "babeldoc" / "__init__.py"]
    required += [cache_dir(home) / folder / item.name for folder, item in asset_files()]
    return (site_dir, home) if all(path.is_file() for path in required) else None


def installed_info() -> dict[str, Any]:
    record = _read_record()
    dirs = installed_dirs()
    return {
        "installed": dirs is not None,
        "path": str(dirs[0].parent if dirs else ""),
        "version": str(record.get("version") or ""),
        "babeldoc": str(record.get("babeldoc") or ""),
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


def _check_disk_space(root: Path) -> None:
    """装到一半磁盘写满时，用户只看到一句「解压失败」：先算清楚要多少空间。"""
    needed = int(download_size() * _DISK_FACTOR)
    probe = root
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError as exc:
        logger.debug(f"[{COMPONENT_NAME}] 读不到磁盘剩余空间，照常安装：{exc!r}")
        return
    if free < needed:
        raise ComponentInstallError(
            f"磁盘空间不足：安装需要约 {needed / 1024 ** 3:.1f} GB，当前只剩 {free / 1024 ** 3:.1f} GB。")


_INSTALL_LOCK = threading.Lock()


def _http_client():
    """测试替换成假的下载源；平时由 download_files 自己建、自己关。"""
    return None


def install(*, progress: ProgressFn | None = None, cancel: threading.Event | None = None) -> dict[str, Any]:
    """下载、核对、解压、在子进程里真排一页 PDF，全部通过才换上新目录。"""
    from core.babeldoc_component import numpy_site_dir, probe_in_subprocess

    wheels = wheel_files()
    if not wheels:
        raise ComponentInstallError("当前系统不支持一键安装。")
    numpy_site = numpy_site_dir()
    if numpy_site is None:
        raise ComponentInstallError("请先安装「PDF 处理组件」。")
    if not _INSTALL_LOCK.acquire(blocking=False):
        raise ComponentInstallError("组件正在安装，请稍候。")
    root = component_root()
    downloads = root / ".downloads"
    # 每次装进一个新目录，试排通过后写下记录才算换上；没写记录的目录下次安装或卸载时删掉
    # （Windows 上正在用的旧库删不掉也挪不动，不能原地覆盖）。
    final_dir = root / f"{MANIFEST_VERSION}-{uuid.uuid4().hex[:8]}"
    installed = False
    staged: list[tuple[Path, Path]] = []
    try:
        _check_disk_space(root)
        total = download_size()
        base = [0]

        def on_bytes(done: int, _total: int) -> None:
            if progress:
                progress("download", base[0] + done, total)

        site_dir, home = final_dir / "site-packages", final_dir / "home"
        try:
            paths = download_files(wheels, downloads, progress=on_bytes, cancel=cancel, client=_http_client())
            base[0] = sum(item.size for item in wheels)
            # 资源先下到 .downloads 里（装到一半断了，下次接着用已核对好的），装的时候挪进
            # BabelDOC 的缓存目录：它按文件名找，下载时已逐个核对过。
            groups: dict[str, list[RemoteFile]] = {}
            for folder, item in asset_files():
                groups.setdefault(folder, []).append(item)
            for folder, items in groups.items():
                for path in download_files(items, downloads / "assets" / folder, progress=on_bytes,
                                           cancel=cancel, client=_http_client()):
                    staged.append((path, cache_dir(home) / folder / path.name))
                base[0] += sum(item.size for item in items)
            site_dir.mkdir(parents=True, exist_ok=True)
            for index, wheel in enumerate(paths):
                if progress:
                    progress("extract", index, len(paths) + 1)
                extract_wheel(wheel, site_dir)
            if progress:
                progress("extract", len(paths), len(paths) + 1)
            for source, target in staged:
                target.parent.mkdir(parents=True, exist_ok=True)
                replace_file(source, target)
        except ComponentDownloadError as exc:
            raise ComponentInstallError(str(exc)) from exc
        if progress:
            progress("verify", 0, 1)
        ok, version, detail = probe_in_subprocess(site_dir, home, numpy_site)
        if not ok:
            logger.warning(f"[{COMPONENT_NAME}] 一键安装后试排失败：{detail}")
            raise ComponentInstallError("组件已下载，但无法在本机运行。")
        old_dirs = [path for path in root.iterdir() if path.is_dir() and path not in (final_dir, downloads)]
        _write_record({
            "version": MANIFEST_VERSION,
            "dir": final_dir.name,
            "babeldoc": version or BABELDOC_VERSION,
            "platform": platform_key(),
        })
        installed = True
        for old in old_dirs:
            _discard(old)
        shutil.rmtree(downloads, ignore_errors=True)
        if progress:
            progress("done", 1, 1)
        return installed_info()
    finally:
        if not installed:
            _unstage(staged)
            shutil.rmtree(final_dir, ignore_errors=True)
        _INSTALL_LOCK.release()


def _unstage(staged: list[tuple[Path, Path]]) -> None:
    """没装成：已挪进新目录的资源挪回 .downloads，下次安装不用再下几百兆。"""
    for source, target in staged:
        if not target.is_file():
            continue
        try:
            replace_file(target, source)
        except OSError as exc:
            logger.debug(f"[{COMPONENT_NAME}] 资源挪不回下载目录，下次重新下载：{exc!r}")


def _discard(path: Path) -> None:
    trash = path.with_name(f".trash-{uuid.uuid4().hex[:8]}")
    try:
        path.replace(trash)
    except OSError as exc:
        logger.debug(f"[{COMPONENT_NAME}] 旧目录挪不走，原地删除：{exc!r}")
        trash = path
    shutil.rmtree(trash, ignore_errors=True)


def uninstall() -> None:
    """删掉一键安装的组件（下载到一半的文件一起删）。正在翻译的子进程结束后才删得掉的，下次再删。"""
    root = component_root()
    _record_path().unlink(missing_ok=True)
    if not root.is_dir():
        return
    for path in list(root.iterdir()):
        if path.is_dir():
            _discard(path)
