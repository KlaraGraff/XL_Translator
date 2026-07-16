from __future__ import annotations

import unittest
from unittest.mock import patch

from engines.claude_engine import ClaudeEngine
from engines.dashscope_engine import DashscopeEngine


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.calls: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def post(self, url: str, *, headers: dict, json: dict) -> _FakeResponse:
        self.calls.append({"url": url, "headers": headers, "json": json})
        return self.response


class DashscopeEngineTests(unittest.TestCase):
    def test_compatible_engines_scope_api_keys_per_http_request(self) -> None:
        client = _FakeClient(
            _FakeResponse({"choices": [{"message": {"content": "[]"}}]})
        )

        with patch("engines.openai_engine.httpx.Client", return_value=client):
            first = DashscopeEngine("key-a", "model-a")
            second = DashscopeEngine("key-b", "model-b")
            first._call_api("system", "user")
            second._call_api("system", "user")

        self.assertEqual(
            [call["headers"]["Authorization"] for call in client.calls],
            ["Bearer key-a", "Bearer key-b"],
        )
        self.assertEqual([call["json"]["model"] for call in client.calls], ["model-a", "model-b"])
        self.assertTrue(
            all(
                call["url"]
                == "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
                for call in client.calls
            )
        )

    def test_claude_engine_preserves_messages_protocol(self) -> None:
        client = _FakeClient(
            _FakeResponse({"content": [{"type": "text", "text": "[]"}]})
        )

        with patch("engines.claude_engine.httpx.Client", return_value=client):
            response = ClaudeEngine("claude-key", "claude-model")._call_api("system", "user")

        self.assertEqual(response, "[]")
        self.assertEqual(client.calls[0]["url"], "https://api.anthropic.com/v1/messages")
        self.assertEqual(client.calls[0]["headers"]["x-api-key"], "claude-key")
        self.assertEqual(client.calls[0]["headers"]["anthropic-version"], "2023-06-01")
        self.assertEqual(client.calls[0]["json"]["max_tokens"], 8096)


if __name__ == "__main__":
    unittest.main(verbosity=2)
