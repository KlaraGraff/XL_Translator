"""插件页「一键安装」共用的下载、核对和解压。

每个文件都写死了大小和 SHA-256：下载源只是搬运工，内容不对就不装。官方源下载失败时
依次换镜像（同样的路径、同样的文件），核对规则不变。

本模块在 ``core`` 下，不能从 ``api`` 导入。
"""
from __future__ import annotations

import hashlib
import shutil
import threading
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import httpx
from loguru import logger

_CHUNK = 256 * 1024
_TIMEOUT = httpx.Timeout(connect=15.0, read=60.0, write=60.0, pool=15.0)

ProgressFn = Callable[[int, int], None]


class ComponentDownloadError(RuntimeError):
    """下载、核对或解压没有成功；消息是给用户看的一句话。"""


class ComponentDownloadCancelled(ComponentDownloadError):
    pass


@dataclass(frozen=True)
class RemoteFile:
    name: str
    urls: tuple[str, ...]
    size: int
    sha256: str


# PyPI 的文件路径在几个常用镜像上一样：官方源连不上时依次换用。
PYPI_HOSTS = (
    "https://files.pythonhosted.org/packages/",
    "https://pypi.tuna.tsinghua.edu.cn/packages/",
    "https://mirrors.aliyun.com/pypi/packages/",
)


def pypi_file(name: str, path: str, size: int, sha256: str) -> RemoteFile:
    """path 是 files.pythonhosted.org/packages/ 之后的部分。"""
    return RemoteFile(name, tuple(host + path for host in PYPI_HOSTS), size, sha256)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verified(path: Path, item: RemoteFile) -> bool:
    try:
        return path.stat().st_size == item.size and _sha256(path) == item.sha256
    except OSError:
        return False


def download_files(
    items: Sequence[RemoteFile],
    target_dir: Path,
    *,
    progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
    client: httpx.Client | None = None,
) -> list[Path]:
    """下载并核对全部文件，返回本地路径。已下载且核对通过的文件不再重下。"""
    target_dir.mkdir(parents=True, exist_ok=True)
    total = sum(item.size for item in items)
    done = 0
    paths: list[Path] = []
    own_client = client is None
    http = client or httpx.Client(timeout=_TIMEOUT, follow_redirects=True)
    try:
        for item in items:
            path = target_dir / item.name
            if not _verified(path, item):
                _download_one(http, item, path, done, total, progress, cancel)
            done += item.size
            if progress:
                progress(done, total)
            paths.append(path)
    finally:
        if own_client:
            http.close()
    return paths


def _download_one(http: httpx.Client, item: RemoteFile, path: Path, base: int, total: int,
                  progress: ProgressFn | None, cancel: threading.Event | None) -> None:
    partial = path.with_name(path.name + ".part")
    last_error = ""
    for url in item.urls:
        if cancel is not None and cancel.is_set():
            raise ComponentDownloadCancelled("安装已取消。")
        received = 0
        digest = hashlib.sha256()
        try:
            with http.stream("GET", url) as response:
                response.raise_for_status()
                with partial.open("wb") as handle:
                    for chunk in response.iter_bytes(_CHUNK):
                        if cancel is not None and cancel.is_set():
                            raise ComponentDownloadCancelled("安装已取消。")
                        handle.write(chunk)
                        digest.update(chunk)
                        received += len(chunk)
                        if received > item.size:
                            raise ComponentDownloadError("downloaded more bytes than expected")
                        if progress:
                            progress(base + received, total)
        except ComponentDownloadCancelled:
            partial.unlink(missing_ok=True)
            raise
        except (httpx.HTTPError, OSError, ComponentDownloadError) as exc:
            last_error = f"{url}: {exc}"
            logger.warning(f"[组件安装] 下载失败，换下一个下载源：{last_error}")
            partial.unlink(missing_ok=True)
            continue
        if received != item.size or digest.hexdigest() != item.sha256:
            last_error = f"{url}: size or SHA-256 mismatch"
            logger.warning(f"[组件安装] 文件核对未通过，换下一个下载源：{last_error}")
            partial.unlink(missing_ok=True)
            continue
        partial.replace(path)
        return
    logger.warning(f"[组件安装] {item.name} 所有下载源都失败：{last_error}")
    raise ComponentDownloadError("组件下载失败，请检查网络连接。")


def extract_wheel(wheel: Path, target: Path) -> None:
    """wheel 就是 zip：解到 target（site-packages），拒绝指向外面的路径。"""
    root = target.resolve()
    try:
        with zipfile.ZipFile(wheel) as archive:
            for info in archive.infolist():
                name = PurePosixPath(info.filename)
                if name.is_absolute() or ".." in name.parts:
                    raise ComponentDownloadError("组件文件不完整。")
                destination = (root / Path(*name.parts)).resolve()
                if root not in destination.parents and destination != root:
                    raise ComponentDownloadError("组件文件不完整。")
                if info.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, destination.open("wb") as handle:
                    shutil.copyfileobj(source, handle, _CHUNK)
                mode = (info.external_attr >> 16) & 0o777
                if mode & 0o111:
                    destination.chmod(destination.stat().st_mode | 0o111)
    except (zipfile.BadZipFile, OSError) as exc:
        logger.warning(f"[组件安装] 解压 {wheel.name} 失败：{exc!r}")
        raise ComponentDownloadError("组件文件解压失败。") from exc
