"""Regression coverage for the one-time model upgrade migration."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from settings import AppSettings, ModelRoleSettings, save_settings


class ModelUpgradeMigrationTests(unittest.TestCase):
    def test_old_payload_migrates_all_roles_fields_and_does_not_mutate_source(self):
        payload = {
            "settings_version": 26,
            "engine": {
                "cloud_model": "deepseek-chat",
                "cloud_provider_configs": {"deepseek": {"cloud_model": "deepseek-reasoner"}},
                "connections": [{"provider": "deepseek", "model": "deepseek-coder"}],
            },
            "cleaner_model_role": {"cloud_model": "deepseek-v3"},
            "image_model_role": {
                "cloud_model": "gpt-image-2",
                "connections": [{"model": "gpt-image-2"}],
            },
            "pdf_review_model_role": {"cloud_model": "deepseek-r1"},
            "cleaner_model": "deepseek-chat",
        }
        original = copy.deepcopy(payload)
        settings = AppSettings.model_validate(payload)
        self.assertEqual(payload, original)
        self.assertEqual(settings.engine.cloud_model, "deepseek-flash")
        self.assertEqual(settings.engine.connections[0].model, "deepseek-flash")
        self.assertEqual(
            settings.engine.cloud_provider_configs["deepseek"].cloud_model,
            "deepseek-flash",
        )
        self.assertEqual(settings.cleaner_model_role.cloud_model, "deepseek-flash")
        self.assertEqual(settings.image_model_role.cloud_model, "gpt-image-2.5-flare")
        self.assertEqual(settings.image_model_role.connections[0].model, "gpt-image-2.5-flare")
        self.assertEqual(settings.pdf_review_model_role.cloud_model, "deepseek-flash")
        self.assertEqual(settings.cleaner_model, "deepseek-flash")

    def test_missing_roles_keep_defaults_and_base_model_input_is_accepted(self):
        settings = AppSettings.model_validate(ModelRoleSettings(cloud_model="deepseek-chat"))
        self.assertEqual(settings.image_model_role.cloud_model, "")
        self.assertEqual(settings.pdf_review_model_role.cloud_model, "")

    def test_current_payload_is_idempotent_and_preserves_explicit_new_choices(self):
        payload = {
            "settings_version": 27,
            "engine": {"cloud_model": "deepseek-chat"},
            "image_model_role": {"cloud_model": "gpt-image-2"},
        }
        settings = AppSettings.model_validate(payload)
        self.assertEqual(settings.engine.cloud_model, "deepseek-chat")
        self.assertEqual(settings.image_model_role.cloud_model, "gpt-image-2")
        self.assertEqual(AppSettings.model_validate(settings.model_dump()).image_model_role.cloud_model, "gpt-image-2")

    def test_first_save_of_old_file_backs_up_source_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings_path = root / "settings.json"
            backups = root / "backups"
            old = {"settings_version": 26, "image_model_role": {"cloud_model": "gpt-image-2"}}
            settings_path.write_text(json.dumps(old), encoding="utf-8")
            with patch("settings.SETTINGS_PATH", settings_path), patch("settings.APP_DATA_DIR", root), patch("settings.BACKUPS_DIR", backups):
                loaded = __import__("settings").load_settings()
                loaded.image_model_role = ModelRoleSettings(cloud_model="gpt-image-2.5-flare")
                save_settings(loaded)
                backup_files = list((backups / "settings").glob("settings_unusable_*.json"))
                self.assertEqual(len(backup_files), 1)
                save_settings(__import__("settings").load_settings())
                self.assertEqual(len(list((backups / "settings").glob("settings_unusable_*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
