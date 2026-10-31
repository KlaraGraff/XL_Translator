"""The system proxy httpx would use for a URL, and when to try once without it.

httpx (``trust_env=True``, the default) reads the same settings as
``urllib.request.getproxies()``: the HTTPS_PROXY / ALL_PROXY / NO_PROXY
environment variables, plus the system proxy settings on macOS and Windows. A
proxy left behind by a VPN or a proxy app that is no longer running turns
every request into a connection error even though the server is reachable
directly, so the few requests the user starts by hand (sending feedback,
checking for updates) try once more without it.

``http_client`` / ``async_client`` are what every outgoing request in the app
builds its client with. httpx refuses to even create a client when the system
proxy is a kind it cannot speak: Windows「Internet 选项」里只填了 SOCKS 时 Python
读出来是 ``socks4://``（httpx 不支持），``socks5://`` 则要另装 socksio（随包附带，
万一缺了也不至于建不了客户端）。这两个函数先照常建；建不了时把 socks4 改成 socks5
（代理软件的 SOCKS 端口几乎都同时支持 5），实在用不了的那一条代理就直连，
并记一条日志，而不是让翻译、反馈、检查更新统统报错。
"""

from __future__ import annotations

import urllib.request
from typing import Any

import httpx
from loguru import logger

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


# 这几种写法 httpx 不认，但对面基本都是同时支持 SOCKS5 的代理软件端口。
_SOCKS_ALIASES = ("socks4://", "socks4a://", "socks://")
# 传给代理通道的连接参数（其余参数如 timeout、headers 属于客户端本身）。
_TRANSPORT_KEYS = ("verify", "cert", "trust_env", "http1", "http2", "limits")
_warned: set[str] = set()


def _warn_once(message: str) -> None:
    if message not in _warned:
        _warned.add(message)
        logger.warning(message)


def _redacted(url: str) -> str:
    """日志里不带代理的用户名和密码。"""
    try:
        parsed = httpx.URL(url)
        return f"{parsed.scheme}://{parsed.host}" + (f":{parsed.port}" if parsed.port else "")
    except Exception:  # noqa: BLE001
        return "（无法解析）"


def normalize_proxy_url(url: str) -> str:
    """socks4:// / socks4a:// / socks:// 改成 socks5://，其余原样返回。"""
    lowered = url.lower()
    for alias in _SOCKS_ALIASES:
        if lowered.startswith(alias):
            return "socks5://" + url[len(alias):]
    return url


def _environment_proxies() -> dict[str, str | None]:
    """httpx 自己会用的「地址范围 → 代理」表（含 NO_PROXY 排除项）。"""
    try:
        from httpx._utils import get_environment_proxies
    except ImportError:  # httpx 改了内部结构：退回最基本的读法
        proxies = urllib.request.getproxies()
        mounts: dict[str, str | None] = {}
        for scheme in ("http", "https", "all"):
            if proxies.get(scheme):
                value = proxies[scheme]
                mounts[f"{scheme}://"] = value if "://" in value else f"http://{value}"
        return mounts
    return get_environment_proxies()


def _proxy_mounts(kwargs: dict[str, Any], transport_cls: type) -> dict[str, Any]:
    transport_kwargs = {key: kwargs[key] for key in _TRANSPORT_KEYS if key in kwargs}
    mounts: dict[str, Any] = {}
    for pattern, url in _environment_proxies().items():
        if url is None:
            mounts[pattern] = None
            continue
        fixed = normalize_proxy_url(url)
        try:
            mounts[pattern] = transport_cls(proxy=fixed, **transport_kwargs)
        except (ValueError, ImportError) as exc:
            _warn_once(f"[网络] 系统代理 {_redacted(url)} 无法使用，改为直连：{exc}")
            mounts[pattern] = None
            continue
        if fixed != url:
            _warn_once(f"[网络] 系统代理 {_redacted(url)} 按 SOCKS5 连接")
    return mounts


def _build(client_cls: type, transport_cls: type, kwargs: dict[str, Any]) -> Any:
    try:
        return client_cls(**kwargs)
    except (ValueError, ImportError):
        # 只有系统代理会让一个不带 proxy/transport/mounts 参数的客户端建不起来。
        if not kwargs.get("trust_env", True) or {"proxy", "transport", "mounts"} & kwargs.keys():
            raise
    # 传了 transport 后 httpx 不再自己读系统代理，改由 mounts 按地址分别走代理或直连。
    transport_kwargs = {key: kwargs[key] for key in _TRANSPORT_KEYS if key in kwargs}
    return client_cls(**kwargs, transport=transport_cls(**transport_kwargs),
                      mounts=_proxy_mounts(kwargs, transport_cls))


def http_client(**kwargs: Any) -> httpx.Client:
    """``httpx.Client(**kwargs)``，系统代理是 httpx 用不了的类型时也能建出来。"""
    return _build(httpx.Client, httpx.HTTPTransport, kwargs)


def async_client(**kwargs: Any) -> httpx.AsyncClient:
    """``httpx.AsyncClient(**kwargs)`` 的同款。"""
    return _build(httpx.AsyncClient, httpx.AsyncHTTPTransport, kwargs)
