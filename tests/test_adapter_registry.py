"""The reviewed extensions list (core/adapter_registry.py) and installing from it.

A fake server (httpx.MockTransport) stands in for jsDelivr, GitHub and the
authors' release pages.  These pin that the list falls back to the second
address and to the cache, that bad entries are skipped rather than breaking the
page, that every entry carries the state the 「浏览扩展」 list shows, and that an
install only happens when the downloaded zip matches the list's size and SHA-256
and passes the same checks as a local package.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.surfaces import all_surfaces
from core import adapter_install, adapter_registry
from core.adapter_registry import CACHE_TTL_SECONDS, INDEX_URLS, RegistryEntry, parse_index
from tests.test_adapter_install import KEY, _wait_install, isolated, make_package, zip_package  # noqa: F401

PRIMARY, FALLBACK = INDEX_URLS
ZIP_URL = "https://github.com/someone/linetext/releases/download/v1.0.0/linetext-1.0.0.zip"
MIRROR_URL = "https://mirror.example.com/linetext-1.0.0.zip"


def entry(**overrides) -> dict:
    data = {
        "id": "linetext",
        "name": {"zh": "按行文本", "en": "Line text", "fr": "Texte par ligne"},
        "summary": {"zh": "逐行翻译纯文本", "en": "Translate plain text line by line", "fr": "Traduit ligne par ligne"},
        "extensions": [".TXT"],
        "author": "someone",
        "repo": "https://github.com/someone/linetext",
        "license": "MIT",
        "version": "1.0.0",
        "adapter_api_version": 1,
        "min_app_version": "",
        "download": [ZIP_URL],
        "sha256": "0" * 64,
        "size": 100,
        "tags": ["text"],
        "reviewed_at": "2026-10-14",
        "reviewed_by": "maple",
        "status": "reviewed",
    }
    data.update(overrides)
    return data


class FakeServer:
    """URL → bytes or status code; counts requests."""

    def __init__(self, routes: dict[str, bytes | int]) -> None:
        self.routes = routes
        self.requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        answer = self.routes.get(url, 404)
        if isinstance(answer, int):
            return httpx.Response(answer)
        return httpx.Response(200, content=answer)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


@pytest.fixture
def serve() -> Callable[[dict[str, bytes | int]], FakeServer]:
    patches = []

    def start(routes: dict[str, bytes | int]) -> FakeServer:
        server = FakeServer(routes)
        patcher = patch("core.adapter_registry._http_client", server.client)
        patcher.start()
        patches.append(patcher)
        return server

    yield start
    for patcher in patches:
        patcher.stop()


def index(*entries: dict) -> bytes:
    return json.dumps({"schema": 1, "extensions": list(entries)}, ensure_ascii=False).encode("utf-8")


def package_zip(tmp_path: Path, *, version: str = "1.0.0") -> tuple[bytes, str]:
    archive = zip_package(make_package(tmp_path / f"linetext-{version}", version=version), tmp_path / f"linetext-{version}.zip")
    data = archive.read_bytes()
    return data, hashlib.sha256(data).hexdigest()


# ── the list ────────────────────────────────────────────────────────────────


def test_the_second_address_is_used_when_the_first_fails(serve) -> None:
    server = serve({PRIMARY: 503, FALLBACK: index(entry())})
    view = adapter_registry.fetch_index()
    assert [item["id"] for item in view["entries"]] == ["linetext"]
    assert view["source"] == FALLBACK and view["stale"] is False and view["error"] == ""
    assert view["entries"][0]["extensions"] == ["txt"]
    assert view["entries"][0]["state"] == "available"
    assert view["registry_home"].endswith("/XL_Translator-extensions")
    assert view["template_home"].endswith("/XL_Translator-extension-template")
    assert server.requests == [PRIMARY, FALLBACK]


def test_a_primary_that_hangs_does_not_hold_up_the_list(serve) -> None:
    # jsDelivr sometimes takes many seconds before answering 503; the second
    # address starts after FALLBACK_DELAY_SECONDS and the first answer wins.
    server = serve({FALLBACK: index(entry())})
    release = threading.Event()
    answer = server.handler

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == PRIMARY:
            server.requests.append(PRIMARY)
            release.wait(10)
            return httpx.Response(503)
        return answer(request)

    server.handler = handler
    try:
        with patch.object(adapter_registry, "FALLBACK_DELAY_SECONDS", 0.05):
            started = time.monotonic()
            view = adapter_registry.fetch_index()
            elapsed = time.monotonic() - started
    finally:
        release.set()
    assert view["source"] == FALLBACK and view["stale"] is False
    assert [item["id"] for item in view["entries"]] == ["linetext"]
    assert server.requests == [PRIMARY, FALLBACK]
    assert elapsed < 2


def test_a_fresh_cache_is_used_without_the_network(serve) -> None:
    server = serve({PRIMARY: index(entry())})
    adapter_registry.fetch_index(now=1_000_000)
    adapter_registry.fetch_index(now=1_000_000 + CACHE_TTL_SECONDS - 60)
    assert server.requests == [PRIMARY]
    # Refresh always asks again.
    adapter_registry.fetch_index(force=True, now=1_000_000 + 120)
    assert server.requests == [PRIMARY, PRIMARY]


def test_an_old_cache_is_shown_as_stale_when_offline(serve) -> None:
    serve({PRIMARY: index(entry())})
    adapter_registry.fetch_index(now=1_000_000)
    server = serve({})
    view = adapter_registry.fetch_index(now=1_000_000 + CACHE_TTL_SECONDS + 1)
    assert [item["id"] for item in view["entries"]] == ["linetext"]
    assert view["stale"] is True and view["fetched_at"] == 1_000_000
    assert "无法获取扩展列表" in view["error"]
    assert server.requests == [PRIMARY, FALLBACK]


def test_offline_without_a_cache_is_an_empty_list_not_an_error(serve) -> None:
    serve({PRIMARY: 500, FALLBACK: b"not json"})
    view = adapter_registry.fetch_index()
    assert view["entries"] == [] and view["stale"] is True
    assert view["error"] == "无法获取扩展列表，请检查网络连接。"


def test_a_damaged_cache_is_ignored(serve) -> None:
    path = adapter_registry.cache_path()
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")
    serve({PRIMARY: index(entry())})
    assert [item["id"] for item in adapter_registry.fetch_index()["entries"]] == ["linetext"]


def test_bad_entries_are_skipped_and_the_rest_still_listed() -> None:
    entries, skipped = parse_index(
        {
            "extensions": [
                entry(id="ok-one", future_field={"anything": 1}),
                entry(id="Bad Id"),
                entry(id="no-fr", name={"zh": "x", "en": "x"}),
                entry(id="plain-http", download=["http://example.com/a.zip"]),
                entry(id="bad-hash", sha256="abc"),
                entry(id="too-big", size=50 * 1024 * 1024),
                entry(id="pending", status="pending"),
                "not an object",
                entry(id="ok-one", version="9.9.9"),
                entry(id="ok-two", status="official"),
            ]
        }
    )
    assert [(item.id, item.version) for item in entries] == [("ok-one", "1.0.0"), ("ok-two", "1.0.0")]
    assert [problem.split("：")[0] for problem in skipped] == [
        "Bad Id", "no-fr", "plain-http", "bad-hash", "too-big", "pending", "#7",
    ]
    with pytest.raises(ValueError):
        parse_index({"items": []})


def test_each_entry_says_whether_it_can_be_installed(serve, tmp_path: Path) -> None:
    serve(
        {
            PRIMARY: index(
                entry(id="fresh"),
                entry(id="linetext", version="1.2.0"),
                entry(id="future-api", adapter_api_version=99),
                entry(id="future-app", min_app_version="99.0.0"),
                entry(id="text", status="official"),
            )
        }
    )
    adapter_install.install(make_package(tmp_path / "pkg"), confirmed=True)
    with patch("core.adapter_registry.bundled_ids", lambda: {"text"}):
        view = adapter_registry.fetch_index()
    states = {item["id"]: (item["state"], item["installed_version"], item["incompatible_reason"]) for item in view["entries"]}
    assert states == {
        "fresh": ("available", "", ""),
        "linetext": ("update", "1.0.0", ""),
        "future-api": ("incompatible", "", "需要更新 Translator 才能安装"),
        "future-app": ("incompatible", "", "需要 Translator 99.0.0 或更新的版本"),
        "text": ("bundled", "", ""),
    }
    with patch("core.adapter_registry.bundled_ids", lambda: {"text"}):
        assert adapter_registry.updates() == ["linetext"]


def test_incompatibility_reads_the_app_version() -> None:
    item = RegistryEntry.model_validate(entry(min_app_version="10.2.0"))
    assert item.incompatibility("10.1.0") == "需要 Translator 10.2.0 或更新的版本"
    assert item.incompatibility("10.2.0") == ""
    assert item.incompatibility("10.10.0") == ""


# ── installing from the list ────────────────────────────────────────────────


def _registry_install(client: TestClient, entry_id: str = "linetext") -> dict:
    started = client.post("/api/extensions/registry/install", json={"id": entry_id, "confirmed": True})
    assert started.status_code == 200, started.text
    return _wait_install(client)


def test_install_from_the_list(serve, tmp_path: Path, isolated: Path) -> None:  # noqa: F811
    data, digest = package_zip(tmp_path)
    serve({PRIMARY: index(entry(sha256=digest, size=len(data))), ZIP_URL: data})
    with TestClient(create_app()) as client:
        listed = client.get("/api/extensions/registry").json()
        assert listed["entries"][0]["state"] == "available"
        progress = _registry_install(client)
        assert progress["phase"] == "done", progress
        assert progress["result"]["origin"] == adapter_install.ORIGIN_REGISTRY
        assert progress["result"]["source"] == "linetext@1.0.0"
        assert KEY in [surface.key for surface in all_surfaces()]
        assert client.get("/api/extensions/registry").json()["entries"][0]["state"] == "installed"
    # The download is not kept.
    assert not any(adapter_registry.download_dir().rglob("*.zip"))


def test_a_mirror_with_the_wrong_file_is_skipped(serve, tmp_path: Path) -> None:
    data, digest = package_zip(tmp_path)
    tampered = data[:-10] + b"0123456789"
    serve(
        {
            PRIMARY: index(entry(sha256=digest, size=len(data), download=[MIRROR_URL, ZIP_URL])),
            MIRROR_URL: tampered,
            ZIP_URL: data,
        }
    )
    with TestClient(create_app()) as client:
        assert _registry_install(client)["phase"] == "done"


def test_a_file_that_does_not_match_the_list_is_not_installed(serve, tmp_path: Path) -> None:
    data, digest = package_zip(tmp_path)
    serve({PRIMARY: index(entry(sha256="f" * 64, size=len(data))), ZIP_URL: data})
    with TestClient(create_app()) as client:
        progress = _registry_install(client)
        assert progress["phase"] == "failed"
        assert progress["error"] == "扩展下载失败，请检查网络连接。"
        assert client.get("/api/extensions/installed").json()["extensions"] == []
    assert KEY not in [surface.key for surface in all_surfaces()]


def test_a_package_whose_id_or_version_differs_from_the_list_is_refused(serve, tmp_path: Path) -> None:
    data, digest = package_zip(tmp_path, version="2.0.0")
    serve({PRIMARY: index(entry(sha256=digest, size=len(data))), ZIP_URL: data})
    with TestClient(create_app()) as client:
        progress = _registry_install(client)
        assert progress["phase"] == "failed"
        assert progress["error"] == "下载的扩展与列表中的信息不一致，未安装"
        assert client.get("/api/extensions/installed").json()["extensions"] == []


def test_an_incompatible_or_unknown_entry_is_refused(serve, tmp_path: Path) -> None:
    serve({PRIMARY: index(entry(adapter_api_version=99))})
    with TestClient(create_app()) as client:
        assert _registry_install(client)["error"] == "需要更新 Translator 才能安装"
        assert _registry_install(client, "nobody")["error"] == "扩展列表里没有这个扩展，请刷新后重试。"


def test_registry_install_needs_confirmation(serve) -> None:
    serve({})
    with TestClient(create_app()) as client:
        refused = client.post("/api/extensions/registry/install", json={"id": "linetext"})
        assert refused.status_code == 422
        assert refused.json()["reason"] == "extension_unconfirmed"


def test_refresh_route_asks_the_network_again(serve) -> None:
    server = serve({PRIMARY: index(entry())})
    with TestClient(create_app()) as client:
        client.get("/api/extensions/registry")
        client.get("/api/extensions/registry")
        assert server.requests == [PRIMARY]
        refreshed = client.post("/api/extensions/registry/refresh").json()
        assert server.requests == [PRIMARY, PRIMARY]
        assert refreshed["fetched_at"] <= time.time()
