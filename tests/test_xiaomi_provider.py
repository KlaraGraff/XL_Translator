"""小米 MiMo 预设（反馈 #17）：和 DeepSeek 同一条路，OpenAI 兼容、能看图、不能出图。

官方文档（mimo.mi.com/docs）核对过的事实：Token Plan 套餐地址按地区
https://token-plan-{cn,sgp,ams}.xiaomimimo.com/v1，按量付费地址 https://api.xiaomimimo.com/v1，
两者密钥不通用；多数用户用 Token Plan，默认填国内套餐地址（maple 2026-10-22）。支持 Bearer；GET /v1/models 可用，但把语音识别、语音合成模型和对话模型列在一起；
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
    CLOUD_PROVIDER_BASE_URL_HINTS,
    CLOUD_PROVIDER_BASE_URL_OPTIONS,
    CLOUD_PROVIDER_CONNECTION_FAILURE_HINTS,
    XIAOMI_BASE_URL,
    XIAOMI_PAY_AS_YOU_GO_BASE_URL,
    cloud_provider_base_url_default,
    normalize_cloud_base_url,
    with_connection_failure_hint,
)
from core.connectivity_check import OPENAI_COMPATIBLE_PROVIDERS, ConnectivityResult
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
from settings import AppSettings, _api_key_env_names, load_settings, save_key, save_settings
from tests.app_data_isolation import IsolatedAppDataTestCase

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

    def test_default_base_url_is_the_token_plan_endpoint(self) -> None:
        self.assertEqual(XIAOMI_BASE_URL, "https://token-plan-cn.xiaomimimo.com/v1")
        self.assertEqual(XIAOMI_PAY_AS_YOU_GO_BASE_URL, "https://api.xiaomimimo.com/v1")
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

    def test_key_console_links_and_base_url_hint(self) -> None:
        """Token Plan（tp- 开头）的密钥在套餐管理页；按量付费的在 API Keys 页。"""
        client = TestClient(create_app())
        payload = client.get("/api/models/provider-defaults").json()

        self.assertEqual(payload["base_url_defaults"]["xiaomi"], XIAOMI_BASE_URL)
        self.assertNotIn("model_defaults", payload)
        self.assertEqual(
            payload["api_key_pages"]["xiaomi"],
            "https://platform.xiaomimimo.com/#/console/plan-manage",
        )
        self.assertEqual(payload["api_key_pages"], CLOUD_PROVIDER_API_KEY_PAGES)
        self.assertEqual(
            payload["api_key_pages_by_host"]["api.xiaomimimo.com"],
            "https://platform.xiaomimimo.com/#/console/api-keys",
        )
        # Base URL 旁的说明写明两种地址不同；下拉里列出三个 Token Plan 集群和按量付费地址，
        # 第一项就是默认地址。
        self.assertEqual(payload["base_url_hints"], CLOUD_PROVIDER_BASE_URL_HINTS)
        self.assertIn("Token Plan", payload["base_url_hints"]["xiaomi"])
        options = payload["base_url_options"]["xiaomi"]
        self.assertEqual(options, CLOUD_PROVIDER_BASE_URL_OPTIONS["xiaomi"])
        self.assertEqual(options[0]["url"], XIAOMI_BASE_URL)
        self.assertEqual(
            [option["url"] for option in options],
            [
                "https://token-plan-cn.xiaomimimo.com/v1",
                "https://token-plan-sgp.xiaomimimo.com/v1",
                "https://token-plan-ams.xiaomimimo.com/v1",
                XIAOMI_PAY_AS_YOU_GO_BASE_URL,
            ],
        )
        self.assertNotIn("deepseek", payload["base_url_options"])

    def test_failure_hint_only_for_real_failures(self) -> None:
        hint = CLOUD_PROVIDER_CONNECTION_FAILURE_HINTS["xiaomi"]
        failed = ConnectivityResult(ok=False, status="request_failed", message="连接测试失败：HTTP 401")
        self.assertEqual(
            with_connection_failure_hint("xiaomi", failed).message,
            f"连接测试失败：HTTP 401。{hint}",
        )
        # 表单没填全、测试通过、别家服务商：原样返回。
        for provider, result in (
            ("xiaomi", ConnectivityResult(ok=False, status="missing_api_key", message="请先填写 API 密钥。")),
            ("xiaomi", ConnectivityResult(ok=True, status="ok", message="测试通过。")),
            ("deepseek", failed),
        ):
            self.assertIs(with_connection_failure_hint(provider, result), result)


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
        content = payload["messages"][-1]["content"]
        if isinstance(content, list):  # 审核：带图
            answer = json.dumps({"pass": True, "blocking_issues": [], "minor_suggestions": [], "summary": "ok"})
        else:  # 文本测试连接要求只回 OK；mimo-v2.6-pro 在这里故意答非所问
            answer = "OK" if payload["model"] != "mimo-v2.6-pro" else "好的"
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



class XiaomiFailureHintRouteTests(IsolatedAppDataTestCase):
    """测试连接、获取模型列表失败时，小米额外提示检查 Base URL（套餐地址和按量付费地址不通用）。"""

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
        super().setUp()
        clear_model_catalog_cache()
        self._save_settings("mimo-v2.6-flash")
        self.client = TestClient(create_app())
        self.hint = CLOUD_PROVIDER_CONNECTION_FAILURE_HINTS["xiaomi"]

    def _save_settings(self, model: str) -> None:
        settings = AppSettings()
        settings.engine.mode = "cloud"
        settings.engine.cloud_provider = "xiaomi"
        settings.engine.cloud_model = model
        settings.engine.cloud_base_url = self.base_url
        save_settings(AppSettings(**settings.model_dump()))

    def test_wrong_key_points_at_the_base_url(self) -> None:
        save_key("xiaomi", "sk-wrong", self.base_url)
        result = self.client.post("/api/models/connectivity/translation", json={}).json()
        self.assertFalse(result["ok"])
        self.assertTrue(result["message"].endswith(self.hint), result["message"])
        # 连接卡片上的「不可用」原因也带着这句。
        self.assertIn(self.hint, load_settings().engine.availability_message)

        catalog = self.client.post("/api/models/catalog/translation", json={"refresh": True}).json()
        self.assertFalse(catalog["ok"])
        self.assertTrue(catalog["message"].endswith(self.hint), catalog["message"])

    def test_passing_test_and_missing_key_carry_no_hint(self) -> None:
        missing = self.client.post("/api/models/connectivity/translation", json={}).json()
        self.assertFalse(missing["ok"])
        self.assertNotIn(self.hint, missing["message"])

        save_key("xiaomi", "sk-test", self.base_url)
        passed = self.client.post("/api/models/connectivity/translation", json={}).json()
        self.assertTrue(passed["ok"], passed["message"])
        self.assertNotIn(self.hint, passed["message"])

    def test_connected_but_wrong_answer_is_not_blamed_on_the_address(self) -> None:
        self._save_settings("mimo-v2.6-pro")
        save_key("xiaomi", "sk-test", self.base_url)
        result = self.client.post("/api/models/connectivity/translation", json={}).json()
        self.assertFalse(result["ok"])
        self.assertNotIn(self.hint, result["message"])


if __name__ == "__main__":
    unittest.main()
