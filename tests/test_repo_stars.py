"""设置「更新与关于」页「去点 Star」旁边的 Star 数：查 GitHub、缓存、查不到时不显示。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
from fastapi.testclient import TestClient

import settings as settings_module
from api.app import create_app
from core import repo_stars


def _client_returning(payload=None, *, error: Exception | None = None) -> MagicMock:
    response = MagicMock()
    if error is not None:
        response.raise_for_status.side_effect = error
    response.json.return_value = payload
    client = MagicMock()
    client.get.return_value = response
    context = MagicMock()
    context.__enter__.return_value = client
    context.__exit__.return_value = False
    return context


class FetchStarCountTests(unittest.TestCase):
    def test_reads_stargazers_count(self) -> None:
        with patch.object(repo_stars.system_proxy, "http_client", return_value=_client_returning({"stargazers_count": 15})):
            self.assertEqual(repo_stars.fetch_star_count(), 15)

    def test_bad_payload_is_none(self) -> None:
        for payload in ({}, {"stargazers_count": "15"}, {"stargazers_count": True}, {"stargazers_count": -1}, []):
            with self.subTest(payload=payload), patch.object(
                repo_stars.system_proxy, "http_client", return_value=_client_returning(payload)
            ):
                self.assertIsNone(repo_stars.fetch_star_count())

    def test_http_error_is_none_not_raised(self) -> None:
        error = httpx.HTTPStatusError("403", request=MagicMock(), response=MagicMock())
        with patch.object(repo_stars.system_proxy, "http_client", return_value=_client_returning(error=error)):
            self.assertIsNone(repo_stars.fetch_star_count())

    def test_dead_proxy_retries_direct(self) -> None:
        calls: list[bool] = []

        def fake_client(**kwargs):
            calls.append(kwargs["trust_env"])
            if kwargs["trust_env"]:
                raise httpx.ProxyError("proxy down")
            return _client_returning({"stargazers_count": 7})

        with patch.object(repo_stars.system_proxy, "http_client", side_effect=fake_client), patch.object(
            repo_stars.system_proxy, "should_retry_direct", return_value=True
        ):
            self.assertEqual(repo_stars.fetch_star_count(), 7)
        self.assertEqual(calls, [True, False])


class CachedStarCountTests(unittest.TestCase):
    def setUp(self) -> None:
        repo_stars.reset_cache()
        self.addCleanup(repo_stars.reset_cache)

    def test_success_is_kept_for_a_day(self) -> None:
        fetch = MagicMock(side_effect=[15, 16])
        clock = [1000.0]
        now = lambda: clock[0]  # noqa: E731
        self.assertEqual(repo_stars.cached_star_count(now=now, fetch=fetch), 15)
        clock[0] += repo_stars.SUCCESS_TTL_SECONDS - 1
        self.assertEqual(repo_stars.cached_star_count(now=now, fetch=fetch), 15)
        self.assertEqual(fetch.call_count, 1)
        clock[0] += 2
        self.assertEqual(repo_stars.cached_star_count(now=now, fetch=fetch), 16)
        self.assertEqual(fetch.call_count, 2)

    def test_failure_is_retried_after_an_hour(self) -> None:
        fetch = MagicMock(side_effect=[None, 15])
        clock = [1000.0]
        now = lambda: clock[0]  # noqa: E731
        self.assertIsNone(repo_stars.cached_star_count(now=now, fetch=fetch))
        clock[0] += repo_stars.FAILURE_TTL_SECONDS - 1
        self.assertIsNone(repo_stars.cached_star_count(now=now, fetch=fetch))
        self.assertEqual(fetch.call_count, 1)
        clock[0] += 2
        self.assertEqual(repo_stars.cached_star_count(now=now, fetch=fetch), 15)


class StarsRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        patcher = patch.multiple(
            settings_module,
            APP_DATA_DIR=root / "app-data",
            SETTINGS_PATH=root / "app-data" / "settings.json",
            KEYS_PATH=root / "app-data" / "keys.json",
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        repo_stars.reset_cache()
        self.addCleanup(repo_stars.reset_cache)
        self.client = TestClient(create_app())

    def test_returns_count_and_repo_url(self) -> None:
        with patch("core.repo_stars.fetch_star_count", return_value=15):
            body = self.client.get("/api/updates/stars").json()
        self.assertEqual(body, {"stars": 15, "repo_url": "https://github.com/KlaraGraff/XL_Translator"})

    def test_unreachable_github_gives_null(self) -> None:
        with patch("core.repo_stars.fetch_star_count", return_value=None):
            response = self.client.get("/api/updates/stars")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["stars"])

    def test_paused_background_checks_do_not_go_online(self) -> None:
        self.client.put("/api/updates/preferences", json={"notifications_paused": True})
        with patch("core.repo_stars.fetch_star_count") as fetch:
            body = self.client.get("/api/updates/stars").json()
        fetch.assert_not_called()
        self.assertIsNone(body["stars"])


if __name__ == "__main__":
    unittest.main()
