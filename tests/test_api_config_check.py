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
        self.assertIn("API 密钥", result.message)

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


class PrimaryConnectionKeyTests(unittest.TestCase):
    """连接池里调换顺序或删掉首条连接后，新的首条连接自带 Key 就该通过预检。

    连接的 Key 存在 ``conn::<id>`` 名下，旧的按 provider + base_url 那一格只在设置
    首条连接时写过一次。预检以前只认旧格：首条换成另一条连接（不同 base_url）就报
    「尚未填写 API Key」，任务根本开不了。
    """

    def test_reordered_or_deleted_primary_connection_keeps_passing(self) -> None:
        import tempfile
        from pathlib import Path

        from fastapi.testclient import TestClient

        import settings as settings_module
        from api.app import create_app

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.object(settings_module, "APP_DATA_DIR", root),
                patch.object(settings_module, "KEYS_PATH", root / "keys.json"),
                patch.object(settings_module, "SETTINGS_PATH", root / "settings.json"),
            ):
                client = TestClient(create_app(auth_token="t"))
                client.headers.update({"X-Translator-Token": "t"})
                client.put(
                    "/api/models/roles/translation/connections/pool-translation",
                    json={
                        "provider": "custom_openai",
                        "model": "a",
                        "base_url": "https://a.example/v1",
                        "api_key": "ka",
                        "connection_mode": "cloud",
                    },
                )
                client.put(
                    "/api/keys/custom_openai",
                    json={"api_key": "ka", "base_url": "https://a.example/v1"},
                )
                created = client.post(
                    "/api/models/roles/translation/connections",
                    json={
                        "provider": "custom_openai",
                        "model": "b",
                        "base_url": "https://b.example/v1",
                        "api_key": "kb",
                    },
                )
                ids = [item["id"] for item in created.json()["connections"]]
                self.assertTrue(check_translation_api_config(settings_module.load_settings()).ok)

                client.post(
                    "/api/models/roles/translation/connections/reorder",
                    json={"ordered_ids": [ids[1], ids[0]]},
                )
                result = check_translation_api_config(settings_module.load_settings())
                self.assertTrue(result.ok, result.message)

                client.post(
                    "/api/models/roles/translation/connections/reorder",
                    json={"ordered_ids": ids},
                )
                client.delete(f"/api/models/roles/translation/connections/{ids[0]}")
                result = check_translation_api_config(settings_module.load_settings())
                self.assertTrue(result.ok, result.message)


if __name__ == "__main__":
    unittest.main(verbosity=2)
