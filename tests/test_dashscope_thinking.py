"""阿里百炼「深度思考」开关：设置怎么存、怎么变成请求参数。"""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import httpx

from core.text_transport import clear_protocol_cache
from engines.dashscope_engine import DashscopeEngine, dashscope_thinking_params
from settings import ModelConnection

_OK = {"choices": [{"finish_reason": "stop", "message": {"content": "[\"A\"]"}}]}


def _run(engine: DashscopeEngine, responses: list[httpx.Response]) -> list[dict]:
    sent: list[dict] = []
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return responses[min(len(sent), len(responses)) - 1]

    def build_client(**kwargs):
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    with patch("core.text_transport.httpx.AsyncClient", side_effect=build_client):
        assert engine.translate_batch(["甲"], "fr", "") == {"甲": "A"}
    return sent


class DashscopeThinkingTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_protocol_cache()

    def test_params_per_setting(self) -> None:
        self.assertEqual(dashscope_thinking_params("auto"), {})
        self.assertEqual(dashscope_thinking_params("off"), {"enable_thinking": False})
        self.assertEqual(dashscope_thinking_params("on"), {"enable_thinking": True})

    def test_connection_defaults_to_auto_and_rejects_unknown_values(self) -> None:
        self.assertEqual(ModelConnection().thinking, "auto")
        self.assertEqual(ModelConnection(thinking="off").thinking, "off")
        self.assertEqual(ModelConnection(thinking="maybe").thinking, "auto")

    def test_auto_sends_no_thinking_parameter(self) -> None:
        sent = _run(DashscopeEngine("k", "qwen3.6-plus"), [httpx.Response(200, json=_OK)])
        self.assertNotIn("enable_thinking", sent[0])

    def test_off_and_on_reach_the_chat_request(self) -> None:
        for mode, expected in (("off", False), ("on", True)):
            clear_protocol_cache()
            sent = _run(DashscopeEngine("k", "qwen3.6-plus", thinking=mode), [httpx.Response(200, json=_OK)])
            self.assertIs(sent[0]["enable_thinking"], expected)

    def test_model_that_rejects_the_switch_falls_back_to_its_default(self) -> None:
        rejected = httpx.Response(
            400,
            json={"error": {"code": "invalid_parameter_error",
                            "message": "The value of the enable_thinking parameter is restricted to True."}},
        )
        sent = _run(
            DashscopeEngine("k", "qwen3.7-max-preview", thinking="off"),
            [rejected, httpx.Response(200, json=_OK)],
        )
        self.assertEqual(len(sent), 2)
        self.assertIs(sent[0]["enable_thinking"], False)
        self.assertNotIn("enable_thinking", sent[1])


class ThinkingSettingFlowTests(unittest.TestCase):
    def test_saved_setting_reaches_the_built_engine(self) -> None:
        from core.engine_dispatcher import build_engine
        from core.model_roles import (
            ROLE_TRANSLATION, resolve_effective_model_config, settings_for_text_role,
            update_role_connection,
        )
        from settings import AppSettings

        settings = AppSettings()
        primary = settings.engine.connections[0]
        update_role_connection(
            settings, ROLE_TRANSLATION, primary.id,
            connection_mode="cloud", provider="dashscope", model="qwen3.6-plus",
            thinking="off",
        )
        self.assertEqual(resolve_effective_model_config(settings, ROLE_TRANSLATION).thinking, "off")
        engine = build_engine(settings_for_text_role(settings, ROLE_TRANSLATION))
        self.assertIsInstance(engine, DashscopeEngine)
        self.assertEqual(engine._chat_extra, {"enable_thinking": False})


if __name__ == "__main__":
    unittest.main()
