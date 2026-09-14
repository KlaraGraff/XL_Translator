from __future__ import annotations

import unittest

from core.model_config import (
    apply_model_config_import,
    build_model_config_export_payload,
    parse_model_config_import,
)
from core.model_roles import ROLE_TRANSLATION, add_role_connection
from settings import AppSettings, CloudProviderConfig


class ProtocolModelConfigTests(unittest.TestCase):
    def test_export_import_preserves_protocol_and_configured_paths(self) -> None:
        settings = AppSettings()
        settings.engine.cloud_provider = "custom_openai"
        settings.engine.cloud_model = "primary"
        settings.engine.cloud_base_url = "https://gateway.example/custom"
        settings.engine.api_mode = "chat"
        settings = AppSettings.model_validate(settings.model_dump())
        settings.engine.cloud_provider_configs["custom_openai"] = CloudProviderConfig(
            api_mode="responses", cloud_model="primary",
            cloud_base_url="https://gateway.example/custom",
        )
        add_role_connection(
            settings,
            ROLE_TRANSLATION,
            label="备用",
            provider="custom_openai",
            model="backup",
            base_url="https://gateway.example/alt",
            api_mode="chat",
        )
        payload = build_model_config_export_payload(settings, get_api_key=lambda *_: "")
        profile = payload["model_profiles"]["translation"]

        self.assertEqual(profile["cloud"]["base_url"], "https://gateway.example/custom")
        self.assertEqual(profile["cloud"]["api_mode"], "chat")
        self.assertEqual(profile["effective"]["api_mode"], "responses")
        self.assertEqual(profile["connections"][0]["api_mode"], "chat")
        self.assertEqual(profile["connections"][1]["api_mode"], "chat")
        self.assertEqual(
            profile["cloud"]["provider_configs"]["custom_openai"]["api_mode"],
            "responses",
        )

        restored = apply_model_config_import(
            AppSettings(), parse_model_config_import(payload),
            save_api_key=lambda *_args, **_kwargs: None,
        )
        self.assertEqual(restored.engine.cloud_base_url, "https://gateway.example/custom")
        self.assertEqual(restored.engine.api_mode, "chat")
        self.assertEqual(restored.engine.connections[0].api_mode, "chat")

    def test_auto_protocol_round_trips_without_url_rewrite(self) -> None:
        settings = AppSettings()
        settings.engine.cloud_provider = "custom_openai"
        settings.engine.cloud_base_url = "https://root.example"
        settings.engine.api_mode = "auto"
        payload = build_model_config_export_payload(settings, get_api_key=lambda *_: "")
        self.assertEqual(
            payload["model_profiles"]["translation"]["cloud"]["base_url"],
            "https://root.example",
        )

    def test_sparse_old_payload_keeps_protocol_and_scoped_key_identity(self) -> None:
        settings = AppSettings()
        settings.engine.cloud_provider = "custom_openai"
        settings.engine.cloud_base_url = "https://root.example"
        settings.engine.api_mode = "responses"
        payload = {
            "type": "translator_model_config",
            "version": 2,
            "model_profiles": {"translation": {"cloud": {
                "provider": "custom_openai",
                "base_url": "https://root.example",
                "api_key": "secret",
            }}},
        }
        restored = apply_model_config_import(
            settings, parse_model_config_import(payload),
            save_api_key=lambda *_args, **_kwargs: None,
        )
        self.assertEqual(restored.engine.api_mode, "responses")
        imported = parse_model_config_import(payload)
        self.assertEqual(imported.scoped_api_keys[0]["base_url"], "https://root.example")


if __name__ == "__main__":
    unittest.main()
