"""Claude 响应正文的提取（审计 2026-08-31 中-11）。

``_extract_claude_text`` 以前只看 ``content[0]``：网关替用户打开思考模式时首块是
thinking / redacted_thinking，真正的译文块被整个丢掉、函数返回空串，``parse_response``
随即按解析失败处理，dispatcher 把同一批二分重发——每次上游都是 200，照常计费。
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import httpx

from engines.claude_engine import ClaudeEngine, _extract_claude_text


class ExtractClaudeTextTests(unittest.TestCase):
    def test_text_block_after_a_thinking_block_is_returned(self) -> None:
        payload = {
            "content": [
                {"type": "thinking", "thinking": "let me plan the translation"},
                {"type": "text", "text": '["这是译文"]'},
            ],
            "stop_reason": "end_turn",
        }
        self.assertEqual(_extract_claude_text(payload), '["这是译文"]')

    def test_every_text_block_is_joined(self) -> None:
        payload = {
            "content": [
                {"type": "text", "text": '["A",'},
                {"type": "thinking", "thinking": "hmm"},
                {"type": "text", "text": ' "B"]'},
            ],
            "stop_reason": "end_turn",
        }
        self.assertEqual(_extract_claude_text(payload), '["A", "B"]')

    def test_no_text_block_raises_with_the_reason_and_first_block_type(self) -> None:
        # 返回空串会被下游当成「解析失败」并二分重发；必须当场抛错并说清为什么。
        payload = {
            "content": [{"type": "redacted_thinking", "data": "xxx"}],
            "stop_reason": "end_turn",
        }
        with self.assertRaises(ValueError) as caught:
            _extract_claude_text(payload)
        message = str(caught.exception)
        self.assertIn("redacted_thinking", message)
        self.assertIn("end_turn", message)

    def test_max_tokens_stop_reason_is_reported_as_truncation(self) -> None:
        payload = {
            "content": [{"type": "text", "text": '["A", "B"'}],
            "stop_reason": "max_tokens",
        }
        with self.assertRaises(ValueError) as caught:
            _extract_claude_text(payload)
        self.assertIn("截断", str(caught.exception))


class ThinkingFirstBatchTests(unittest.TestCase):
    def test_a_thinking_first_answer_translates_in_one_paid_request(self) -> None:
        # 端到端钉住「不再二分重发」：同样的 payload 以前要发 15 次才放弃。
        requests: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={
                "content": [
                    {"type": "thinking", "thinking": "plan"},
                    {"type": "text", "text": '["A", "B"]'},
                ],
                "stop_reason": "end_turn",
            })

        real_client = httpx.Client

        def client_factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return real_client(*args, **kwargs)

        engine = ClaudeEngine(api_key="k", model="claude-sonnet-5.5", base_url="https://relay.example/v1")
        with patch("httpx.Client", side_effect=client_factory):
            result = engine.translate_batch(["甲", "乙"], "fr", "规则")

        self.assertEqual(result, {"甲": "A", "乙": "B"})
        self.assertEqual(len(requests), 1)

    def test_a_text_free_answer_is_not_resent_on_the_engine_retry_budget(self) -> None:
        # 正文提取如果留在 tenacity 包住的 _call_api 里，这个 200 会被重发
        # RETRY_MAX_ATTEMPTS 次；每一次都成功计费，而结果不可能变好。
        requests: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={
                "content": [{"type": "redacted_thinking", "data": "xxx"}],
                "stop_reason": "end_turn",
            })

        real_client = httpx.Client

        def client_factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return real_client(*args, **kwargs)

        engine = ClaudeEngine(api_key="k", model="claude-sonnet-5.5", base_url="https://relay.example/v1")
        with patch("httpx.Client", side_effect=client_factory):
            with self.assertRaises(ValueError) as caught:
                engine.translate_batch(["甲"], "fr", "规则")

        self.assertEqual(len(requests), 1)
        self.assertIn("redacted_thinking", str(caught.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
