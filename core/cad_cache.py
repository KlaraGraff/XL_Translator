"""DWG → DXF conversions kept between the scan and the task.

DWG 要先经外部转换器转成 DXF 才能读，一张图几秒到几分钟。扫描时为了显示文字数和
图块名转换过的图，开始翻译时不该再转一遍：转换结果按「路径 + 大小 + 修改时间」存在
应用数据目录的缓存里，任务直接复用。图纸一改（大小或修改时间变了）就对不上号，按新图
重新转换。

缓存只是加速，丢了不影响结果：读写出错一律当作没有缓存，照常转换。总量超过上限时
从最久没用过的开始删。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import time
from pathlib import Path

from loguru import logger

# 目录名带版本号：将来缓存内容的格式变了换个目录，旧的自然失效。
CACHE_DIRNAME = "cad-dxf-v1"
DEFAULT_MAX_BYTES = 2 * 1024 ** 3
DEFAULT_MAX_AGE_SECONDS = 14 * 24 * 3600


class CadDxfCache:
    def __init__(
        self,
        root: str | Path,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
    ) -> None:
        self.root = Path(root)
        self.max_bytes = max_bytes
        self.max_age_seconds = max_age_seconds

    @staticmethod
    def key(source: Path) -> str | None:
        try:
            stat = source.stat()
        except OSError:
            return None
        identity = f"{source.resolve()}\0{stat.st_size}\0{stat.st_mtime_ns}"
        return hashlib.sha256(identity.encode("utf-8", "surrogatepass")).hexdigest()

    def _entry(self, source: Path) -> Path | None:
        key = self.key(source)
        return self.root / f"{key}.dxf" if key else None

    def lookup(self, source: str | Path) -> Path | None:
        """The cached DXF of ``source`` if it is still current, else None."""
        entry = self._entry(Path(source))
        if entry is None or not entry.is_file():
            return None
        try:
            os.utime(entry)  # 记下最近一次使用，清理时按它排先后
        except OSError:
            pass
        return entry

    def store(self, source: str | Path, dxf: str | Path) -> Path | None:
        """Copy a fresh conversion of ``source`` into the cache; None when it cannot."""
        entry = self._entry(Path(source))
        if entry is None:
            return None
        temporary: str | None = None
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            # 先写临时文件再改名：同时有扫描和任务在存同一张图时，读到的不会是半截文件。
            handle, temporary = tempfile.mkstemp(dir=self.root, suffix=".part")
            os.close(handle)
            shutil.copyfile(dxf, temporary)
            os.replace(temporary, entry)
        except OSError as exc:
            logger.warning(f"[CAD] 无法缓存转换后的图纸：{exc!r}")
            if temporary:
                Path(temporary).unlink(missing_ok=True)
            return None
        self.prune()
        return entry

    def prune(self) -> None:
        try:
            entries = [path for path in self.root.glob("*.dxf") if path.is_file()]
        except OSError:
            return
        now = time.time()
        survivors: list[tuple[float, int, Path]] = []
        for path in entries:
            try:
                stat = path.stat()
            except OSError:
                continue
            if now - stat.st_mtime > self.max_age_seconds:
                path.unlink(missing_ok=True)
                continue
            survivors.append((stat.st_mtime, stat.st_size, path))
        total = sum(size for _mtime, size, _path in survivors)
        for _mtime, size, path in sorted(survivors):
            if total <= self.max_bytes:
                break
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue
            total -= size
        for leftover in self.root.glob("*.part"):
            try:
                if now - leftover.stat().st_mtime > 3600:
                    leftover.unlink(missing_ok=True)
            except OSError:
                pass


def default_cad_cache() -> CadDxfCache:
    # 与任务历史一样从 settings 读，测试替换应用数据目录时缓存跟着走。
    import settings as settings_module

    return CadDxfCache(Path(settings_module.APP_DATA_DIR) / "cache" / CACHE_DIRNAME)
