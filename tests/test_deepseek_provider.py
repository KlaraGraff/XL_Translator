"""DeepSeek is a first-class cloud provider, and its preset reaches the form.

The panel used to leave Base URL blank when a provider was picked: the default
only got applied server-side at save time, so a preset that existed in
``config.py`` was invisible until after the user had already typed one in.  The
provider-defaults route is what lets the form prefill before the save, so it is
covered here alongside the provider registration itself.
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
    CLOUD_PROVIDER_BASE_URL_DEFAULTS,
    DEEPSEEK_BASE_URL,
    cloud_provider_base_url_default,
    normalize_cloud_base_url,
)
from core.connectivity_check import OPENAI_COMPATIBLE_PROVIDERS
from core.engine_dispatcher import build_engine
from core.model_catalog import OPENAI_COMPATIBLE_MODEL_PROVIDERS
from core.model_roles import (
    ROLE_IMAGE,
    ROLE_PDF_REVIEW,
    ROLE_TRANSLATION,
    SOURCE_INDEPENDENT,
    provider_supports_capability,
    role_capability,
)
from core.pdf_review import REVIEW_MODEL_ADVICE, check_pdf_review_connectivity
from engines.openai_engine import OpenAIEngine
from settings import AppSettings, _api_key_env_names
from tests.image_probe_reader import probe_code_in_request


class DeepSeekProviderTests(unittest.TestCase):
    def test_listed_as_a_cloud_provider(self) -> None:
        self.assertEqual(CLOUD_ENGINES.get("DeepSeek"), "deepseek")

    def test_default_base_url_is_the_official_endpoint(self) -> None:
        self.assertEqual(CLOUD_PROVIDER_BASE_URL_DEFAULTS["deepseek"], DEEPSEEK_BASE_URL)
        self.assertEqual(cloud_provider_base_url_default("deepseek"), DEEPSEEK_BASE_URL)

    def test_blank_base_url_falls_back_to_the_preset(self) -> None:
        self.assertEqual(normalize_cloud_base_url("deepseek", ""), DEEPSEEK_BASE_URL)

    def test_reads_images_but_cannot_generate_them(self) -> None:
        """DeepSeek 的 API 自 2026-08-21 起接受图片输入（deepseek-flash），但不出图。

        以前审核用途也把它挡在门外，用户选 DeepSeek 存都存不下（反馈 #16）。
        """
        self.assertTrue(
            provider_supports_capability("deepseek", role_capability(ROLE_TRANSLATION))
        )
        self.assertTrue(
            provider_supports_capability("deepseek", role_capability(ROLE_PDF_REVIEW))
        )
        self.assertFalse(
            provider_supports_capability("deepseek", role_capability(ROLE_IMAGE))
        )

    def test_supports_the_two_buttons_next_to_the_provider_picker(self) -> None:
        """获取模型列表 and 测试连接 must work, not answer "不支持该服务商".

        Registering a provider for translation only is worse than not adding
        it: the form offers it, prefills its Base URL, and then the two
        buttons beside that field refuse it.  DeepSeek serves /v1/models and
        /v1/chat/completions, so both are just a matter of listing it.
        """
        self.assertIn("deepseek", OPENAI_COMPATIBLE_PROVIDERS)
        self.assertIn("deepseek", OPENAI_COMPATIBLE_MODEL_PROVIDERS)

    def test_reads_its_key_from_the_conventional_environment_variable(self) -> None:
        self.assertEqual(_api_key_env_names("deepseek"), ("DEEPSEEK_API_KEY",))

    def test_dispatches_to_the_openai_compatible_engine(self) -> None:
        settings = AppSettings()
        settings.engine.mode = "cloud"
        settings.engine.source_role = "independent"
        settings.engine.cloud_provider = "deepseek"
        settings.engine.cloud_model = "deepseek-chat"
        settings.engine.cloud_base_url = DEEPSEEK_BASE_URL

        engine = build_engine(settings)

        self.assertIsInstance(engine, OpenAIEngine)


VISION_MODELS = {"deepseek-flash"}


class _FakeDeepSeek(BaseHTTPRequestHandler):
    """只有 /chat/completions 的 DeepSeek：deepseek-flash 认图，deepseek-v4-pro 不认。"""

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

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        self.calls.append({"path": self.path, "json": payload})
        if self.path != "/v1/chat/completions":
            self._send_json(404, {"error": {"message": "Not Found"}})
            return
        content = payload["messages"][-1]["content"]
        has_image = isinstance(content, list) and any(part.get("type") == "image_url" for part in content)
        if has_image and payload.get("model") not in VISION_MODELS:
            self._send_json(400, {"error": {"message": "image input is not supported by this model", "type": "invalid_request_error"}})
            return
        # 认图的模型读出测试图里的码；带的是页面图时照常回审核结果。
        answer = probe_code_in_request(payload) or json.dumps(
            {"pass": True, "blocking_issues": [], "minor_suggestions": [], "summary": "ok"}
        )
        self._send_json(200, {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": answer}}]})


class DeepSeekReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeDeepSeek)
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}/v1"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        _FakeDeepSeek.calls = []

    def _settings(self, model: str) -> AppSettings:
        settings = AppSettings()
        role = settings.pdf_review_model_role
        role.source_role = SOURCE_INDEPENDENT
        role.cloud_provider = "deepseek"
        role.cloud_model = model
        role.cloud_base_url = self.base_url
        return settings

    def test_review_test_passes_with_the_vision_model_over_chat(self) -> None:
        with patch("core.model_roles.get_key", return_value="sk-test"):
            result = check_pdf_review_connectivity(self._settings("deepseek-flash"), max_attempts=1)
        self.assertTrue(result.ok, result.message)
        # 直接走 chat，不先去试 /v1/responses。
        self.assertEqual([call["path"] for call in _FakeDeepSeek.calls], ["/v1/chat/completions"])
        content = _FakeDeepSeek.calls[0]["json"]["messages"][-1]["content"]
        # 一张写着随机码的测试图，模型读对了才算通过。
        self.assertEqual([part["type"] for part in content], ["text", "image_url"])

    def test_text_only_model_is_told_to_pick_one_that_reads_images(self) -> None:
        with patch("core.model_roles.get_key", return_value="sk-test"):
            result = check_pdf_review_connectivity(self._settings("deepseek-v4-pro"), max_attempts=1)
        self.assertFalse(result.ok)
        self.assertEqual(result.message, f"图像翻译审核模型连接测试失败：{REVIEW_MODEL_ADVICE}")
        self.assertEqual(result.status, "no_image_input")

    def test_a_gateway_that_drops_the_image_no_longer_passes(self) -> None:
        """以前发两张白图，接口不报错就算通过；中转站把图丢了也「通过」。现在要读对测试图里的字。"""
        settings = self._settings("deepseek-flash")
        with patch("core.model_roles.get_key", return_value="sk-test"), patch(
            "tests.test_deepseek_provider.probe_code_in_request", return_value=""
        ):
            result = check_pdf_review_connectivity(settings, max_attempts=1)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "image_unseen")
        self.assertIn("没有读出测试图中的文字", result.message)
        self.assertEqual([check.result for check in settings.image_input_checks.values()], ["unseen"])


class ProviderDefaultsRouteTests(unittest.TestCase):
    """The UI reads its prefill table from here instead of keeping a copy.

    Two hardcoded copies of a Base URL drift the first time a provider moves
    its endpoint, and the copy that goes stale is the one the user can see.
    """

    def setUp(self) -> None:
        self.client = TestClient(create_app())

    def test_serves_every_preset_including_deepseek(self) -> None:
        payload = self.client.get("/api/models/provider-defaults").json()

        self.assertEqual(payload["base_url_defaults"], CLOUD_PROVIDER_BASE_URL_DEFAULTS)
        self.assertEqual(payload["base_url_defaults"]["deepseek"], DEEPSEEK_BASE_URL)

    def test_reports_providers_that_take_no_base_url(self) -> None:
        payload = self.client.get("/api/models/provider-defaults").json()

        self.assertEqual(payload["base_url_disabled"], ["dashscope", "zhipu"])
        self.assertTrue(payload["disabled_placeholder"])

    def test_prefills_no_model_name_and_links_every_named_provider_key_page(self) -> None:
        """不预填型号（各家换代太快，写死的推荐很快过时）；每家给一个创建密钥的控制台链接。"""
        payload = self.client.get("/api/models/provider-defaults").json()

        self.assertNotIn("model_defaults", payload)
        self.assertEqual(payload["api_key_pages"], CLOUD_PROVIDER_API_KEY_PAGES)
        # 自定义配置的端点由用户自填，没有统一的控制台。
        self.assertNotIn("custom_openai", payload["api_key_pages"])
        named = set(CLOUD_ENGINES.values()) - {"custom_openai"}
        self.assertEqual(named - set(payload["api_key_pages"]), set())
        self.assertTrue(all(url.startswith("https://") for url in payload["api_key_pages"].values()))
        # 硅基流动国内站、国际站账号不通用：国际站地址对应国际站控制台。
        self.assertIn("siliconflow.cn", payload["api_key_pages"]["siliconflow"])
        self.assertIn("siliconflow.com", payload["api_key_pages_by_host"]["api.siliconflow.com"])

if __name__ == "__main__":
    unittest.main()
