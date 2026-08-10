"""DeepSeek is a first-class cloud provider, and its preset reaches the form.

The panel used to leave Base URL blank when a provider was picked: the default
only got applied server-side at save time, so a preset that existed in
``config.py`` was invisible until after the user had already typed one in.  The
provider-defaults route is what lets the form prefill before the save, so it is
covered here alongside the provider registration itself.
"""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from api.app import create_app
from config import (
    CLOUD_ENGINES,
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
    ROLE_TRANSLATION,
    provider_supports_capability,
    role_capability,
)
from engines.openai_engine import OpenAIEngine
from settings import AppSettings, _api_key_env_names


class DeepSeekProviderTests(unittest.TestCase):
    def test_listed_as_a_cloud_provider(self) -> None:
        self.assertEqual(CLOUD_ENGINES.get("DeepSeek"), "deepseek")

    def test_default_base_url_is_the_official_endpoint(self) -> None:
        self.assertEqual(CLOUD_PROVIDER_BASE_URL_DEFAULTS["deepseek"], DEEPSEEK_BASE_URL)
        self.assertEqual(cloud_provider_base_url_default("deepseek"), DEEPSEEK_BASE_URL)

    def test_blank_base_url_falls_back_to_the_preset(self) -> None:
        self.assertEqual(normalize_cloud_base_url("deepseek", ""), DEEPSEEK_BASE_URL)

    def test_satisfies_text_capability_but_not_image(self) -> None:
        self.assertTrue(
            provider_supports_capability("deepseek", role_capability(ROLE_TRANSLATION))
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


if __name__ == "__main__":
    unittest.main()
