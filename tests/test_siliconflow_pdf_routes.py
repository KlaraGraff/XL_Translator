"""PDF 整页图像翻译与逐页审核在只有 chat 接口的服务商（硅基流动）上的请求路线。

本地起一个假的硅基流动：只有 /models、/chat/completions、/images/generations，
没有 OpenAI 的 /responses 和 /images/edits（以前这两处写死只发 OpenAI 格式，
选硅基流动时不管哪个模型都失败）。
"""

from __future__ import annotations

import base64
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from core.image_generation import (
    IMAGE_MODEL_ADVICE,
    OpenAICompatibleImageGenerationClient,
    check_image_generation_connectivity,
)
from core.model_roles import ROLE_IMAGE, ROLE_PDF_REVIEW, SOURCE_INDEPENDENT, EffectiveModelConfig
from core.pdf_review import (
    REVIEW_MODEL_ADVICE,
    OpenAICompatiblePdfReviewClient,
    check_pdf_review_connectivity,
    review_routes,
)
from settings import AppSettings

IMAGE_MODELS = {"Qwen/Qwen-Image-Edit-2509"}
VISION_MODELS = {"Qwen/Qwen3-VL-32B-Instruct"}


def _png_bytes(size=(64, 64)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, "white").save(buffer, format="PNG")
    return buffer.getvalue()


