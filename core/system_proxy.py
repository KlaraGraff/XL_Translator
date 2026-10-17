"""The system proxy httpx would use for a URL, and when to try once without it.

httpx (``trust_env=True``, the default) reads the same settings as
``urllib.request.getproxies()``: the HTTPS_PROXY / ALL_PROXY / NO_PROXY
environment variables, plus the system proxy settings on macOS and Windows. A
proxy left behind by a VPN or a proxy app that is no longer running turns
every request into a connection error even though the server is reachable
directly, so the few requests the user starts by hand (sending feedback,
checking for updates) try once more without it.
"""

from __future__ import annotations

import urllib.request

import httpx

# 只在「连代理、经代理建连」这一步失败时才直连再试：请求还没发到服务器，重发不会重复；
# 服务器已经回了状态码（4xx/5xx）就不是代理的问题，不重试。
PROXY_CONNECT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError)


def configured_proxy(url: str) -> str:
    """The proxy set for ``url``'s scheme, before NO_PROXY is applied, or ""."""
    proxies = urllib.request.getproxies()
    scheme = httpx.URL(url).scheme
    return proxies.get(scheme) or proxies.get("all") or ""


def bypasses_proxy(url: str) -> bool:
    """Whether NO_PROXY exempts ``url`` (exact or suffix match, ``*`` for all)."""
    no_proxy = urllib.request.getproxies().get("no", "")
    if no_proxy.strip() == "*":
        return True
    host = httpx.URL(url).host
    excluded = [item.strip().lstrip("*").lstrip(".") for item in no_proxy.split(",")]
    return any(item and (host == item or host.endswith(f".{item}")) for item in excluded)


def proxy_for(url: str) -> str:
    """The proxy httpx will route ``url`` through, or "" when it goes direct."""
    proxy = configured_proxy(url)
    return "" if not proxy or bypasses_proxy(url) else proxy


def should_retry_direct(exc: BaseException, url: str) -> bool:
    """A connection-stage failure of a request that went through a system proxy."""
    if not isinstance(exc, PROXY_CONNECT_ERRORS):
        return False
    try:
        return bool(proxy_for(url))
    except Exception:  # noqa: BLE001 - 读不了代理设置就当没走代理
        return False
