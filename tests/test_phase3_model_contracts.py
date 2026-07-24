"""Focused M3A/M3B/M3C model, prompt, throughput, and exchange contracts.

The tests use in-memory ``AppSettings`` and mocked transport only.  The
expected-failure cases document behavior that is frozen by the migration
decision but is not yet represented by the current implementation.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from config import DOMAIN_PRESETS
from core.engine_dispatcher import get_system_prompt
from core.model_config import (
    MODEL_CONFIG_EXPORT_VERSION,
    apply_model_config_import,
    build_model_config_export_payload,
    parse_model_config_import,
)
from core.model_roles import (
    ROLE_CLEANER,
    ROLE_IMAGE,
    ROLE_PDF_REVIEW,
    ROLE_TRANSLATION,
    SOURCE_INDEPENDENT,
    ChainedModelFollowError,
    LocalModelFollowNotAllowedError,
    allowed_source_roles,
    model_config_signature,
    provider_supports_capability,
    resolve_effective_model_config,
    role_capability,
)
from core.model_throughput import (
    batch_size_bounds,
    concurrency_bounds,
    get_model_throughput,
    set_model_throughput,
)
from engines.openai_engine import OpenAIEngine
from settings import AppSettings, EngineSettings, set_cloud_provider_config


class Phase3ModelContractTests(unittest.TestCase):
    def test_four_roles_have_explicit_capabilities_and_allowed_sources(self) -> None:
        self.assertEqual(
            {
                ROLE_TRANSLATION: "text",
                ROLE_CLEANER: "text",
                ROLE_IMAGE: "image",
                ROLE_PDF_REVIEW: "vision_text",
            },
            {role: role_capability(role) for role in (
                ROLE_TRANSLATION,
                ROLE_CLEANER,
                ROLE_IMAGE,
                ROLE_PDF_REVIEW,
            )},
        )
        self.assertEqual(allowed_source_roles(ROLE_CLEANER), [SOURCE_INDEPENDENT, ROLE_TRANSLATION])
        self.assertEqual(
            allowed_source_roles(ROLE_IMAGE),
            [SOURCE_INDEPENDENT, ROLE_TRANSLATION, ROLE_CLEANER],
        )
        self.assertEqual(
            allowed_source_roles(ROLE_PDF_REVIEW),
            [SOURCE_INDEPENDENT, ROLE_TRANSLATION, ROLE_IMAGE],
        )

    def test_connection_reuse_shares_access_but_keeps_role_model_name(self) -> None:
        settings = AppSettings(
            engine=EngineSettings(
                mode="cloud",
                cloud_provider="custom_openai",
                cloud_model="translation-model",
                cloud_base_url="https://shared.example/v1",
            )
        )
        settings.cleaner_model_role.source_role = ROLE_TRANSLATION
        settings.cleaner_model_role.cloud_model = "cleaner-model"

        with patch("core.model_roles.get_key", return_value="shared-secret"):
            translation = resolve_effective_model_config(settings, ROLE_TRANSLATION)
            cleaner = resolve_effective_model_config(settings, ROLE_CLEANER)

        self.assertEqual(cleaner.provider, translation.provider)
        self.assertEqual(cleaner.base_url, translation.base_url)
        self.assertEqual(cleaner.api_key, translation.api_key)
        self.assertEqual(translation.model, "translation-model")
        self.assertEqual(cleaner.model, "cleaner-model")
        self.assertTrue(cleaner.follows)

    def test_cloud_only_roles_reject_local_translation_follow(self) -> None:
        settings = AppSettings(
            engine=EngineSettings(
                mode="local",
                local_provider="ollama",
                local_model="qwen2.5:14b",
            )
        )
        for role in (ROLE_CLEANER, ROLE_IMAGE, ROLE_PDF_REVIEW):
            settings_for_role = settings.model_copy(deep=True)
            getattr(settings_for_role, {
                ROLE_CLEANER: "cleaner_model_role",
                ROLE_IMAGE: "image_model_role",
                ROLE_PDF_REVIEW: "pdf_review_model_role",
            }[role]).source_role = ROLE_TRANSLATION
            with self.subTest(role=role), self.assertRaises(LocalModelFollowNotAllowedError):
                resolve_effective_model_config(settings_for_role, role)

    def test_chained_following_is_rejected_before_resolution(self) -> None:
        settings = AppSettings()
        settings.cleaner_model_role.source_role = ROLE_TRANSLATION
        settings.image_model_role.source_role = ROLE_CLEANER
        with self.assertRaises(ChainedModelFollowError):
            resolve_effective_model_config(settings, ROLE_IMAGE)

    def test_capability_registry_does_not_overclaim_provider_features(self) -> None:
        self.assertTrue(provider_supports_capability("custom_openai", "text"))
        self.assertTrue(provider_supports_capability("custom_openai", "image"))
        self.assertTrue(provider_supports_capability("custom_openai", "vision_text"))
        self.assertFalse(provider_supports_capability("claude", "image"))
        self.assertFalse(provider_supports_capability("claude", "vision_text"))

    def test_model_signature_changes_when_effective_config_changes(self) -> None:
        settings = AppSettings()
        settings.engine.mode = "cloud"
        set_cloud_provider_config(
            settings.engine,
            "custom_openai",
            cloud_model="model-a",
            cloud_base_url="https://api.example/v1",
        )
        with patch("core.model_roles.get_key", return_value="key-a"):
            config_a = resolve_effective_model_config(settings, ROLE_TRANSLATION)
            signature_a = model_config_signature(config_a)

        set_cloud_provider_config(
            settings.engine,
            "custom_openai",
            cloud_model="model-b",
            cloud_base_url="https://api.example/v1",
        )
        with patch("core.model_roles.get_key", return_value="key-a"):
            config_b = resolve_effective_model_config(settings, ROLE_TRANSLATION)
            signature_b = model_config_signature(config_b)
        self.assertNotEqual(signature_a, signature_b)
        self.assertNotIn("key-a", signature_a)
        self.assertNotIn("key-a", signature_b)

    def test_domain_presets_keep_the_three_named_builtins_and_custom_mode(self) -> None:
        self.assertEqual(
            set(DOMAIN_PRESETS),
            {"同步工程场景", "资料管理场景", "行政生活化场景", "自定义"},
        )
        settings = AppSettings(domain_preset="资料管理场景")
        prompt = get_system_prompt(settings, target_lang="en", source_lang="zh")
        self.assertIn("资料管理", prompt)

    def test_translation_engine_appends_fixed_output_protocol_after_domain_prompt(self) -> None:
        settings = AppSettings(domain_preset="自定义", custom_prompt="用户领域要求")
        domain_prompt = get_system_prompt(settings, target_lang="fr", source_lang="en")
        engine = OpenAIEngine(
            api_key="secret",
            model="model",
            base_url="https://api.example/v1",
        )
        with patch.object(engine, "_call_api", return_value='["ok"]') as call:
            self.assertEqual(
                engine.translate_batch(
                    ["hello"],
                    "fr",
                    domain_prompt,
                    source_lang="en",
                ),
                {"hello": "ok"},
            )
        full_system = call.call_args.args[0]
        self.assertIn("用户领域要求", full_system)
        self.assertIn("从英文翻译为法文", full_system)
        self.assertIn("JSON 数组", full_system)

    def test_excel_and_word_domain_state_is_independent(self) -> None:
        """M3B-02: each translation page needs its own domain state."""
        settings = AppSettings()
        self.assertTrue(hasattr(settings, "excel_domain_preset"))
        self.assertTrue(hasattr(settings, "word_domain_preset"))

    @unittest.expectedFailure
    def test_cleaning_full_override_cannot_remove_json_protocol(self) -> None:
        """M3B-07: full cleaner overrides must retain protocol boundaries."""
        from core.tm_cleaner import build_clean_system_prompt

        prompt = build_clean_system_prompt(
            lang_pair="fr-en",
            full_override_prompt="只输出随意文本，不要 JSON",
        )
        self.assertIn("严格输出 JSON 数组", prompt)
        self.assertIn("id", prompt)

    def test_text_roles_have_batch_size_and_pdf_roles_do_not(self) -> None:
        settings = AppSettings()
        settings.engine.mode = "cloud"
        set_cloud_provider_config(
            settings.engine,
            "custom_openai",
            cloud_model="translation-model",
            cloud_base_url="https://api.example/v1",
        )
        translation = resolve_effective_model_config(settings, ROLE_TRANSLATION)
        image = resolve_effective_model_config(settings, ROLE_IMAGE)
        review = resolve_effective_model_config(settings, ROLE_PDF_REVIEW)

        self.assertIsNotNone(batch_size_bounds(translation))
        self.assertIsNone(batch_size_bounds(image))
        self.assertIsNone(batch_size_bounds(review))
        self.assertEqual(concurrency_bounds(image)[0], 1)
        self.assertEqual(concurrency_bounds(review)[0], 1)

        image_tuning = set_model_throughput(settings, image, batch_size=99, concurrency=2)
        self.assertIsNone(image_tuning.batch_size)
        self.assertEqual(image_tuning.concurrency, 2)

    def test_throughput_profiles_are_separate_for_role_and_model(self) -> None:
        settings = AppSettings()
        settings.engine.mode = "cloud"
        set_cloud_provider_config(
            settings.engine,
            "custom_openai",
            cloud_model="translation-model",
            cloud_base_url="https://api.example/v1",
        )
        settings.cleaner_model_role.source_role = SOURCE_INDEPENDENT
        set_cloud_provider_config(
            settings.cleaner_model_role,
            "custom_openai",
            cloud_model="cleaner-model",
            cloud_base_url="https://api.example/v1",
        )
        translation = resolve_effective_model_config(settings, ROLE_TRANSLATION)
        cleaner = resolve_effective_model_config(settings, ROLE_CLEANER)
        translation_profile = set_model_throughput(
            settings, translation, batch_size=10, concurrency=2
        )
        cleaner_profile = set_model_throughput(
            settings, cleaner, batch_size=11, concurrency=3
        )
        self.assertNotEqual(translation_profile.profile_key, cleaner_profile.profile_key)
        self.assertEqual(get_model_throughput(settings, translation).batch_size, 10)
        self.assertEqual(get_model_throughput(settings, translation).concurrency, 2)
        self.assertEqual(get_model_throughput(settings, cleaner).batch_size, 11)
        self.assertEqual(get_model_throughput(settings, cleaner).concurrency, 3)

        set_cloud_provider_config(
            settings.engine,
            "custom_openai",
            cloud_model="translation-model-v2",
            cloud_base_url="https://api.example/v1",
        )
        changed = resolve_effective_model_config(settings, ROLE_TRANSLATION)
        self.assertNotEqual(
            get_model_throughput(settings, changed).profile_key,
            translation_profile.profile_key,
        )

    def test_model_config_export_contains_only_the_four_role_profiles(self) -> None:
        payload = build_model_config_export_payload(AppSettings(), get_api_key=lambda *_: "")
        self.assertEqual(payload["type"], "translator_model_config")
        self.assertEqual(
            set(payload["model_profiles"]),
            {"translation", "cleaner", "pdf_translation", "pdf_review"},
        )
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("custom_prompt", serialized)
        self.assertNotIn("tm_entries", serialized)
        self.assertNotIn("task_history", serialized)

    def test_model_config_export_is_v3(self) -> None:
        self.assertEqual(MODEL_CONFIG_EXPORT_VERSION, 3)

    def test_model_config_export_excludes_api_keys_by_default(self) -> None:
        settings = AppSettings()
        payload = build_model_config_export_payload(
            settings,
            get_api_key=lambda *_: "secret-token",
        )
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("secret-token", serialized)
        for profile in payload["model_profiles"].values():
            self.assertNotIn("api_key", json.dumps(profile, ensure_ascii=False))

    def test_model_config_import_round_trip_preserves_roles_and_scoped_key(self) -> None:
        raw = {
            "type": "translator_model_config",
            "version": 3,
            "model_profiles": {
                "translation": {
                    "mode": "cloud",
                    "cloud": {
                        "provider": "custom_openai",
                        "model": "imported-translation",
                        "base_url": "https://import.example/v1",
                        "api_key": "import-secret",
                    },
                    "throughput": {"batch_size": 6, "concurrency": 2},
                },
                "cleaner": {
                    "source_role": "translation",
                    "cloud": {
                        "provider": "custom_openai",
                        "model": "imported-cleaner",
                        "base_url": "https://import.example/v1",
                    },
                },
            },
        }
        imported = parse_model_config_import(raw)
        settings = AppSettings()
        with patch("core.model_config.save_key") as save_key:
            updated = apply_model_config_import(settings, imported, save_api_key=save_key)

        self.assertEqual(updated.engine.cloud_model, "imported-translation")
        self.assertEqual(updated.cleaner_model_role.source_role, ROLE_TRANSLATION)
        self.assertEqual(updated.cleaner_model_role.cloud_model, "imported-cleaner")
        self.assertTrue(save_key.called)
        self.assertIn("import-secret", repr(save_key.call_args))


if __name__ == "__main__":
    unittest.main(verbosity=2)
