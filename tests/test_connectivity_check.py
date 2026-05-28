from __future__ import annotations

import unittest
from unittest.mock import patch

from core.connectivity_check import (
    _check_ollama_model,
    _check_openai_compatible,
)
from settings import AppSettings, EngineSettings


class _FakeResponse:
    def __init__(self, payload=None, *, status_code: int = 200, text: str = "") -> None:
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.text = text

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, *, get_response=None, post_response=None) -> None:
        self.get_response = get_response or _FakeResponse()
        self.post_response = post_response or _FakeResponse()
        self.get_calls: list[str] = []
        self.post_calls: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def get(self, url: str):
        self.get_calls.append(url)
        return self.get_response

    def post(self, url: str, *, headers=None, json=None):
        self.post_calls.append({"url": url, "headers": headers, "json": json})
        return self.post_response


class ConnectivityCheckTests(unittest.TestCase):
    def _patch_client(self, fake_client: _FakeClient):
        return patch(
            "core.connectivity_check.httpx.Client",
            autospec=True,
            return_value=fake_client,
        )

    def test_ollama_model_found_returns_ok(self) -> None:
        fake_client = _FakeClient(
            get_response=_FakeResponse(
                {"models": [{"name": "qwen2.5:14b"}, {"model": "llama3.1:8b"}]}
            )
        )

        with self._patch_client(fake_client):
            result = _check_ollama_model("qwen2.5:14b")

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "ok")
        self.assertTrue(fake_client.get_calls[0].endswith("/api/tags"))

    def test_ollama_model_missing_reports_model_name(self) -> None:
        fake_client = _FakeClient(
            get_response=_FakeResponse({"models": [{"name": "llama3.1:8b"}]})
        )

        with self._patch_client(fake_client):
            result = _check_ollama_model("qwen2.5:14b")

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "model_missing")
        self.assertIn("qwen2.5:14b", result.message)

    def test_check_connectivity_requires_api_key(self) -> None:
        from core.connectivity_check import check_connectivity

        settings = AppSettings(
            engine=EngineSettings(
                mode="cloud",
                cloud_provider="custom_openai",
                cloud_model="gpt-5.4",
                cloud_base_url="https://api.example.test/v1",
            )
        )

        with patch("core.connectivity_check.get_key", return_value=""):
            result = check_connectivity(settings)

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "missing_api_key")

    def test_local_lm_studio_connectivity_does_not_require_api_key(self) -> None:
        from core.connectivity_check import check_connectivity

        settings = AppSettings(
            engine=EngineSettings(
                mode="local",
                local_provider="lm_studio",
                local_model="qwen-local",
                local_base_url="http://localhost:1234/v1",
            )
        )
        fake_client = _FakeClient(post_response=_FakeResponse({"id": "chatcmpl_1"}))

        with self._patch_client(fake_client):
            result = check_connectivity(settings)

        self.assertTrue(result.ok)
        self.assertEqual(result.provider, "lm_studio")
        self.assertEqual(
            fake_client.post_calls[0]["url"],
            "http://localhost:1234/v1/chat/completions",
        )
        self.assertNotIn("Authorization", fake_client.post_calls[0]["headers"])

    def test_openai_compatible_uses_responses_route_for_asxs(self) -> None:
        fake_client = _FakeClient(post_response=_FakeResponse({"id": "resp_1"}))

        with self._patch_client(fake_client):
            result = _check_openai_compatible(
                provider="custom_openai",
                api_key="secret",
                model="gpt-5.4",
                base_url="https://api.asxs.top/v1",
            )

        self.assertTrue(result.ok)
        self.assertEqual(fake_client.post_calls[0]["url"], "https://api.asxs.top/v1/responses")
        self.assertEqual(fake_client.post_calls[0]["json"]["model"], "gpt-5.4")

    def test_openai_error_does_not_echo_api_key(self) -> None:
        fake_client = _FakeClient(
            post_response=_FakeResponse(
                {},
                status_code=401,
                text="invalid secret-token",
            )
        )

        with self._patch_client(fake_client):
            result = _check_openai_compatible(
                provider="custom_openai",
                api_key="secret-token",
                model="gpt-5.4",
                base_url="https://api.example.test/v1",
            )

        self.assertFalse(result.ok)
        self.assertNotIn("secret-token", result.message)
        self.assertIn("***", result.message)

    def test_official_openai_uses_configured_base_url(self) -> None:
        from core.engine_dispatcher import build_engine

        settings = AppSettings(
            engine=EngineSettings(
                mode="cloud",
                cloud_provider="openai",
                cloud_model="gpt-5.4",
                cloud_base_url="https://api.example.test/v1",
            )
        )

        with (
            patch("core.engine_dispatcher.get_key", return_value="secret"),
            patch("engines.openai_engine.OpenAIEngine") as engine_cls,
        ):
            build_engine(settings)

        self.assertEqual(engine_cls.call_args.kwargs["base_url"], "https://api.example.test/v1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
