from __future__ import annotations

import unittest
from unittest.mock import patch

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
from settings import AppSettings, EngineSettings


class ModelRoleTests(unittest.TestCase):
    def test_current_baseline_has_model_roles_pdf_defaults_and_local_defaults(self) -> None:
        settings = AppSettings(
            engine=EngineSettings(
                mode="cloud",
                cloud_provider="custom_openai",
                cloud_model="gpt-5.4",
                cloud_base_url="https://example.test/v1",
                local_provider="ollama",
                local_model="qwen2.5:14b",
            )
        )

        self.assertEqual(settings.cleaner_model_role.source_role, ROLE_TRANSLATION)
        self.assertEqual(settings.image_model_role.source_role, ROLE_TRANSLATION)
        self.assertEqual(settings.pdf_review_model_role.source_role, ROLE_TRANSLATION)
        self.assertEqual(settings.pdf.page_retry_attempts, 3)
        self.assertEqual(settings.pdf.target_lang, "zh")
        self.assertIsNone(settings.pdf.page_generation_concurrency)
        self.assertFalse(settings.pdf.review_enabled)
        self.assertFalse(settings.update.notifications_paused)
        self.assertEqual(settings.update.ignored_release_version, "")
        self.assertEqual(settings.update.last_background_check_at, "")
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
        settings.pdf_review_model_role.source_role = ROLE_CLEANER

        with self.assertRaises(ChainedModelFollowError):
            resolve_effective_model_config(settings, ROLE_PDF_REVIEW)

    def test_cloud_only_roles_cannot_follow_local_translation_model(self) -> None:
        settings = AppSettings(
            engine=EngineSettings(
                mode="local",
                local_provider="ollama",
                local_model="qwen2.5:14b",
            )
        )
        settings.pdf_review_model_role.source_role = ROLE_TRANSLATION

        # Cleaner is excluded on purpose: it is a text role, so a local runner
        # satisfies its capability and following one is legal.  The image role
        # is excluded because it cannot follow anything at all.
        with self.assertRaises(LocalModelFollowNotAllowedError):
            resolve_effective_model_config(settings, ROLE_PDF_REVIEW)

    def test_image_role_reads_a_stored_follow_as_independent(self) -> None:
        """旧配置里 image 跟随翻译模型：读得出来，且降级成独立配置。"""
        settings = AppSettings(
            engine=EngineSettings(
                mode="cloud",
                cloud_provider="custom_openai",
                cloud_model="text-model",
                cloud_base_url="https://api.example/v1",
            )
        )
        settings.image_model_role.source_role = ROLE_TRANSLATION
        settings.image_model_role.cloud_model = "gpt-image-1"

        with patch("core.model_roles.get_key", return_value="secret"):
            image_config = resolve_effective_model_config(settings, ROLE_IMAGE)

        self.assertFalse(image_config.follows)
        self.assertEqual(image_config.source_role, SOURCE_INDEPENDENT)
        self.assertEqual(settings.image_model_role.source_role, SOURCE_INDEPENDENT)
        self.assertEqual(image_config.capability, "image")
        # 模型名是本角色自己的，跟随从来不共用它，降级后必须还在。
        self.assertEqual(image_config.model, "gpt-image-1")

    def test_degraded_image_role_keeps_the_endpoint_it_was_dialing(self) -> None:
        """降级不能把用户留在一份他从没配过的端点上。"""
        settings = AppSettings(
            engine=EngineSettings(
                mode="cloud",
                cloud_provider="custom_openai",
                cloud_model="text-model",
                cloud_base_url="https://new.example/v1",
            )
        )
        settings.image_model_role.source_role = ROLE_TRANSLATION
        settings.image_model_role.cloud_model = "gpt-image-1"
        # 跟随期间没人维护本角色自己的端点，它停在几个版本前的值上。
        settings.image_model_role.cloud_base_url = "https://stale.example/v1"
        settings.image_model_role.cloud_provider_configs = {}

        with patch("core.model_roles.get_key", return_value="secret"):
            image_config = resolve_effective_model_config(settings, ROLE_IMAGE)

        self.assertEqual(image_config.base_url, "https://new.example/v1")
        self.assertEqual(image_config.provider, "custom_openai")
        self.assertEqual(image_config.model, "gpt-image-1")
        self.assertEqual(
            settings.image_model_role.connections[0].base_url,
            "https://new.example/v1",
        )
        # 端点换了，旧的测试结论不再描述它。
        self.assertEqual(image_config.availability_status, "unknown")

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

    def test_a_stored_follow_of_the_image_role_reads_as_independent(self) -> None:
        """旧配置里 pdf_review 跟随 image：读得出来，且降级成独立配置。"""
        settings = AppSettings()
        settings.image_model_role.source_role = SOURCE_INDEPENDENT
        settings.image_model_role.cloud_provider = "custom_openai"
        settings.image_model_role.cloud_model = "gpt-image-2"
        settings.image_model_role.cloud_base_url = "https://images.example/v1"
        settings.pdf_review_model_role.source_role = ROLE_IMAGE
        settings.pdf_review_model_role.cloud_model = "vision-review-model"

        with patch("core.model_roles.get_key", return_value="secret"):
            config = resolve_effective_model_config(settings, ROLE_PDF_REVIEW)

        self.assertFalse(config.follows)
        self.assertEqual(config.source_role, SOURCE_INDEPENDENT)
        self.assertEqual(config.model, "vision-review-model")
        # 跟随时用的就是图像角色那个端点，降级后固化到自己名下。
        self.assertEqual(config.base_url, "https://images.example/v1")

    def test_both_illegal_follow_directions_read_without_raising(self) -> None:
        """两个方向的非法跟随同时存在，也不能让配置读不出来。"""
        settings = AppSettings()
        settings.image_model_role.source_role = ROLE_TRANSLATION
        settings.pdf_review_model_role.source_role = ROLE_IMAGE

        with patch("core.model_roles.get_key", return_value="secret"):
            image_config = resolve_effective_model_config(settings, ROLE_IMAGE)
            review_config = resolve_effective_model_config(settings, ROLE_PDF_REVIEW)

        self.assertEqual(image_config.source_role, SOURCE_INDEPENDENT)
        self.assertEqual(review_config.source_role, SOURCE_INDEPENDENT)
        self.assertFalse(image_config.follows)
        self.assertFalse(review_config.follows)


if __name__ == "__main__":
    unittest.main(verbosity=2)
