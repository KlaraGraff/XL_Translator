"""插件页「一键安装」共用的下载、核对和解压。

每个文件都写死了大小和 SHA-256：下载源只是搬运工，内容不对就不装。官方源下载失败时
依次换镜像（同样的路径、同样的文件），核对规则不变。

本模块在 ``core`` 下，不能从 ``api`` 导入。
"""
from __future__ import annotations

import hashlib
import io
import os
import shutil
import threading
import time
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


# Windows 上杀毒软件会在文件刚写完时打开它扫描，这一两秒里改名会被拒绝（WinError 5 / 32）。
_REPLACE_RETRY_DELAYS = (0.2, 0.5, 1.0, 2.0, 4.0, 8.0)


def replace_file(source: Path, target: Path) -> None:
    """``os.replace``；Windows 上被占用时等一会儿再试，一共等十几秒。"""
    for delay in (*_REPLACE_RETRY_DELAYS, None):
        try:
            os.replace(source, target)
            return
        except PermissionError as exc:
            if delay is None or os.name != "nt":
                raise
            logger.debug(f"[组件安装] {target.name} 暂时无法改名，稍后再试：{exc!r}")
            time.sleep(delay)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_matches(path: Path, size: int, sha256: str) -> bool:
    try:
        return path.stat().st_size == size and _sha256(path) == sha256
    except OSError:
        return False


def _verified(path: Path, item: RemoteFile) -> bool:
    return file_matches(path, item.size, item.sha256)


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
        replace_file(partial, path)
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


# ---------------------------------------------------------------------------
# conda-forge 的 .conda 包：zip 里放一个 pkg-*.tar.zst（文件本体）和一个 info-*.tar.zst。
# 只取用得到的几个文件，不跑 conda 的安装流程（不改写路径、不重签名），所以文件和
# 包里原样一致；conda-forge 的程序和库都按相对路径（@rpath / $ORIGIN）找彼此。

CONDA_HOSTS = (
    "https://conda.anaconda.org/conda-forge/",
    "https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud/conda-forge/",
    "https://mirrors.bfsu.edu.cn/anaconda/cloud/conda-forge/",
)


def conda_file(subdir: str, name: str, size: int, sha256: str) -> RemoteFile:
    return RemoteFile(name, tuple(f"{host}{subdir}/{name}" for host in CONDA_HOSTS), size, sha256)


def _safe_member(name: str) -> str:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise ComponentDownloadError("组件文件不完整。")
    return str(path)


def _write_member(source, destination: Path, executable: bool) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.part")
    with temporary.open("wb") as handle:
        shutil.copyfileobj(source, handle, _CHUNK)
    if executable:
        temporary.chmod(0o755)
    replace_file(temporary, destination)


def extract_tar_zst(stream, target: Path, files: dict[str, str],
                    hashes: dict[str, tuple[int, str]] | None = None) -> None:
    """从 tar.zst 流里取出 files（包内路径 -> target 下的相对路径），取齐就停。

    给了 hashes（包内路径 -> 大小、SHA-256）就逐个核对；只读到取齐为止，所以只下载了
    包的开头一段时也能用。缺文件或核对不过都算失败。
    """
    import tarfile

    import zstandard

    wanted = {_safe_member(name): destination for name, destination in files.items()}
    try:
        reader = zstandard.ZstdDecompressor().stream_reader(stream, read_size=_CHUNK)
        with tarfile.open(fileobj=reader, mode="r|") as archive:
            for member in archive:
                destination = wanted.pop(member.name, None)
                if destination is None:
                    if not wanted:
                        break
                    continue
                if not member.isfile():
                    raise ComponentDownloadError("组件文件不完整。")
                source = archive.extractfile(member)
                target_path = target / destination
                if hashes is not None:
                    size, sha256 = hashes[member.name]
                    data = source.read()
                    if len(data) != size or hashlib.sha256(data).hexdigest() != sha256:
                        logger.warning(f"[组件安装] {member.name} 核对未通过")
                        raise ComponentDownloadError("组件文件核对未通过。")
                    source = io.BytesIO(data)
                _write_member(source, target_path, bool(member.mode & 0o111))
                if not wanted:
                    break
    except ComponentDownloadError:
        raise
    except (OSError, EOFError, tarfile.TarError, zstandard.ZstdError) as exc:
        logger.warning(f"[组件安装] 解压失败：{exc!r}")
        raise ComponentDownloadError("组件文件解压失败。") from exc
    if wanted:
        logger.warning(f"[组件安装] 包里缺少：{sorted(wanted)}")
        raise ComponentDownloadError("组件文件不完整。")