class _FakeSiliconFlow(BaseHTTPRequestHandler):
    calls: list[dict] = []
    has_responses = False

    def log_message(self, *_args) -> None:
        return None

    def _send_json(self, status: int, payload) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self.calls.append({"method": "GET", "path": self.path})
        if self.path.startswith("/files/"):
            body = _png_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._send_json(404, {"message": "404 page not found"})

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        self.calls.append({"method": "POST", "path": self.path, "json": payload})
        if self.headers.get("Authorization") != "Bearer sk-test":
            self._send_json(401, {"message": "Invalid token"})
            return
        model = payload.get("model")
        if self.path == "/v1/images/generations":
            if model not in IMAGE_MODELS:
                # 真实硅基流动（2026-10-21 实测）：文字模型调这个接口回 403 而不是 400。
                self._send_json(403, {"code": 30000, "message": "Illegal operation.", "data": None})
                return
            if not str(payload.get("image", "")).startswith("data:image/png;base64,"):
                self._send_json(400, {"message": "image is required"})
                return
            port = self.server.server_address[1]
            self._send_json(200, {"images": [{"url": f"http://127.0.0.1:{port}/files/out.png"}], "seed": 1})
            return
        if self.path == "/v1/chat/completions":
            content = payload["messages"][-1]["content"]
            has_image = isinstance(content, list) and any(part.get("type") == "image_url" for part in content)
            if has_image and model not in VISION_MODELS:
                # 真实硅基流动（2026-10-21 实测）的原话。
                self._send_json(400, {"code": 20041, "message": "The model is not a VLM (Vision Language Model). Please use text-only prompts.", "data": None})
                return
            answer = json.dumps({"pass": True, "blocking_issues": [], "minor_suggestions": [], "summary": "ok"})
            self._send_json(200, {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": answer}}]})
            return
        if self.path == "/v1/responses" and self.has_responses:
            answer = json.dumps({"pass": True, "blocking_issues": [], "summary": "ok"})
            self._send_json(200, {"status": "completed", "output_text": answer})
            return
        self._send_json(404, {"message": "404 page not found"})


class SiliconFlowPdfRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeSiliconFlow)
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}/v1"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        _FakeSiliconFlow.calls = []
        _FakeSiliconFlow.has_responses = False
        self.tmp = tempfile.TemporaryDirectory()
        self.source = Path(self.tmp.name) / "page.png"
        self.source.write_bytes(_png_bytes((120, 160)))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _config(self, role: str, model: str, provider: str = "siliconflow", api_mode: str = "auto"):
        return EffectiveModelConfig(
            role=role,
            label="PDF 与图片翻译模型" if role == ROLE_IMAGE else "图像翻译审核模型",
            capability="image" if role == ROLE_IMAGE else "vision_text",
            mode="cloud",
            provider=provider,
            model=model,
            base_url=self.base_url,
            api_key="sk-test",
            api_mode=api_mode,
        )

    def _paths(self) -> list[str]:
        return [call["path"] for call in _FakeSiliconFlow.calls]

    def test_image_page_uses_siliconflow_image_api_and_downloads_the_result(self) -> None:
        image_bytes = OpenAICompatibleImageGenerationClient(timeout_seconds=10).generate_page(
            source_image_path=self.source,
            target_language="English",
            target_lang_code="en",
            model_config=self._config(ROLE_IMAGE, "Qwen/Qwen-Image-Edit-2509"),
        )
        Image.open(BytesIO(image_bytes)).verify()
        self.assertEqual(self._paths(), ["/v1/images/generations", "/files/out.png"])
        sent = _FakeSiliconFlow.calls[0]["json"]
        self.assertEqual(base64.b64decode(sent["image"].split(",", 1)[1]), self.source.read_bytes())
        self.assertIn("English", sent["prompt"])
        self.assertNotIn("image_size", sent)

    def _image_settings(self, model: str) -> AppSettings:
        settings = AppSettings()
        role = settings.image_model_role
        role.source_role = SOURCE_INDEPENDENT
        role.cloud_provider = "siliconflow"
        role.cloud_model = model
        role.cloud_base_url = self.base_url
        return settings

    def test_image_connectivity_passes_with_an_image_edit_model(self) -> None:
        settings = self._image_settings("Qwen/Qwen-Image-Edit-2509")
        with patch("core.model_roles.get_key", return_value="sk-test"):
            result = check_image_generation_connectivity(settings, max_attempts=1)
        self.assertTrue(result.ok, result.message)
        self.assertNotIn("/v1/responses", self._paths())

    def test_text_model_in_image_role_gets_the_image_model_advice(self) -> None:
        settings = self._image_settings("Qwen/Qwen2.5-7B-Instruct")
        with patch("core.model_roles.get_key", return_value="sk-test"):
            result = check_image_generation_connectivity(settings, max_attempts=1)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "wrong_model")
        self.assertTrue(result.message.endswith(IMAGE_MODEL_ADVICE), result.message)
        self.assertIn("image-2.5-flare", result.message)
        self.assertEqual(settings.image_model_role.availability_message, result.message)

    def test_wrong_key_is_not_blamed_on_the_model(self) -> None:
        settings = self._image_settings("Qwen/Qwen-Image-Edit-2509")
        with patch("core.model_roles.get_key", return_value="sk-wrong"):
            result = check_image_generation_connectivity(settings, max_attempts=1)
        self.assertFalse(result.ok)
        self.assertNotIn(IMAGE_MODEL_ADVICE, result.message)
        self.assertIn("密钥", result.message)

    def test_review_on_siliconflow_goes_straight_to_chat_with_both_images(self) -> None:
        result = OpenAICompatiblePdfReviewClient(timeout_seconds=10).review_page(
            source_image_path=self.source,
            translated_image_path=self.source,
            target_language="English",
            model_config=self._config(ROLE_PDF_REVIEW, "Qwen/Qwen3-VL-32B-Instruct"),
        )
        self.assertTrue(result.passed)
        self.assertEqual(self._paths(), ["/v1/chat/completions"])
        content = _FakeSiliconFlow.calls[0]["json"]["messages"][-1]["content"]
        self.assertEqual([part["type"] for part in content], ["text", "image_url", "image_url"])

    def test_review_auto_mode_falls_back_to_chat_when_responses_is_missing(self) -> None:
        # 「自定义配置」接到一个没有 /responses 的网关：自动模式先试 /responses，再换 chat。
        result = OpenAICompatiblePdfReviewClient(timeout_seconds=10).review_page(
            source_image_path=self.source,
            translated_image_path=self.source,
            target_language="English",
            model_config=self._config(ROLE_PDF_REVIEW, "Qwen/Qwen3-VL-32B-Instruct", provider="custom_openai"),
        )
        self.assertTrue(result.passed)
        self.assertEqual(self._paths(), ["/v1/responses", "/v1/chat/completions"])

    def test_review_follows_the_connection_protocol_setting(self) -> None:
        _FakeSiliconFlow.has_responses = True
        config = self._config(ROLE_PDF_REVIEW, "gpt-x", provider="custom_openai", api_mode="responses")
        self.assertEqual(review_routes(config), ["responses"])
        OpenAICompatiblePdfReviewClient(timeout_seconds=10).review_page(
            source_image_path=self.source,
            translated_image_path=self.source,
            target_language="English",
            model_config=config,
        )
        self.assertEqual(self._paths(), ["/v1/responses"])
        chat = self._config(ROLE_PDF_REVIEW, "m", provider="openai", api_mode="chat")
        self.assertEqual(review_routes(chat), ["chat"])
        self.assertEqual(review_routes(self._config(ROLE_PDF_REVIEW, "m", provider="openai")), ["responses", "chat"])

    def test_review_connectivity_with_a_text_only_model_says_to_pick_a_vision_model(self) -> None:
        settings = AppSettings()
        role = settings.pdf_review_model_role
        role.source_role = SOURCE_INDEPENDENT
        role.cloud_provider = "siliconflow"
        role.cloud_model = "Qwen/Qwen2.5-7B-Instruct"
        role.cloud_base_url = self.base_url
        with patch("core.model_roles.get_key", return_value="sk-test"):
            result = check_pdf_review_connectivity(settings, max_attempts=1)
        self.assertFalse(result.ok)
        self.assertEqual(result.message, f"图像翻译审核模型连接测试失败：{REVIEW_MODEL_ADVICE}")

    def test_route_panel_treats_a_failed_image_model_as_missing(self) -> None:
        # 自动组合：图像模型测过、没通过，就和没设置一样，扫描页与保底页保留原样。
        from api.routes.pdf_routes import route_capabilities

        settings = self._image_settings("Qwen/Qwen2.5-7B-Instruct")
        with patch("core.model_roles.get_key", return_value="sk-test"):
            check_image_generation_connectivity(settings, max_attempts=1)
            with patch("api.routes.pdf_routes.load_settings", return_value=settings):
                caps = route_capabilities()
        self.assertFalse(caps["image_model_ready"])
        self.assertIn("测试未通过", caps["image_model_message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
