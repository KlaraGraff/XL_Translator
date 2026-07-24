from __future__ import annotations

import unittest
from unittest.mock import patch

from core.model_api_identity import task_api_context_for_page
from core.model_throughput import set_model_throughput
from core.model_roles import ROLE_TRANSLATION, resolve_effective_model_config
from settings import AppSettings, api_key_scope, set_cloud_provider_config


def _fake_key(provider: str, base_url: str = "") -> str:
    return f"key::{provider}::{base_url}"


class ModelApiIdentityTests(unittest.TestCase):
    def test_task_context_contains_frozen_role_and_throughput_snapshot_without_secret(self) -> None:
        settings = AppSettings()
        settings.engine.mode = "cloud"
        settings.engine.cloud_provider = "custom_openai"
        set_cloud_provider_config(
            settings.engine,
            "custom_openai",
            cloud_model="snapshot-model",
            cloud_base_url="https://snapshot.example/v1",
        )
        with patch("core.model_roles.get_key", return_value="snapshot-secret"):
            config = resolve_effective_model_config(settings, ROLE_TRANSLATION)
            set_model_throughput(settings, config, batch_size=11, concurrency=3)
            context = task_api_context_for_page(settings, "excel_translate")

        snapshot = context.model_snapshot[ROLE_TRANSLATION]
        self.assertEqual(snapshot["model"], "snapshot-model")
        self.assertEqual(snapshot["throughput"]["batch_size"], 11)
        self.assertEqual(snapshot["throughput"]["concurrency"], 3)
        self.assertEqual(
            snapshot["api_scope"],
            api_key_scope("custom_openai", "https://snapshot.example/v1"),
        )
        self.assertNotIn("snapshot-secret", repr(snapshot))

    def test_text_task_context_captures_translation_api_and_key(self) -> None:
        settings = AppSettings()
        settings.engine.mode = "cloud"
        settings.engine.cloud_provider = "custom_openai"
        set_cloud_provider_config(
            settings.engine,
            "custom_openai",
            cloud_model="text-model",
            cloud_base_url="https://text-api.example/v1",
        )

        with patch("core.model_roles.get_key", side_effect=_fake_key):
            context = task_api_context_for_page(settings, "excel_translate")

        self.assertEqual(len(context.api_groups), 1)
        group = next(iter(context.api_groups))
        self.assertEqual(group[:3], ("cloud", "custom_openai", "https://text-api.example/v1"))
        self.assertTrue(group[3])
        self.assertEqual(
            context.key_overrides,
            {
                api_key_scope(
                    "custom_openai",
                    "https://text-api.example/v1",
                ): "key::custom_openai::https://text-api.example/v1"
            },
        )

    def test_pdf_context_includes_review_api_when_enabled(self) -> None:
        settings = AppSettings()
        settings.pdf.review_enabled = True
        settings.image_model_role.source_role = "independent"
        settings.image_model_role.cloud_provider = "custom_openai"
        set_cloud_provider_config(
            settings.image_model_role,
            "custom_openai",
            cloud_model="image-model",
            cloud_base_url="https://image-api.example/v1",
        )
        settings.pdf_review_model_role.source_role = "independent"
        settings.pdf_review_model_role.cloud_provider = "custom_openai"
        set_cloud_provider_config(
            settings.pdf_review_model_role,
            "custom_openai",
            cloud_model="review-model",
            cloud_base_url="https://review-api.example/v1",
        )

        with patch("core.model_roles.get_key", side_effect=_fake_key):
            context = task_api_context_for_page(settings, "pdf_translate")

        self.assertEqual(
            {group[:3] for group in context.api_groups},
            {
                ("cloud", "custom_openai", "https://image-api.example/v1"),
                ("cloud", "custom_openai", "https://review-api.example/v1"),
            },
        )
        self.assertEqual(
            set(context.key_overrides),
            {
                api_key_scope("custom_openai", "https://image-api.example/v1"),
                api_key_scope("custom_openai", "https://review-api.example/v1"),
            },
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
