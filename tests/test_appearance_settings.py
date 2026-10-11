from __future__ import annotations

import json
import unittest

from settings import AppSettings
import settings as settings_module
from tests.app_data_isolation import IsolatedAppDataTestCase


class AppearanceSettingsTests(unittest.TestCase):
    def test_default_theme_is_light(self) -> None:
        self.assertEqual(AppSettings().appearance.theme, "light")
        self.assertEqual(AppSettings().appearance.ui_language, "zh")

    def test_invalid_theme_falls_back_to_light(self) -> None:
        settings = AppSettings.model_validate({"appearance": {"theme": "violet"}})

        self.assertEqual(settings.appearance.theme, "light")

    def test_invalid_ui_language_falls_back_to_chinese(self) -> None:
        settings = AppSettings.model_validate({"appearance": {"ui_language": "de"}})

        self.assertEqual(settings.appearance.ui_language, "zh")

    def test_supported_ui_languages_are_kept(self) -> None:
        for value in ("zh", "en", "fr"):
            settings = AppSettings.model_validate({"appearance": {"ui_language": value}})
            self.assertEqual(settings.appearance.ui_language, value)


class ThemeDefaultMigrationTests(IsolatedAppDataTestCase):
    """v29 files stored the old default "system"; v30 reads that as "light"."""

    def _write_settings(self, payload: dict) -> None:
        self.app_data_dir.mkdir(parents=True, exist_ok=True)
        (self.app_data_dir / "settings.json").write_text(json.dumps(payload), encoding="utf-8")

    def _read_settings(self) -> dict:
        return json.loads((self.app_data_dir / "settings.json").read_text(encoding="utf-8"))

    def test_old_system_theme_becomes_light(self) -> None:
        self._write_settings({"settings_version": 29, "appearance": {"theme": "system"}})

        loaded = settings_module.load_settings()

        self.assertEqual(loaded.appearance.theme, "light")
        self.assertEqual(loaded.appearance.ui_language, "zh")

    def test_old_explicit_dark_is_kept(self) -> None:
        self._write_settings({"settings_version": 29, "appearance": {"theme": "dark"}})

        self.assertEqual(settings_module.load_settings().appearance.theme, "dark")

    def test_migrated_theme_is_persisted_on_next_save(self) -> None:
        self._write_settings({"settings_version": 29, "appearance": {"theme": "system"}})

        loaded = settings_module.load_settings()
        loaded.appearance.ui_language = "fr"
        settings_module.save_settings(loaded)

        persisted = self._read_settings()
        self.assertEqual(persisted["appearance"]["theme"], "light")
        self.assertEqual(persisted["appearance"]["ui_language"], "fr")
        self.assertEqual(persisted["settings_version"], settings_module.SETTINGS_SCHEMA_VERSION)

    def test_picking_system_after_upgrade_is_not_migrated_again(self) -> None:
        self._write_settings({"settings_version": 29, "appearance": {"theme": "system"}})

        loaded = settings_module.load_settings()
        loaded.appearance.theme = "system"
        settings_module.save_settings(loaded)

        self.assertEqual(self._read_settings()["appearance"]["theme"], "system")
        self.assertEqual(settings_module.load_settings().appearance.theme, "system")

    def test_current_version_system_is_respected(self) -> None:
        self._write_settings(
            {"settings_version": settings_module.SETTINGS_SCHEMA_VERSION, "appearance": {"theme": "system"}}
        )

        self.assertEqual(settings_module.load_settings().appearance.theme, "system")


if __name__ == "__main__":
    unittest.main(verbosity=2)
