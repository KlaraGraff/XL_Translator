"""A SOCKS system proxy: translation, feedback and downloads still get through.

Real sockets on 127.0.0.1: a small SOCKS5 server (what a proxy app's SOCKS port
speaks) relaying to a small HTTP server. Windows「Internet 选项」里填的 SOCKS 代理
Python 读成 ``socks4://``，httpx 本身连客户端都建不起来。
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import patch

import httpx
import pytest

from core import component_download, feedback, system_proxy
from scripts.socks_proxy_smoke import Origin, Socks5Server

_PROXY_VARS = ("http_proxy", "https_proxy", "all_proxy", "no_proxy")


@contextmanager
def _system_proxy(url: str, no_proxy: str = "") -> Iterator[None]:
    values = {name: url for name in _PROXY_VARS[:3]}
    values["no_proxy"] = no_proxy
    env = {**values, **{name.upper(): value for name, value in values.items()}}
    with patch.dict("os.environ", env):
        yield


@pytest.fixture
def socks():
    server = Socks5Server()
    yield server
    server.close()


@pytest.fixture
def origin():
    server = Origin()
    yield server
    server.close()


@pytest.mark.parametrize("scheme", ["socks5", "socks5h", "socks4", "socks"])
def test_requests_go_through_a_socks_system_proxy(socks, origin, scheme):
    with _system_proxy(f"{scheme}://127.0.0.1:{socks.port}"):
        with system_proxy.http_client(timeout=5) as client:
            assert client.get(origin.url + "/models").json()["ok"] is True
    assert socks.targets == [origin.url.removeprefix("http://")]


def test_plain_httpx_cannot_even_build_a_client_for_socks4(socks):
    # 修复前每个地方都直接 httpx.Client()：这一步就报错，翻译、反馈、检查更新全都失败。
    with _system_proxy(f"socks4://127.0.0.1:{socks.port}"), pytest.raises(ValueError):
        httpx.Client()


def test_async_client_goes_through_a_socks4_system_proxy(socks, origin):
    async def fetch() -> dict:
        async with system_proxy.async_client(timeout=5) as client:
            return (await client.post(origin.url + "/chat/completions", json={})).json()

    with _system_proxy(f"socks4://127.0.0.1:{socks.port}"):
        assert asyncio.run(fetch())["ok"] is True
    assert len(socks.targets) == 1


def test_feedback_and_component_download_clients_use_the_socks_proxy(socks, origin):
    with _system_proxy(f"socks4://127.0.0.1:{socks.port}"):
        with feedback._open_client() as client:
            assert client.post(origin.url + "/v1/feedback", json={}).status_code == 200
        with component_download.open_client() as client:
            assert client.get(origin.url + "/a.whl").status_code == 200
    assert len(socks.targets) == 2


def test_no_proxy_still_wins_when_the_proxy_is_rewritten(socks, origin):
    with _system_proxy(f"socks4://127.0.0.1:{socks.port}", no_proxy="127.0.0.1"):
        with system_proxy.http_client(timeout=5) as client:
            assert client.get(origin.url).status_code == 200
    assert socks.targets == []


def test_without_socksio_the_request_goes_direct_instead_of_failing(socks, origin, monkeypatch):
    monkeypatch.setitem(sys.modules, "socksio", None)  # import socksio → ImportError
    with _system_proxy(f"socks5://127.0.0.1:{socks.port}"):
        with pytest.raises(ImportError):
            httpx.Client()
        with system_proxy.http_client(timeout=5) as client:
            assert client.get(origin.url).status_code == 200
    assert socks.targets == []


def test_an_explicit_transport_is_never_rewritten(socks):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    with _system_proxy(f"socks4://127.0.0.1:{socks.port}"):
        with system_proxy.http_client(transport=httpx.MockTransport(handler)) as client:
            assert client.get("https://example.com").status_code == 204


def test_proxy_url_normalization():
    assert system_proxy.normalize_proxy_url("socks4://127.0.0.1:1080") == "socks5://127.0.0.1:1080"
    assert system_proxy.normalize_proxy_url("SOCKS4A://u:p@h:1") == "socks5://u:p@h:1"
    assert system_proxy.normalize_proxy_url("socks://h:1") == "socks5://h:1"
    assert system_proxy.normalize_proxy_url("socks5h://h:1") == "socks5h://h:1"
    assert system_proxy.normalize_proxy_url("http://h:1") == "http://h:1"
