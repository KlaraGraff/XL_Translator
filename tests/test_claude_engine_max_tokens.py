"""Claude 引擎的输出上限：不再对所有型号一律发 8096（CODE_AUDIT_2026-08-31 中-10）。

Claude 3 系（opus/sonnet/haiku）单次最多输出 4096 个 token，请求里写超了直接
400 invalid_request_error；400 在批次重试里算「不可重试」，于是整批原样退回。
而连通性测试只发 8 个 token，永远全绿——用户看到的是「测试通过、一格都没翻」。

这里用假的 Anthropic 端点复现「按型号的实际输出上限拒绝」，不走网络、不用密钥。
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import httpx

from core import connectivity_check
from engines.claude_engine import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    KNOWN_MAX_OUTPUT_TOKENS,
    ClaudeEngine,
    model_max_output_tokens,
    rejected_max_tokens,
)

_REAL_CLIENT = httpx.Client


class _MaxTokensCapTransport(httpx.BaseTransport):
    """Anthropic 那样的端点：max_tokens 超过型号上限就回 400。"""

    def __init__(self, cap: int) -> None:
        self.cap = cap
        self.sent: list[dict] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content or b"{}")
        self.sent.append(payload)
        if int(payload.get("max_tokens", 0)) > self.cap:
            return httpx.Response(
                400,
                json={
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "message": (
                            f"max_tokens: {payload['max_tokens']} > {self.cap}, which is the "
                            "maximum allowed number of tokens for this model."
                        ),
                    },
                },
                request=request,
            )
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": '["Bienvenue"]'}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
            request=request,
        )


def _client_factory(transport: _MaxTokensCapTransport):
    def build(*args, **kwargs):
        kwargs.pop("transport", None)
        return _REAL_CLIENT(transport=transport, **kwargs)

    return build


class ClaudeOutputLimitTests(unittest.TestCase):
    def test_a_4096_cap_model_translates_instead_of_returning_the_source(self) -> None:
        """测试全绿、每批 400、整份文档原样退回——这条回归的就是它。"""
        transport = _MaxTokensCapTransport(cap=4096)
        with patch("httpx.Client", _client_factory(transport)):
            result = ClaudeEngine("claude-key", "claude-3-haiku").translate_batch(
                ["Welcome"], "fr", "Translate."
            )

        self.assertEqual(result, {"Welcome": "Bienvenue"})
        self.assertEqual(transport.sent[0]["max_tokens"], 4096)
        # 上限认得准，就不该先发一次会被拒的请求。
        self.assertEqual(len(transport.sent), 1)

    def test_the_connectivity_probe_is_green_for_the_same_model(self) -> None:
        """同一个型号「测试连接」一直是绿的——这正是这个 bug 藏得住的原因。

        这里只断言连接测试能过，不再钉死它发多少 token：探针值本身不是本 bug 的
        契约，钉住它会让以后「连接测试用和真实翻译一致的输出上限」这条改进绊住。
        """
        transport = _MaxTokensCapTransport(cap=4096)
        with patch("httpx.Client", _client_factory(transport)):
            result = connectivity_check._check_claude(
                api_key="claude-key",
                model="claude-3-haiku",
            )

        self.assertTrue(result.ok, result.message)

    def test_a_relay_model_with_a_lower_cap_is_learned_once(self) -> None:
        """型号名认不出、实际上限更低：降一档重发，之后每批都不再撞。"""
        transport = _MaxTokensCapTransport(cap=2048)
        engine = ClaudeEngine("claude-key", "my-claude-relay")
        with patch("httpx.Client", _client_factory(transport)):
            first = engine.translate_batch(["Welcome"], "fr", "Translate.")
            second = engine.translate_batch(["Welcome"], "fr", "Translate.")

        self.assertEqual(first, {"Welcome": "Bienvenue"})
        self.assertEqual(second, {"Welcome": "Bienvenue"})
        self.assertEqual(
            [body["max_tokens"] for body in transport.sent], [4096, 2048, 2048]
        )

    def test_a_400_about_something_else_is_not_swallowed(self) -> None:
        """只有「嫌 max_tokens 太大」才在这里消化，别的 400 照旧交给上层。"""
        transport = _MaxTokensCapTransport(cap=4096)

        def reject(request: httpx.Request) -> httpx.Response:
            transport.sent.append(json.loads(request.content or b"{}"))
            return httpx.Response(
                400,
                json={"error": {"message": "invalid api key"}},
                request=request,
            )

        with patch("httpx.Client", lambda **kwargs: _REAL_CLIENT(
            transport=httpx.MockTransport(reject), **kwargs
        )):
            with self.assertRaises(httpx.HTTPStatusError):
                ClaudeEngine("claude-key", "claude-3-haiku").translate_batch(
                    ["Welcome"], "fr", "Translate."
                )
        self.assertEqual(len(transport.sent), 1)

    def test_newer_models_keep_the_larger_output_budget(self) -> None:
        self.assertEqual(model_max_output_tokens("claude-3-haiku-20240307"), 4096)
        self.assertEqual(model_max_output_tokens("claude-3-opus-20240229"), 4096)
        self.assertEqual(model_max_output_tokens("claude-3-5-sonnet-20241022"), KNOWN_MAX_OUTPUT_TOKENS)
        self.assertEqual(model_max_output_tokens("claude-sonnet-4-5"), KNOWN_MAX_OUTPUT_TOKENS)
        # 认不出的型号走所有 Claude 型号都接受的保守值，宁可少给也不被 400 退回。
        self.assertEqual(model_max_output_tokens("some-relay-model"), DEFAULT_MAX_OUTPUT_TOKENS)

    def test_the_limit_is_read_from_either_wording(self) -> None:
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        comparisons = httpx.Response(
            400,
            json={"error": {"message": "max_tokens: 8096 > 4096, which is the maximum allowed number of tokens"}},
            request=request,
        )
        ceilings = httpx.Response(
            400,
            json={"error": {"message": "max_tokens must be less than or equal to 2048"}},
            request=request,
        )
        relays = httpx.Response(
            400,
            json={"error": {"message": "max_tokens is too large: 8096 (maximum 4096)"}},
            request=request,
        )
        unrelated = httpx.Response(
            400, json={"error": {"message": "invalid api key"}}, request=request
        )
        positive = httpx.Response(
            400,
            json={"error": {"message": "max_tokens must be a positive integer"}},
            request=request,
        )
        ok = httpx.Response(200, json={}, request=request)

        self.assertEqual(rejected_max_tokens(comparisons, 8096), 4096)
        self.assertEqual(rejected_max_tokens(ceilings, 4096), 2048)
        self.assertEqual(rejected_max_tokens(relays, 8096), 4096)
        self.assertIsNone(rejected_max_tokens(unrelated, 8096))
        # 同字段但意思不同的 400 不能被当成「输出上限不够」去重发。
        self.assertIsNone(rejected_max_tokens(positive, 8096))
        self.assertIsNone(rejected_max_tokens(ok, 8096))

    def test_a_message_without_a_ceiling_is_not_cut_to_its_own_request_value(self) -> None:
        """「max_tokens is too large: 8096」只说了请求值，没有上限可读。"""
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        vague = httpx.Response(
            400,
            json={"error": {"message": "max_tokens is too large: 8096"}},
            request=request,
        )
        # 没给出上限时按半递减，而不是把 8096 当成上限再发一次同样的值。
        self.assertEqual(rejected_max_tokens(vague, 8096), 4048)


if __name__ == "__main__":
    unittest.main(verbosity=2)
