"""小米 MiMo 预设（反馈 #17）：和 DeepSeek 同一条路，OpenAI 兼容、能看图、不能出图。

官方文档（mimo.mi.com/docs）核对过的事实：按量付费地址 https://api.xiaomimimo.com/v1，
支持 Bearer；GET /v1/models 可用，但把语音识别、语音合成模型和对话模型列在一起；
mimo-v2.6 系列读图走 /chat/completions 的 image_url，没有生成图片的模型。
"""

from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from fastapi.testclient import TestClient

from api.app import create_app
from config import (
    CLOUD_ENGINES,
    CLOUD_PROVIDER_API_KEY_PAGES,
    XIAOMI_BASE_URL,
    cloud_provider_base_url_default,
    normalize_cloud_base_url,
)
from core.connectivity_check import OPENAI_COMPATIBLE_PROVIDERS
from core.engine_dispatcher import build_engine
from core.model_catalog import (
    OPENAI_COMPATIBLE_MODEL_PROVIDERS,
    clear_model_catalog_cache,
    fetch_openai_compatible_models,
)
from core.model_roles import (
    ROLE_IMAGE,
    ROLE_PDF_REVIEW,
    ROLE_TRANSLATION,
    SOURCE_INDEPENDENT,
    provider_display_name,
    provider_supports_capability,
    role_capability,
)
from core.pdf_review import check_pdf_review_connectivity
from engines.openai_engine import OpenAIEngine
from settings import AppSettings, _api_key_env_names

CHAT_MODELS = ["mimo-v2.6-pro", "mimo-v2.6-flash", "mimo-v2.6-pro-ultraspeed"]
SPEECH_MODELS = [
    "mimo-v2.5-asr",
    "mimo-v2.5-tts",
    "mimo-v2.5-tts-voiceclone",
    "mimo-v2.5-tts-voicedesign",
]


class XiaomiPresetTests(unittest.TestCase):
    def test_listed_as_a_cloud_provider(self) -> None:
        self.assertEqual(CLOUD_ENGINES.get("小米 MiMo"), "xiaomi")
        self.assertEqual(provider_display_name("xiaomi"), "小米 MiMo")

    def test_default_base_url_is_the_pay_as_you_go_endpoint(self) -> None:
        self.assertEqual(XIAOMI_BASE_URL, "https://api.xiaomimimo.com/v1")
        self.assertEqual(cloud_provider_base_url_default("xiaomi"), XIAOMI_BASE_URL)
        self.assertEqual(normalize_cloud_base_url("xiaomi", ""), XIAOMI_BASE_URL)

    def test_reads_images_but_cannot_generate_them(self) -> None:
        self.assertTrue(provider_supports_capability("xiaomi", role_capability(ROLE_TRANSLATION)))
        self.assertTrue(provider_supports_capability("xiaomi", role_capability(ROLE_PDF_REVIEW)))
        self.assertFalse(provider_supports_capability("xiaomi", role_capability(ROLE_IMAGE)))

    def test_model_list_and_connection_test_accept_it(self) -> None:
        self.assertIn("xiaomi", OPENAI_COMPATIBLE_PROVIDERS)
        self.assertIn("xiaomi", OPENAI_COMPATIBLE_MODEL_PROVIDERS)

    def test_reads_its_key_from_the_conventional_environment_variables(self) -> None:
        self.assertEqual(_api_key_env_names("xiaomi"), ("XIAOMI_API_KEY", "MIMO_API_KEY"))

    def test_dispatches_to_the_openai_compatible_engine(self) -> None:
        settings = AppSettings()
        settings.engine.mode = "cloud"
        settings.engine.source_role = "independent"
        settings.engine.cloud_provider = "xiaomi"
        settings.engine.cloud_model = "mimo-v2.6-flash"
        settings.engine.cloud_base_url = XIAOMI_BASE_URL

        self.assertIsInstance(build_engine(settings), OpenAIEngine)

    def test_key_console_links(self) -> None:
        """按量付费的密钥在 API Keys 页；Token Plan（tp- 开头）的在套餐管理页。"""
        client = TestClient(create_app())
        payload = client.get("/api/models/provider-defaults").json()

        self.assertEqual(payload["base_url_defaults"]["xiaomi"], XIAOMI_BASE_URL)
        self.assertNotIn("model_defaults", payload)
        self.assertEqual(
            payload["api_key_pages"]["xiaomi"],
            "https://platform.xiaomimimo.com/#/console/api-keys",
        )
        self.assertEqual(payload["api_key_pages"], CLOUD_PROVIDER_API_KEY_PAGES)
        for region in ("cn", "sgp", "ams"):
            self.assertEqual(
                payload["api_key_pages_by_host"][f"token-plan-{region}.xiaomimimo.com"],
                "https://platform.xiaomimimo.com/#/console/plan-manage",
            )


