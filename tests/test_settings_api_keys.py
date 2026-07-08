from __future__ import annotations

import unittest
from unittest.mock import patch

from settings import api_key_scope, get_key, provider_key_overrides


class SettingsApiKeyTests(unittest.TestCase):
    def test_scoped_api_key_takes_precedence_over_provider_key(self) -> None:
        with patch(
            "settings.load_keys",
            return_value={
                "custom_openai": "legacy-secret",
                "custom_openai::https://api.example.test/v1": "scoped-secret",
            },
        ):
            self.assertEqual(
                get_key("custom_openai", "https://api.example.test/v1"),
                "scoped-secret",
            )
            self.assertEqual(
                get_key("custom_openai", "https://other.example.test/v1"),
                "legacy-secret",
            )

    def test_scoped_provider_overrides_are_thread_local(self) -> None:
        scoped = api_key_scope("custom_openai", "https://api.example.test/v1")
        with (
            patch("settings.load_keys", return_value={"custom_openai": "stored-secret"}),
            provider_key_overrides(
                {
                    "custom_openai": "task-legacy-secret",
                    scoped: "task-scoped-secret",
                }
            ),
        ):
            self.assertEqual(
                get_key("custom_openai", "https://api.example.test/v1"),
                "task-scoped-secret",
            )
            self.assertEqual(
                get_key("custom_openai", "https://other.example.test/v1"),
                "task-legacy-secret",
            )

        with patch("settings.load_keys", return_value={"custom_openai": "stored-secret"}):
            self.assertEqual(
                get_key("custom_openai", "https://api.example.test/v1"),
                "stored-secret",
            )

    def test_env_key_used_when_no_saved_key_exists(self) -> None:
        with (
            patch("settings.load_keys", return_value={}),
            patch.dict("os.environ", {"OPENAI_COMPATIBLE_API_KEY": "env-secret"}, clear=False),
        ):
            self.assertEqual(get_key("custom_openai", "https://api.example.test/v1"), "env-secret")

    def test_openai_key_does_not_fall_back_to_custom_openai(self) -> None:
        with (
            patch("settings.load_keys", return_value={}),
            patch.dict("os.environ", {"OPENAI_API_KEY": "openai-secret"}, clear=True),
        ):
            self.assertEqual(get_key("custom_openai", "https://api.example.test/v1"), "")
            self.assertEqual(get_key("openai", ""), "openai-secret")


if __name__ == "__main__":
    unittest.main(verbosity=2)
