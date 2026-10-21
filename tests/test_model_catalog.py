from __future__ import annotations

import unittest
from unittest.mock import patch

from core.model_catalog import (
    build_model_catalog_signature,
    clear_model_catalog_cache,
    fetch_openai_compatible_models,
)


class _FakeResponse:
    def __init__(self, payload=None, *, status_code: int = 200, text: str = "") -> None:
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.text = text

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}: {self.text}")

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.get_calls: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def get(self, url: str, *, headers=None, params=None):
        self.get_calls.append({"url": url, "headers": headers, "params": params})
        return self.response


class ModelCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_model_catalog_cache()

    def _patch_client(self, fake_client: _FakeClient):
        return patch(
            "core.model_catalog.httpx.Client",
            autospec=True,
            return_value=fake_client,
        )

    def test_fetch_openai_compatible_models_reads_data_ids(self) -> None:
        fake_client = _FakeClient(
            _FakeResponse(
                {
                    "data": [
                        {"id": "gpt-5.4"},
                        {"id": "gpt-5.4"},
                        {"id": "gpt-5.4-mini"},
                    ]
                }
            )
        )

        with self._patch_client(fake_client):
            result = fetch_openai_compatible_models(
                provider="custom_openai",
                api_key="secret-token",
                base_url="https://api.example.test/v1",
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.models, ["gpt-5.4", "gpt-5.4-mini"])
        self.assertEqual(fake_client.get_calls[0]["url"], "https://api.example.test/v1/models")
        self.assertEqual(fake_client.get_calls[0]["headers"]["Authorization"], "Bearer secret-token")

    def test_fetch_models_supports_models_array_shape(self) -> None:
        fake_client = _FakeClient(
            _FakeResponse({"models": [{"name": "qwen-max"}, {"model": "deepseek-v3"}]})
        )

        with self._patch_client(fake_client):
            result = fetch_openai_compatible_models(
                provider="custom_openai",
                api_key="secret",
                base_url="https://api.example.test/v1/",
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.models, ["qwen-max", "deepseek-v3"])

    def test_fetch_models_requires_api_key_and_base_url(self) -> None:
        no_key = fetch_openai_compatible_models(
            provider="custom_openai",
            api_key="",
            base_url="https://api.example.test/v1",
        )
        no_base_url = fetch_openai_compatible_models(
            provider="custom_openai",
            api_key="secret",
            base_url="",
        )

        self.assertFalse(no_key.ok)
        self.assertEqual(no_key.status, "missing_api_key")
        self.assertFalse(no_base_url.ok)
        self.assertEqual(no_base_url.status, "missing_base_url")

    def test_fetch_models_uses_official_openai_url_for_openai_provider(self) -> None:
        fake_client = _FakeClient(_FakeResponse({"data": [{"id": "gpt-5.4"}]}))

        with self._patch_client(fake_client):
            result = fetch_openai_compatible_models(
                provider="openai",
                api_key="secret",
                base_url="",
            )

        self.assertTrue(result.ok)
        self.assertEqual(fake_client.get_calls[0]["url"], "https://api.openai.com/v1/models")

    def test_fetch_models_respects_openai_custom_base_url(self) -> None:
        fake_client = _FakeClient(_FakeResponse({"data": [{"id": "gpt-5.4"}]}))

        with self._patch_client(fake_client):
            result = fetch_openai_compatible_models(
                provider="openai",
                api_key="secret",
                base_url="https://proxy.example.test/v1",
            )

        self.assertTrue(result.ok)
        self.assertEqual(fake_client.get_calls[0]["url"], "https://proxy.example.test/v1/models")

    def test_fetch_models_appends_v1_for_bare_compatible_host(self) -> None:
        fake_client = _FakeClient(_FakeResponse({"data": [{"id": "gpt-5.4"}]}))

        with self._patch_client(fake_client):
            result = fetch_openai_compatible_models(
                provider="custom_openai",
                api_key="secret",
                base_url="https://api.example.test",
            )

        self.assertTrue(result.ok)
        self.assertEqual(fake_client.get_calls[0]["url"], "https://api.example.test/v1/models")

    def test_fetch_ollama_models_uses_api_tags(self) -> None:
        fake_client = _FakeClient(
            _FakeResponse({"models": [{"name": "qwen2.5:14b"}, {"model": "llama3.1:8b"}]})
        )

        with self._patch_client(fake_client):
            result = fetch_openai_compatible_models(
                provider="ollama",
                api_key="",
                base_url="http://localhost:11434",
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.models, ["qwen2.5:14b", "llama3.1:8b"])
        self.assertEqual(fake_client.get_calls[0]["url"], "http://localhost:11434/api/tags")
        self.assertEqual(fake_client.get_calls[0]["headers"], None)

    def test_fetch_lm_studio_models_does_not_require_api_key(self) -> None:
        fake_client = _FakeClient(_FakeResponse({"data": [{"id": "qwen-local"}]}))

        with self._patch_client(fake_client):
            result = fetch_openai_compatible_models(
                provider="lm_studio",
                api_key="",
                base_url="",
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.models, ["qwen-local"])
        self.assertEqual(fake_client.get_calls[0]["url"], "http://localhost:1234/v1/models")
        self.assertEqual(fake_client.get_calls[0]["headers"], {})

    def test_error_message_does_not_echo_api_key(self) -> None:
        fake_client = _FakeClient(
            _FakeResponse({}, status_code=401, text="invalid secret-token")
        )

        with self._patch_client(fake_client):
            result = fetch_openai_compatible_models(
                provider="custom_openai",
                api_key="secret-token",
                base_url="https://api.example.test/v1",
            )

        self.assertFalse(result.ok)
        self.assertNotIn("secret-token", result.message)
        self.assertIn("***", result.message)

    def test_signature_hashes_key(self) -> None:
        signature = build_model_catalog_signature(
            provider="custom_openai",
            api_key="secret-token",
            base_url="https://api.example.test/v1",
        )

        self.assertIn("custom_openai", signature)
        self.assertIn("https://api.example.test/v1", signature)
        self.assertNotIn("secret-token", signature)
        self.assertRegex(signature.split("|")[-1], r"^[0-9a-f]{12}$")

    def test_successful_model_list_is_cached_per_connection(self) -> None:
        fake_client = _FakeClient(_FakeResponse({"data": [{"id": "cached-model"}]}))

        with self._patch_client(fake_client):
            first = fetch_openai_compatible_models(
                provider="custom_openai",
                api_key="secret",
                base_url="https://api.example.test/v1",
            )
            second = fetch_openai_compatible_models(
                provider="custom_openai",
                api_key="secret",
                base_url="https://api.example.test/v1",
            )

        self.assertTrue(first.ok)
        self.assertEqual(second.models, ["cached-model"])
        self.assertEqual(len(fake_client.get_calls), 1)

        # Results are defensive copies, so a caller cannot corrupt the cache.
        second.models.append("caller-only")
        with self._patch_client(fake_client):
            third = fetch_openai_compatible_models(
                provider="custom_openai",
                api_key="secret",
                base_url="https://api.example.test/v1",
            )
        self.assertEqual(third.models, ["cached-model"])

    def test_connection_change_bypasses_session_cache(self) -> None:
        fake_client = _FakeClient(_FakeResponse({"data": [{"id": "model-a"}]}))
        with self._patch_client(fake_client):
            first = fetch_openai_compatible_models(
                provider="custom_openai",
                api_key="secret-a",
                base_url="https://api.example.test/v1",
            )
        fake_client.response = _FakeResponse({"data": [{"id": "model-b"}]})
        with self._patch_client(fake_client):
            second = fetch_openai_compatible_models(
                provider="custom_openai",
                api_key="secret-b",
                base_url="https://api.example.test/v1",
            )

        self.assertEqual(first.models, ["model-a"])
        self.assertEqual(second.models, ["model-b"])
        self.assertEqual(len(fake_client.get_calls), 2)

    def test_openai_signature_includes_configured_base_url(self) -> None:
        first = build_model_catalog_signature(
            provider="openai",
            api_key="secret-token",
            base_url="",
        )
        second = build_model_catalog_signature(
            provider="openai",
            api_key="secret-token",
            base_url="https://api.example.test/v1",
        )

        self.assertNotEqual(first, second)

    def test_siliconflow_lists_only_models_that_fit_the_role(self) -> None:
        # 硅基流动的 /models 默认把文字、图片、语音模型混在一起；PDF 与图片翻译只列改图模型。
        # type=image 里还有只能文字生图的（2026-10-21 真实接口返回的 6 个），本地再筛掉。
        image_models = ["Tongyi-MAI/Z-Image-Turbo", "Tongyi-MAI/Z-Image", "Qwen/Qwen-Image-Edit-2509",
                        "Qwen/Qwen-Image-Edit", "Qwen/Qwen-Image", "Kwai-Kolors/Kolors"]
        fake_client = _FakeClient(_FakeResponse({"data": [{"id": model} for model in image_models]}))
        with self._patch_client(fake_client):
            image = fetch_openai_compatible_models(
                provider="siliconflow", api_key="k", base_url="", capability="image",
            )
            text = fetch_openai_compatible_models(
                provider="siliconflow", api_key="k", base_url="", capability="text",
            )
        self.assertTrue(image.ok and text.ok)
        self.assertEqual(image.models, ["Qwen/Qwen-Image-Edit-2509", "Qwen/Qwen-Image-Edit"])
        self.assertEqual(fake_client.get_calls[0]["url"], "https://api.siliconflow.cn/v1/models")
        # sub_type=image-to-image 在真实接口上返回 data: null。
        self.assertEqual(fake_client.get_calls[0]["params"], {"type": "image"})
        # 用途不同，缓存也分开：文字用途不能拿到图片模型的列表。
        self.assertEqual(fake_client.get_calls[1]["params"], {"sub_type": "chat"})

    def test_siliconflow_image_list_falls_back_when_no_name_says_edit(self) -> None:
        fake_client = _FakeClient(_FakeResponse({"data": [{"id": "Vendor/New-Image-Model"}]}))
        with self._patch_client(fake_client):
            image = fetch_openai_compatible_models(
                provider="siliconflow", api_key="k", base_url="", capability="image",
            )
        self.assertEqual(image.models, ["Vendor/New-Image-Model"])

    def test_providers_without_base_url_and_claude_can_list_models(self) -> None:
        fake_client = _FakeClient(_FakeResponse({"data": [{"id": "m"}]}))
        with self._patch_client(fake_client):
            for provider in ("zhipu", "dashscope", "claude"):
                result = fetch_openai_compatible_models(provider=provider, api_key="k", base_url="")
                self.assertTrue(result.ok, provider)
        urls = [call["url"] for call in fake_client.get_calls]
        self.assertEqual(urls, [
            "https://open.bigmodel.cn/api/paas/v4/models",
            "https://dashscope.aliyuncs.com/compatible-mode/v1/models",
            "https://api.anthropic.com/v1/models",
        ])
        self.assertEqual(fake_client.get_calls[2]["headers"]["x-api-key"], "k")
        self.assertIn("anthropic-version", fake_client.get_calls[2]["headers"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