class _FakeMiMo(BaseHTTPRequestHandler):
    """只有 /models 和 /chat/completions；mimo-v2.6 系列都认图。"""

    calls: list[dict] = []

    def log_message(self, *_args) -> None:
        return None

    def _send_json(self, status: int, payload) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if self.headers.get("Authorization") == "Bearer sk-test":
            return True
        self._send_json(401, {"error": {"message": "Invalid API Key"}})
        return False

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self.calls.append({"path": self.path, "json": None})
        if not self._authorized():
            return
        if self.path != "/v1/models":
            self._send_json(404, {"error": {"message": "Not Found"}})
            return
        data = [{"id": m, "object": "model", "owned_by": "xiaomi"} for m in CHAT_MODELS + SPEECH_MODELS]
        self._send_json(200, {"object": "list", "data": data})

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        self.calls.append({"path": self.path, "json": payload})
        if not self._authorized():
            return
        if self.path != "/v1/chat/completions":
            self._send_json(404, {"error": {"message": "Not Found"}})
            return
        if payload.get("model") not in CHAT_MODELS:
            self._send_json(400, {"error": {"message": "Not supported model"}})
            return
        answer = json.dumps({"pass": True, "blocking_issues": [], "minor_suggestions": [], "summary": "ok"})
        self._send_json(200, {"choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "reasoning_content": "…", "content": answer,
        }}]})


class XiaomiOverHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeMiMo)
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}/v1"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        _FakeMiMo.calls = []
        clear_model_catalog_cache()

    def test_model_list_leaves_out_speech_models(self) -> None:
        for capability in ("text", "vision_text"):
            with self.subTest(capability=capability):
                result = fetch_openai_compatible_models(
                    provider="xiaomi", api_key="sk-test", base_url=self.base_url, capability=capability,
                )
                self.assertTrue(result.ok, result.message)
                self.assertEqual(sorted(result.models), sorted(CHAT_MODELS))

    def test_custom_configuration_keeps_the_full_list(self) -> None:
        """「自定义配置」不知道对面是谁，列表照原样给。"""
        result = fetch_openai_compatible_models(
            provider="custom_openai", api_key="sk-test", base_url=self.base_url,
        )
        self.assertTrue(result.ok, result.message)
        self.assertTrue(set(SPEECH_MODELS) <= set(result.models))

    def test_review_goes_straight_to_chat_with_images(self) -> None:
        settings = AppSettings()
        role = settings.pdf_review_model_role
        role.source_role = SOURCE_INDEPENDENT
        role.cloud_provider = "xiaomi"
        role.cloud_model = "mimo-v2.6-flash"
        role.cloud_base_url = self.base_url
        with patch("core.model_roles.get_key", return_value="sk-test"):
            result = check_pdf_review_connectivity(settings, max_attempts=1)
        self.assertTrue(result.ok, result.message)
        self.assertEqual([call["path"] for call in _FakeMiMo.calls], ["/v1/chat/completions"])
        content = _FakeMiMo.calls[0]["json"]["messages"][-1]["content"]
        self.assertEqual([part["type"] for part in content], ["text", "image_url", "image_url"])


if __name__ == "__main__":
    unittest.main()
