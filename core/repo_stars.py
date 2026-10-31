"""项目在 GitHub 上有多少 Star：设置「更新与关于」页「去点 Star」按钮旁边显示这个数。

只是锦上添花的一个数字：查不到（没网、GitHub 限流、代理坏了）就返回 None，界面不显示数字，
按钮照常能用，也不报错、不记诊断。

一天最多问一次 GitHub：查到的数在内存里留 24 小时；查不到的结果留 1 小时，免得每次
进这一页都去碰一次连不上的网络。不写进设置文件——这个数不属于用户的配置，导出配置时也不该带上。
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from app_meta import APP_UPDATE_USER_AGENT
from core import system_proxy
from core.update_checker import DEFAULT_TIMEOUT_SECONDS, GITHUB_REPO

REPO_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}"
REPO_PAGE_URL = f"https://github.com/{GITHUB_REPO}"
SUCCESS_TTL_SECONDS = 24 * 3600
FAILURE_TTL_SECONDS = 3600

_lock = threading.Lock()
# (查的时刻, 结果)；结果 None 表示那次没查到。
_cache: tuple[float, int | None] | None = None


def fetch_star_count(*, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> int | None:
    """问一次 GitHub。查不到返回 None，不抛异常。"""

    def request(*, direct: bool) -> int | None:
        with system_proxy.http_client(timeout=timeout_seconds, follow_redirects=True, trust_env=not direct) as client:
            response = client.get(
                REPO_API_URL,
                headers={"Accept": "application/vnd.github+json", "User-Agent": APP_UPDATE_USER_AGENT},
            )
            response.raise_for_status()
            payload = response.json()
        value = payload.get("stargazers_count") if isinstance(payload, dict) else None
        # bool 是 int 的子类，要单独排除。
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    try:
        try:
            return request(direct=False)
        except system_proxy.PROXY_CONNECT_ERRORS as exc:
            # 系统代理连不上（关掉的 VPN 留下的设置）：直连再试一次，同检查更新。
            if not system_proxy.should_retry_direct(exc, REPO_API_URL):
                raise
            return request(direct=True)
    except Exception:  # noqa: BLE001 - 只是一个显示用的数字，查不到就不显示。
        return None


def cached_star_count(
    *,
    now: Callable[[], float] = time.monotonic,
    fetch: Callable[[], int | None] | None = None,
) -> int | None:
    """带缓存的 Star 数：查到的留一天，查不到的留一小时。"""
    global _cache
    with _lock:
        current = now()
        if _cache is not None:
            checked_at, value = _cache
            ttl = SUCCESS_TTL_SECONDS if value is not None else FAILURE_TTL_SECONDS
            if current - checked_at < ttl:
                return value
        value = (fetch or fetch_star_count)()
        _cache = (current, value)
        return value


def reset_cache() -> None:
    """测试用：清掉缓存。"""
    global _cache
    with _lock:
        _cache = None
