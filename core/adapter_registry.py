"""扩展列表（注册中心）客户端：在应用里浏览、安装审核过的扩展（扩展性阶段 F）。

列表是公开仓库 ``KlaraGraff/XL_Translator-extensions`` 里由工作流生成的 ``index.json``：每个扩展
一个条目（名称、简介、支持的扩展名、作者、项目主页、许可证、版本、下载地址、SHA-256、体积……），
扩展代码本身在作者自己的仓库里。这里只做三件事：

- ``fetch_index``：先取 jsDelivr；它失败或几秒内没回应就同时取 raw.githubusercontent.com，先拿到
  的为准；缓存 24 小时；两处都连不上而有缓存时返回缓存并标 ``stale``。不合规的条目跳过，只列「官方」和「已审核」两种。
- ``download_entry``：按条目里的地址列表依次下载 zip，核对大小和 SHA-256（``core/component_download``）；
  地址只是搬运工，内容不对就不装，镜像无法篡改。安装本身走与本地添加相同的检查
  （``core/adapter_install``），由 ``api/extension_install_job.py`` 接上注册。
- ``updates``：已装版本低于列表版本的扩展。

本模块在 ``core`` 下，不能从 ``api`` 导入。
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import httpx
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app_meta import APP_VERSION
from core import system_proxy
from core.adapter_install import MAX_PACKAGE_BYTES, bundled_ids, read_registry, version_key
from core.app_paths import get_app_data_dir
from core.component_download import (
    ComponentDownloadCancelled,
    ComponentDownloadError,
    RemoteFile,
    download_files,
    with_cdn_mirrors,
)
from core.format_adapter import LANGUAGES, SUPPORTED_ADAPTER_API_VERSIONS

REGISTRY_REPO = "KlaraGraff/XL_Translator-extensions"
REGISTRY_HOME = f"https://github.com/{REGISTRY_REPO}"
TEMPLATE_HOME = "https://github.com/KlaraGraff/XL_Translator-extension-template"
#: ``index.json`` 的地址，按顺序尝试：jsDelivr 的几个节点（国内大多可达），GitHub 原始地址作后备。
#: 前一个失败，或 ``FALLBACK_DELAY_SECONDS`` 秒内没有结果，就同时开始取下一个，先拿到的为准：
#: jsDelivr 偶尔卡住十几秒才报 503，不能让「浏览扩展」跟着转圈。
INDEX_URLS = (
    *with_cdn_mirrors([f"https://cdn.jsdelivr.net/gh/{REGISTRY_REPO}@main/index.json"]),
    f"https://raw.githubusercontent.com/{REGISTRY_REPO}/main/index.json",
)
#: 缓存多久之内不再联网。
CACHE_TTL_SECONDS = 24 * 3600
FALLBACK_DELAY_SECONDS = 3.0
#: 列表本身的上限（几百个条目也只有几百 KB）。
MAX_INDEX_BYTES = 4 * 1024 * 1024
LISTED_STATUSES = ("official", "reviewed")

_TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=20.0, pool=10.0)
#: 下载扩展包：一个地址 15 秒没有新数据就换下一个（通用组件下载是 60 秒，扩展包只有几十 KB）。
_DOWNLOAD_TIMEOUT = httpx.Timeout(connect=10.0, read=15.0, write=20.0, pool=10.0)
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_LOCK = threading.Lock()

ProgressFn = Callable[[int, int], None]


class RegistryError(RuntimeError):
    """拿不到列表、找不到条目或下载失败；消息是给用户看的一句话。"""


def _texts(value: Any, field: str, *, required: bool) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} 必须是 {{zh, en, fr}} 对象")
    texts = {lang: str(value.get(lang) or "").strip() for lang in LANGUAGES}
    if required and not all(texts.values()):
        raise ValueError(f"{field} 须有 zh、en、fr 三种语言")
    return {lang: text for lang, text in texts.items() if text}


class RegistryEntry(BaseModel):
    """``index.json`` 里的一个扩展（字段同注册仓库的 ``schema/entry.schema.json``）。

    未知字段忽略：以后给条目加字段时，旧版应用照常读。
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    name: dict[str, str]
    summary: dict[str, str]
    extensions: list[str]
    author: str
    repo: str
    license: str
    version: str
    adapter_api_version: int
    min_app_version: str = ""
    download: list[str]
    sha256: str
    size: int
    tags: list[str] = Field(default_factory=list)
    reviewed_at: str = ""
    reviewed_by: str = ""
    status: Literal["official", "reviewed"]

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not _ID_RE.match(value):
            raise ValueError("id 不合规")
        return value

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, value: Any) -> dict[str, str]:
        return _texts(value, "name", required=True)

    @field_validator("summary", mode="before")
    @classmethod
    def _summary(cls, value: Any) -> dict[str, str]:
        return _texts(value, "summary", required=True)

    @field_validator("extensions")
    @classmethod
    def _extensions(cls, value: list[str]) -> list[str]:
        cleaned = [str(item).strip().lower().lstrip(".") for item in value if str(item).strip()]
        if not cleaned:
            raise ValueError("extensions 不能为空")
        return cleaned

    @field_validator("repo")
    @classmethod
    def _repo(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("repo 须是 https 地址")
        return value

    @field_validator("author", "license", "version")
    @classmethod
    def _required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("不能为空")
        return value

    @field_validator("download")
    @classmethod
    def _download(cls, value: list[str]) -> list[str]:
        urls = [url for url in value if isinstance(url, str) and url.startswith("https://")]
        if not urls or len(urls) != len(value):
            raise ValueError("download 须是一个或多个 https 地址")
        return urls[:5]

    @field_validator("sha256")
    @classmethod
    def _sha256(cls, value: str) -> str:
        value = value.strip().lower()
        if not _SHA256_RE.match(value):
            raise ValueError("sha256 不合规")
        return value

    @field_validator("size")
    @classmethod
    def _size(cls, value: int) -> int:
        if not 0 < value <= MAX_PACKAGE_BYTES:
            raise ValueError("size 超出范围")
        return value

    def incompatibility(self, app_version: str = APP_VERSION) -> str:
        """本应用装不了它的原因；装得了时为空。"""
        low, high = SUPPORTED_ADAPTER_API_VERSIONS
        if not low <= self.adapter_api_version <= high:
            return "需要更新 Translator 才能安装"
        if self.min_app_version and version_key(app_version) < version_key(self.min_app_version):
            return f"需要 Translator {self.min_app_version} 或更新的版本"
        return ""


def parse_index(data: Any) -> tuple[list[RegistryEntry], list[str]]:
    """（可列出的条目, 跳过的原因）；同一 id 只取第一条。"""
    if not isinstance(data, dict) or not isinstance(data.get("extensions"), list):
        raise ValueError("index.json 结构不正确")
    entries: list[RegistryEntry] = []
    skipped: list[str] = []
    seen: set[str] = set()
    for position, raw in enumerate(data["extensions"]):
        label = raw.get("id") if isinstance(raw, dict) else None
        try:
            entry = RegistryEntry.model_validate(raw)
        except ValidationError as exc:
            skipped.append(f"{label or f'#{position}'}：{exc.error_count()} 处不合规")
            continue
        if entry.status not in LISTED_STATUSES or entry.id in seen:
            continue
        seen.add(entry.id)
        entries.append(entry)
    return entries, skipped


# ── 缓存与联网 ────────────────────────────────────────────────────────────────


def cache_path() -> Path:
    return get_app_data_dir() / "cache" / "extensions-index.json"


def _read_cache() -> dict[str, Any] | None:
    try:
        data = json.loads(cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("index"), dict):
        return None
    return data


def _write_cache(index: dict[str, Any], source: str, fetched_at: float) -> None:
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps({"fetched_at": fetched_at, "source": source, "index": index}, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except OSError as exc:
        logger.warning(f"[扩展列表] 无法写入缓存：{exc}")
    finally:
        temporary.unlink(missing_ok=True)


def _http_client() -> httpx.Client | None:
    """测试替换成假的服务器；平时每次自己建、自己关。"""
    return None


def _get_index(client: httpx.Client, url: str) -> dict[str, Any]:
    response = client.get(url)
    response.raise_for_status()
    if len(response.content) > MAX_INDEX_BYTES:
        raise ValueError("index.json 过大")
    data = response.json()
    parse_index(data)
    return data


def _download_index(client: httpx.Client) -> tuple[dict[str, Any], str]:
    results: queue.Queue[tuple[str, dict[str, Any] | None, Exception | None]] = queue.Queue()

    def fetch(url: str) -> None:
        try:
            results.put((url, _get_index(client, url), None))
        except Exception as exc:  # 每个地址都必须回一个结果，否则下面会一直等
            results.put((url, None, exc))

    waiting = list(INDEX_URLS)
    running = 0
    while waiting or running:
        # 刚开始、上一个地址失败了，或等了 FALLBACK_DELAY_SECONDS 秒还没结果：开始取下一个。
        # 守护线程：没回来的那个不拖住调用方，也不拖住退出。
        if waiting:
            url = waiting.pop(0)
            threading.Thread(target=fetch, args=(url,), name="extension-index", daemon=True).start()
            running += 1
        try:
            url, data, error = results.get(timeout=FALLBACK_DELAY_SECONDS if waiting else None)
        except queue.Empty:
            logger.info(f"[扩展列表] {FALLBACK_DELAY_SECONDS:g} 秒内没有结果，同时尝试下一个地址")
            continue
        running -= 1
        if data is not None:
            return data, url
        logger.warning(f"[扩展列表] 获取失败，换下一个地址：{url}: {error}")
    raise RegistryError("无法获取扩展列表，请检查网络连接。")


def _view(index: dict[str, Any], *, fetched_at: float, source: str, stale: bool, error: str = "") -> dict[str, Any]:
    entries, skipped = parse_index(index)
    for problem in skipped:
        logger.info(f"[扩展列表] 跳过不合规的条目 {problem}")
    installed = read_registry()
    shipped = bundled_ids()
    items = []
    for entry in entries:
        data = entry.model_dump()
        current = str((installed.get(entry.id) or {}).get("version") or "")
        reason = entry.incompatibility()
        if entry.id in shipped:
            # 随应用提供的类型（例如「文本」）也列在注册仓库里，作为写法示例；不能再装一份。
            state = "bundled"
        elif reason:
            state = "incompatible"
        elif not current:
            state = "available"
        elif version_key(entry.version) > version_key(current):
            state = "update"
        else:
            state = "installed"
        data.update(installed_version=current, state=state, incompatible_reason=reason)
        items.append(data)
    return {
        "entries": items,
        "fetched_at": fetched_at,
        "source": source,
        "stale": stale,
        "error": error,
        "registry_home": REGISTRY_HOME,
        "template_home": TEMPLATE_HOME,
    }


def fetch_index(*, force: bool = False, now: float | None = None) -> dict[str, Any]:
    """扩展列表（给「浏览扩展」用）。

    每个条目另带 ``state``：``available`` 可安装、``installed`` 已是最新、``update`` 有新版本、
    ``incompatible`` 本应用装不了（原因在 ``incompatible_reason``）、``bundled`` 随应用提供。

    缓存不到 24 小时且不是 ``force`` 时不联网；联网失败而有缓存时返回缓存并带 ``stale: true``；
    都没有时 ``entries`` 为空、``error`` 写明原因（不抛错，页面照常显示）。
    """
    now = time.time() if now is None else now
    with _LOCK:
        cached = _read_cache()
        if cached and not force:
            fetched_at = float(cached.get("fetched_at") or 0)
            if 0 <= now - fetched_at < CACHE_TTL_SECONDS:
                try:
                    return _view(cached["index"], fetched_at=fetched_at, source=str(cached.get("source") or ""), stale=False)
                except ValueError:
                    cached = None
        provided = _http_client()
        client = provided or system_proxy.http_client(timeout=_TIMEOUT, follow_redirects=True)
        try:
            index, source = _download_index(client)
        except RegistryError as exc:
            if cached:
                try:
                    return _view(
                        cached["index"],
                        fetched_at=float(cached.get("fetched_at") or 0),
                        source=str(cached.get("source") or ""),
                        stale=True,
                        error=str(exc),
                    )
                except ValueError:
                    pass
            return {**_view({"extensions": []}, fetched_at=0, source="", stale=True), "error": str(exc)}
        finally:
            if provided is None:
                client.close()
        _write_cache(index, source, now)
        return _view(index, fetched_at=now, source=source, stale=False)


def find_entry(entry_id: str) -> RegistryEntry:
    """列表里的一个条目（用缓存，必要时联网）；找不到时抛 ``RegistryError``。"""
    view = fetch_index()
    for item in view["entries"]:
        if item["id"] == str(entry_id or "").strip().lower():
            return RegistryEntry.model_validate(item)
    raise RegistryError(view.get("error") or "扩展列表里没有这个扩展，请刷新后重试。")


def download_entry(
    entry: RegistryEntry,
    target_dir: Path,
    *,
    progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
) -> Path:
    """按条目的地址依次下载 zip，大小和 SHA-256 不对就换下一个；全部失败抛 ``RegistryError``。

    jsDelivr / GitHub 原始地址自动补上 jsDelivr 的其它节点（``with_cdn_mirrors``）。
    """
    urls = with_cdn_mirrors(entry.download)
    item = RemoteFile(f"{entry.id}-{_safe(entry.version)}.zip", urls, entry.size, entry.sha256)
    provided = _http_client()
    client = provided or system_proxy.http_client(timeout=_DOWNLOAD_TIMEOUT, follow_redirects=True)
    try:
        return download_files([item], target_dir, progress=progress, cancel=cancel, client=client)[0]
    except ComponentDownloadCancelled as exc:
        raise RegistryError("安装已取消。") from exc
    except ComponentDownloadError as exc:
        raise RegistryError("扩展下载失败，请检查网络连接。") from exc
    finally:
        if provided is None:
            client.close()


def _safe(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]+", "_", value)[:40]


def download_dir() -> Path:
    return get_app_data_dir() / "cache" / "extension-downloads"


def updates() -> list[str]:
    """已装的扩展里，列表中有更新版本的 id（用缓存，不强制联网）。"""
    return [item["id"] for item in fetch_index()["entries"] if item["state"] == "update"]


__all__ = [
    "CACHE_TTL_SECONDS",
    "INDEX_URLS",
    "REGISTRY_HOME",
    "TEMPLATE_HOME",
    "RegistryEntry",
    "RegistryError",
    "cache_path",
    "download_dir",
    "download_entry",
    "fetch_index",
    "find_entry",
    "parse_index",
    "updates",
]
