from __future__ import annotations

import unittest
from unittest.mock import patch

from core.api_config_check import check_translation_api_config
from settings import AppSettings, EngineSettings, get_key


class ApiConfigCheckTests(unittest.TestCase):
    def test_cloud_provider_requires_api_key_without_network_request(self) -> None:
        settings = AppSettings(
            engine=EngineSettings(
                mode="cloud",
                cloud_provider="custom_openai",
                cloud_model="gpt-5.4",
                cloud_base_url="https://api.example.test/v1",
            )
        )

        with patch("core.api_config_check.get_key", return_value=""):
            result = check_translation_api_config(settings)

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "missing_api_key")
        self.assertIn("API Key", result.message)

    def test_cloud_provider_with_required_fields_passes_config_check(self) -> None:
        settings = AppSettings(
            engine=EngineSettings(
                mode="cloud",
                cloud_provider="custom_openai",
                cloud_model="gpt-5.4",
                cloud_base_url="https://api.example.test/v1",
            )
        )

        with patch("core.api_config_check.get_key", return_value="secret"):
            result = check_translation_api_config(settings)

        self.assertTrue(result.ok)

    def test_local_mode_does_not_require_api_key(self) -> None:
        settings = AppSettings(
            engine=EngineSettings(
                mode="local",
                local_provider="ollama",
                local_model="qwen2.5:14b",
            )
        )

        with patch("core.api_config_check.get_key", return_value=""):
            result = check_translation_api_config(settings)

        self.assertTrue(result.ok)

    def test_local_lm_studio_uses_default_base_url_when_blank(self) -> None:
        settings = AppSettings(
            engine=EngineSettings(
                mode="local",
                local_provider="lm_studio",
                local_model="qwen-local",
                local_base_url="",
            )
        )

        result = check_translation_api_config(settings)

        self.assertTrue(result.ok)
        self.assertEqual(settings.engine.local_base_url, "http://localhost:1234/v1")

    def test_local_custom_requires_base_url(self) -> None:
        settings = AppSettings(
            engine=EngineSettings(
                mode="local",
                local_provider="custom_local",
                local_model="local-model",
                local_base_url="",
            )
        )

        result = check_translation_api_config(settings)

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "missing_local_base_url")

    def test_custom_openai_can_read_legacy_lanyi_key(self) -> None:
        with patch("settings.load_keys", return_value={"lanyi": "legacy-secret"}):
            self.assertEqual(get_key("custom_openai"), "legacy-secret")


if __name__ == "__main__":
    unittest.main(verbosity=2)
