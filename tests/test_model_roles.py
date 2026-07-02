from __future__ import annotations

import unittest
from unittest.mock import patch

from config import SETTINGS_SCHEMA_VERSION
from core.model_roles import (
    ROLE_CLEANER,
    ROLE_IMAGE,
    ROLE_PDF_REVIEW,
    ROLE_TRANSLATION,
    SOURCE_INDEPENDENT,
    ChainedModelFollowError,
    LocalModelFollowNotAllowedError,
    image_model_signature,
    provider_supports_capability,
    record_image_model_availability,
    record_pdf_review_model_availability,
    resolve_effective_model_config,
    settings_for_text_role,
)
from settings import AppSettings, EngineSettings, _migrate_settings_payload


class ModelRoleTests(unittest.TestCase):
    def test_settings_migration_adds_model_roles_pdf_defaults_and_local_defaults(self) -> None:
        migrated = _migrate_settings_payload(
            {
                "settings_version": 11,
                "cleaner_engine": "custom_openai",
                "cleaner_model": "",
                "engine": {
                    "mode": "cloud",
                    "cloud_provider": "custom_openai",
                    "cloud_model": "gpt-5.4",
                    "cloud_base_url": "https://example.test/v1",
                    "ollama_model": "qwen2.5:14b",
                },
            },
            11,
        )

        settings = AppSettings.model_validate(migrated)

        self.assertEqual(settings.settings_version, SETTINGS_SCHEMA_VERSION)
        self.assertEqual(settings.cleaner_model_role.source_role, ROLE_TRANSLATION)
        self.assertEqual(settings.image_model_role.source_role, ROLE_TRANSLATION)
        self.assertEqual(settings.pdf_review_model_role.source_role, ROLE_TRANSLATION)
        self.assertEqual(settings.pdf.page_retry_attempts, 3)
        self.assertEqual(settings.pdf.target_lang, "zh")
        self.assertIsNone(settings.pdf.page_generation_concurrency)
        self.assertFalse(settings.pdf.review_enabled)
        self.assertFalse(settings.update.ignore_updates)
        self.assertEqual(settings.update.ignored_release_version, "")
        self.assertEqual(settings.update.last_prompted_major_version, "")
        self.assertEqual(settings.engine.local_provider, "ollama")
        self.assertEqual(settings.engine.local_model, "qwen2.5:14b")

    def test_model_role_resolution_for_translation_and_following_cleaner(self) -> None:
        settings = AppSettings(
            engine=EngineSettings(
                mode="cloud",
                cloud_provider="custom_openai",
                cloud_model="gpt-main",
                cloud_base_url="https://api.example/v1",
            )
        )
        settings.cleaner_model_role.source_role = ROLE_TRANSLATION
        settings.cleaner_model_role.cloud_model = "cleaner-special"

        with patch("core.model_roles.get_key", return_value="secret"):
            translation = resolve_effective_model_config(settings, ROLE_TRANSLATION)
            cleaner = resolve_effective_model_config(settings, ROLE_CLEANER)

        self.assertEqual(translation.provider, "custom_openai")
        self.assertEqual(translation.capability, "text")
        self.assertTrue(cleaner.follows)
        self.assertEqual(cleaner.provider, "custom_openai")
        self.assertEqual(cleaner.base_url, "https://api.example/v1")
        self.assertEqual(cleaner.model, "cleaner-special")

    def test_settings_for_text_role_uses_effective_cleaner_config(self) -> None:
        settings = AppSettings()
        settings.cleaner_model_role.source_role = SOURCE_INDEPENDENT
        settings.cleaner_model_role.cloud_provider = "openai"
        settings.cleaner_model_role.cloud_model = "cleaner-openai"
        settings.cleaner_model_role.cloud_base_url = ""

        copy_settings = settings_for_text_role(settings, ROLE_CLEANER)

        self.assertEqual(copy_settings.engine.mode, "cloud")
        self.assertEqual(copy_settings.engine.cloud_provider, "openai")
        self.assertEqual(copy_settings.engine.cloud_model, "cleaner-openai")

    def test_translation_role_can_resolve_local_lm_studio_config(self) -> None:
        settings = AppSettings(
            engine=EngineSettings(
                mode="local",
                local_provider="lm_studio",
                local_model="qwen-local",
                local_base_url="http://localhost:1234/v1",
            )
        )

        config = resolve_effective_model_config(settings, ROLE_TRANSLATION)
        copy_settings = settings_for_text_role(settings, ROLE_TRANSLATION)

        self.assertEqual(config.mode, "local")
        self.assertEqual(config.provider, "lm_studio")
        self.assertEqual(config.model, "qwen-local")
        self.assertEqual(config.base_url, "http://localhost:1234/v1")
        self.assertEqual(copy_settings.engine.local_provider, "lm_studio")
        self.assertEqual(copy_settings.engine.local_model, "qwen-local")

    def test_chained_following_is_rejected(self) -> None:
        settings = AppSettings()
        settings.cleaner_model_role.source_role = ROLE_TRANSLATION
        settings.image_model_role.source_role = ROLE_CLEANER

        with self.assertRaises(ChainedModelFollowError):
            resolve_effective_model_config(settings, ROLE_IMAGE)

    def test_cloud_only_roles_cannot_follow_local_translation_model(self) -> None:
        settings = AppSettings(
            engine=EngineSettings(
                mode="local",
                local_provider="ollama",
                local_model="qwen2.5:14b",
            )
        )
        settings.cleaner_model_role.source_role = ROLE_TRANSLATION
        settings.image_model_role.source_role = ROLE_TRANSLATION
        settings.pdf_review_model_role.source_role = ROLE_TRANSLATION

        for role in (ROLE_CLEANER, ROLE_IMAGE, ROLE_PDF_REVIEW):
            with self.subTest(role=role):
                with self.assertRaises(LocalModelFollowNotAllowedError):
                    resolve_effective_model_config(settings, role)

    def test_image_follow_uses_access_config_but_not_text_model_name(self) -> None:
        settings = AppSettings(
            engine=EngineSettings(
                mode="cloud",
                cloud_provider="custom_openai",
                cloud_model="text-model",
                cloud_base_url="https://api.example/v1",
            )
        )
        settings.image_model_role.source_role = ROLE_TRANSLATION
        settings.image_model_role.cloud_model = ""

        with patch("core.model_roles.get_key", return_value="secret"):
            image_config = resolve_effective_model_config(settings, ROLE_IMAGE)

        self.assertTrue(image_config.follows)
        self.assertEqual(image_config.provider, "custom_openai")
        self.assertEqual(image_config.base_url, "https://api.example/v1")
        self.assertEqual(image_config.model, "")
        self.assertEqual(image_config.capability, "image")

    def test_image_generation_capability_and_availability_status(self) -> None:
        settings = AppSettings()
        settings.image_model_role.source_role = SOURCE_INDEPENDENT
        settings.image_model_role.cloud_provider = "custom_openai"
        settings.image_model_role.cloud_model = "image-model"
        settings.image_model_role.cloud_base_url = "https://images.example/v1"

        with patch("core.model_roles.get_key", return_value="secret"):
            config = resolve_effective_model_config(settings, ROLE_IMAGE)
            signature = image_model_signature(settings)

        self.assertEqual(config.capability, "image")
        self.assertTrue(provider_supports_capability(config.provider, "image"))
        self.assertFalse(provider_supports_capability("claude", "image"))

        record_image_model_availability(
            settings,
            ok=False,
            message="invalid api key",
            signature=signature,
            checked_at="2026-05-25T10:00:00",
        )

        self.assertEqual(settings.image_model_role.availability_status, "unavailable")
        self.assertEqual(settings.image_model_role.availability_signature, signature)
        self.assertIn("invalid", settings.image_model_role.availability_message)

    def test_pdf_review_model_uses_vision_text_capability_and_optional_empty_model(self) -> None:
        settings = AppSettings()
        settings.pdf_review_model_role.source_role = ROLE_TRANSLATION
        settings.pdf_review_model_role.cloud_model = ""

        with patch("core.model_roles.get_key", return_value="secret"):
            config = resolve_effective_model_config(settings, ROLE_PDF_REVIEW)

        self.assertEqual(config.capability, "vision_text")
        self.assertTrue(config.follows)
        self.assertEqual(config.model, "")
        self.assertTrue(provider_supports_capability("custom_openai", "vision_text"))
        self.assertFalse(provider_supports_capability("claude", "vision_text"))

        record_pdf_review_model_availability(
            settings,
            ok=True,
            message="review ok",
            signature="review-signature",
            checked_at="2026-05-25T10:00:00",
        )
        self.assertEqual(settings.pdf_review_model_role.availability_status, "available")
        self.assertEqual(settings.pdf_review_model_role.availability_signature, "review-signature")

    def test_pdf_review_model_can_follow_independent_image_model(self) -> None:
        settings = AppSettings()
        settings.image_model_role.source_role = SOURCE_INDEPENDENT
        settings.image_model_role.cloud_provider = "custom_openai"
        settings.image_model_role.cloud_model = "gpt-image-2"
        settings.image_model_role.cloud_base_url = "https://images.example/v1"
        settings.pdf_review_model_role.source_role = ROLE_IMAGE
        settings.pdf_review_model_role.cloud_model = "vision-review-model"

        with patch("core.model_roles.get_key", return_value="secret"):
            config = resolve_effective_model_config(settings, ROLE_PDF_REVIEW)

        self.assertTrue(config.follows)
        self.assertEqual(config.source_role, ROLE_IMAGE)
        self.assertEqual(config.provider, "custom_openai")
        self.assertEqual(config.base_url, "https://images.example/v1")
        self.assertEqual(config.model, "vision-review-model")

    def test_pdf_review_model_cannot_follow_image_model_that_already_follows(self) -> None:
        settings = AppSettings()
        settings.image_model_role.source_role = ROLE_TRANSLATION
        settings.pdf_review_model_role.source_role = ROLE_IMAGE

        with self.assertRaises(ChainedModelFollowError):
            resolve_effective_model_config(settings, ROLE_PDF_REVIEW)


if __name__ == "__main__":
    unittest.main(verbosity=2)
