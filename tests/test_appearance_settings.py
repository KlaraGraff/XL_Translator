from __future__ import annotations

import unittest

from config import SETTINGS_SCHEMA_VERSION
from settings import AppSettings, _migrate_settings_payload


class AppearanceSettingsTests(unittest.TestCase):
    def test_v24_payload_migrates_to_tauri_appearance_defaults(self) -> None:
        migrated = _migrate_settings_payload({"settings_version": 24}, 24)

        self.assertEqual(migrated["settings_version"], SETTINGS_SCHEMA_VERSION)
        self.assertEqual(
            migrated["appearance"],
            {"theme": "system", "model_config_panel_open": False},
        )

    def test_invalid_theme_falls_back_to_system(self) -> None:
        settings = AppSettings.model_validate({"appearance": {"theme": "violet"}})

        self.assertEqual(settings.appearance.theme, "system")


if __name__ == "__main__":
    unittest.main(verbosity=2)
