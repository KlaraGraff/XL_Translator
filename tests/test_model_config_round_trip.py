"""Exporting then importing a model configuration must not lose a role's state.

Translation can follow another role and the cleaner can run against a local
runner.  Both live in fields the exporter writes for all four roles, so the
reader has to pick them up for all four or a round-trip quietly resets them.
"""

from __future__ import annotations

import unittest

from core.model_config import (
    apply_model_config_import,
    build_model_config_export_payload,
    parse_model_config_import,
)
from settings import AppSettings


def _round_trip(settings: AppSettings) -> AppSettings:
    payload = build_model_config_export_payload(settings, get_api_key=lambda *_: "")
    imported = parse_model_config_import(payload)
    return apply_model_config_import(
        AppSettings(),
        imported,
        save_api_key=lambda *_args, **_kwargs: None,
    )


class ModelConfigRoundTripTests(unittest.TestCase):
    def test_translation_following_another_role_survives(self) -> None:
        settings = AppSettings()
        # Cleaner has to become independent first: following a follower is the
        # one shape the graph still rejects.
        settings.cleaner_model_role.source_role = "independent"
        settings.image_model_role.source_role = "independent"
        settings.pdf_review_model_role.source_role = "independent"
        settings.engine.source_role = "cleaner"

        restored = _round_trip(settings)
        assert restored.engine.source_role == "cleaner"

    def test_a_local_cleaner_survives(self) -> None:
        settings = AppSettings()
        settings.cleaner_model_role.source_role = "independent"
        settings.cleaner_model_role.mode = "local"
        settings.cleaner_model_role.local_provider = "lm_studio"
        settings.cleaner_model_role.local_model = "qwen2.5:7b"
        settings.cleaner_model_role.local_base_url = "http://127.0.0.1:1234/v1"

        restored = _round_trip(settings)
        cleaner = restored.cleaner_model_role
        assert cleaner.mode == "local"
        assert cleaner.local_provider == "lm_studio"
        assert cleaner.local_model == "qwen2.5:7b"
        assert cleaner.local_base_url == "http://127.0.0.1:1234/v1"

    def test_a_local_translation_still_survives(self) -> None:
        """The pre-existing translation-only behaviour must not regress."""
        settings = AppSettings()
        # The image and review roles follow translation by default and are
        # cloud-only, so they have to leave first for a local translation to be
        # a legal graph at all.  That rule predates this change.
        settings.image_model_role.source_role = "independent"
        settings.pdf_review_model_role.source_role = "independent"
        settings.engine.mode = "local"
        settings.engine.local_provider = "ollama"
        settings.engine.local_model = "qwen2.5:7b"

        restored = _round_trip(settings)
        assert restored.engine.mode == "local"
        assert restored.engine.local_model == "qwen2.5:7b"
        # The pre-rename field tracks local_model for the engine only.
        assert restored.engine.ollama_model == "qwen2.5:7b"

    def test_cloud_roles_stay_cloud(self) -> None:
        restored = _round_trip(AppSettings())
        assert restored.engine.mode == "cloud"
        assert restored.cleaner_model_role.mode == "cloud"
        assert restored.image_model_role.mode == "cloud"
        assert restored.pdf_review_model_role.mode == "cloud"

    def test_a_sparse_file_does_not_reset_an_unmentioned_mode(self) -> None:
        """A profile that names no mode must leave a configured local role alone."""
        settings = AppSettings()
        settings.cleaner_model_role.source_role = "independent"
        settings.cleaner_model_role.mode = "local"
        settings.cleaner_model_role.local_model = "qwen2.5:7b"

        payload = build_model_config_export_payload(
            settings, get_api_key=lambda *_: ""
        )
        del payload["model_profiles"]["cleaner"]["mode"]
        imported = parse_model_config_import(payload)
        restored = apply_model_config_import(
            settings,
            imported,
            save_api_key=lambda *_args, **_kwargs: None,
        )
        assert restored.cleaner_model_role.mode == "local"


if __name__ == "__main__":
    unittest.main()
