"""统一图片请求模块：按接口写法判断看图、出图，不再按服务商名单挡人。"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

from core.image_requests import (
    build_vision_payload,
    image_base_url,
    image_generation_method,
    supports_image_generation,
    supports_vision,
    vision_routes,
)
from core.model_roles import provider_supports_capability, unsupported_capability_message


class VisionSupportTests(unittest.TestCase):
    def test_every_openai_compatible_provider_can_send_images(self) -> None:
        for provider in ("openai", "custom_openai", "siliconflow", "deepseek", "xiaomi", "zhipu", "dashscope"):
            with self.subTest(provider=provider):
                self.assertTrue(supports_vision(provider))
                self.assertTrue(provider_supports_capability(provider, "vision_text"))

    def test_a_new_provider_defaults_to_the_openai_format(self) -> None:
        self.assertTrue(supports_vision("some_new_provider"))

    def test_anthropic_format_is_not_wired_yet(self) -> None:
        self.assertFalse(supports_vision("claude"))

    def test_local_runners_never_send_images(self) -> None:
        for provider in ("ollama", "lm_studio", "custom_local", ""):
            self.assertFalse(supports_vision(provider))

    def test_zhipu_and_dashscope_use_chat_completions(self) -> None:
        for provider in ("zhipu", "dashscope", "deepseek", "xiaomi", "siliconflow"):
            self.assertEqual(vision_routes(provider, ""), ["chat"])
        self.assertEqual(vision_routes("openai", ""), ["responses", "chat"])
        self.assertEqual(vision_routes("custom_openai", "https://api.siliconflow.cn/v1"), ["chat"])
        self.assertEqual(vision_routes("zhipu", "", "responses"), ["responses"])

    def test_providers_without_a_base_url_field_use_their_fixed_endpoint(self) -> None:
        from config import DASHSCOPE_OPENAI_BASE_URL, ZHIPU_OPENAI_BASE_URL

        self.assertEqual(image_base_url("zhipu", ""), ZHIPU_OPENAI_BASE_URL.rstrip("/"))
        self.assertEqual(image_base_url("dashscope", ""), DASHSCOPE_OPENAI_BASE_URL.rstrip("/"))
        self.assertEqual(image_base_url("openai", ""), "https://api.openai.com/v1")

    def test_payload_carries_every_image_in_both_formats(self) -> None:
        with TemporaryDirectory() as tmp:
            images = []
            for name in ("a.png", "b.png"):
                path = Path(tmp) / name
                Image.new("RGB", (4, 4), "white").save(path)
                images.append(path)
            chat = build_vision_payload("chat", model="m", instructions="sys", user_text="hi", images=images)
            self.assertEqual(chat["messages"][0], {"role": "system", "content": "sys"})
            parts = chat["messages"][1]["content"]
            self.assertEqual(parts[0], {"type": "text", "text": "hi"})
            self.assertEqual([p["type"] for p in parts[1:]], ["image_url", "image_url"])
            self.assertTrue(parts[1]["image_url"]["url"].startswith("data:image/png;base64,"))
            responses = build_vision_payload("responses", model="m", instructions="sys", user_text="hi", images=images)
            self.assertEqual(responses["instructions"], "sys")
            self.assertEqual(
                [p["type"] for p in responses["input"][0]["content"]],
                ["input_text", "input_image", "input_image"],
            )


class ImageGenerationSupportTests(unittest.TestCase):
    def test_only_confirmed_methods_are_wired(self) -> None:
        self.assertEqual(image_generation_method("openai"), "openai")
        self.assertEqual(image_generation_method("custom_openai"), "openai")
        self.assertEqual(image_generation_method("siliconflow"), "siliconflow")
        self.assertEqual(image_generation_method("custom_openai", "https://api.siliconflow.cn/v1"), "siliconflow")
        for provider in ("claude", "zhipu", "dashscope", "deepseek", "xiaomi"):
            with self.subTest(provider=provider):
                self.assertFalse(supports_image_generation(provider))
                self.assertFalse(provider_supports_capability(provider, "image"))

    def test_unwired_message_blames_the_app_and_points_to_custom(self) -> None:
        message = unsupported_capability_message("图像翻译模型", "zhipu", "image")
        self.assertEqual(
            message,
            "智谱 GLM的生成图片功能尚未接入本软件。"
            "如需使用，请在设置中为图像翻译模型改用「自定义配置」，填写兼容 OpenAI 接口的地址。",
        )


if __name__ == "__main__":
    unittest.main()