def extract_conda(package: Path, target: Path, files: dict[str, str]) -> None:
    """从下载好（整包已核对）的 .conda 里取出 files。"""
    try:
        with zipfile.ZipFile(package) as archive:
            members = [info for info in archive.infolist() if info.filename.startswith("pkg-")]
            if len(members) != 1:
                raise ComponentDownloadError("组件文件不完整。")
            with archive.open(members[0]) as raw:
                extract_tar_zst(raw, target, files)
    except (zipfile.BadZipFile, OSError) as exc:
        logger.warning(f"[组件安装] 打开 {package.name} 失败：{exc!r}")
        raise ComponentDownloadError("组件文件解压失败。") from exc


class _RangeStream:
    """把 HTTP 响应当成只读文件给解压用：从 offset 开始，最多 limit 字节。

    服务器不认 Range、回了整个文件时，先跳过 offset 之前的部分。
    """

    def __init__(self, response, offset: int, limit: int, progress: Callable[[int], None] | None,
                 cancel: threading.Event | None) -> None:
        self._chunks = response.iter_bytes(_CHUNK)
        self._buffer = b""
        self._skip = offset if response.status_code == 200 else 0
        self._left = limit
        self._progress = progress
        self._cancel = cancel
        self.received = 0

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        while (size < 0 or len(self._buffer) < size) and self._left > 0:
            if self._cancel is not None and self._cancel.is_set():
                raise ComponentDownloadCancelled("安装已取消。")
            try:
                chunk = next(self._chunks)
            except StopIteration:
                break
            if self._skip:
                dropped = min(self._skip, len(chunk))
                chunk = chunk[dropped:]
                self._skip -= dropped
            chunk = chunk[: self._left]
            self._left -= len(chunk)
            self.received += len(chunk)
            self._buffer += chunk
            if self._progress:
                self._progress(self.received)
        if size < 0:
            data, self._buffer = self._buffer, b""
        else:
            data, self._buffer = self._buffer[:size], self._buffer[size:]
        return data


def stream_conda_files(
    item: RemoteFile,
    data_offset: int,
    data_size: int,
    target: Path,
    files: dict[str, str],
    hashes: dict[str, tuple[int, str]],
    *,
    progress: Callable[[int], None] | None = None,
    cancel: threading.Event | None = None,
    client: httpx.Client | None = None,
    max_bytes: int = 16 * 1024 * 1024,
) -> None:
    """只下载 .conda 里 pkg 部分的开头一段，取出 files 并逐个核对。

    conda-forge 的 tesseract 包把全部一百多种语言数据也打在里面（约 180 MB），程序本身
    排在前面、只占开头一两兆：按 Range 读到取齐为止，不下整包。
    """
    limit = min(data_size, max_bytes)
    own_client = client is None
    http = client or httpx.Client(timeout=_TIMEOUT, follow_redirects=True)
    last_error = ""
    try:
        for url in item.urls:
            if cancel is not None and cancel.is_set():
                raise ComponentDownloadCancelled("安装已取消。")
            headers = {"Range": f"bytes={data_offset}-{data_offset + limit - 1}"}
            try:
                with http.stream("GET", url, headers=headers) as response:
                    if response.status_code not in (200, 206):
                        raise ComponentDownloadError(f"HTTP {response.status_code}")
                    stream = _RangeStream(response, data_offset, limit, progress, cancel)
                    extract_tar_zst(stream, target, files, hashes)
                    return
            except ComponentDownloadCancelled:
                raise
            except (httpx.HTTPError, OSError, ComponentDownloadError) as exc:
                last_error = f"{url}: {exc}"
                logger.warning(f"[组件安装] 下载失败，换下一个下载源：{last_error}")
                continue
    finally:
        if own_client:
            http.close()
    logger.warning(f"[组件安装] {item.name} 所有下载源都失败：{last_error}")
    raise ComponentDownloadError("组件下载失败，请检查网络连接。")
