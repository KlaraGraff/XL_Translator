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

    def test_default_palettes_are_liuguang(self) -> None:
        appearance = AppSettings().appearance
        self.assertEqual(appearance.palette_light, "liuguang")
        self.assertEqual(appearance.palette_dark, "liuguang")
        self.assertEqual(settings_module.PALETTES[0], "liuguang")

    def test_invalid_palette_falls_back_to_default(self) -> None:
        settings = AppSettings.model_validate({"appearance": {"palette_light": "neon", "palette_dark": "石墨"}})

        self.assertEqual(settings.appearance.palette_light, "liuguang")
        self.assertEqual(settings.appearance.palette_dark, "liuguang")

    def test_light_and_dark_palettes_are_independent(self) -> None:
        for light in settings_module.PALETTES:
            for dark in settings_module.PALETTES:
                appearance = AppSettings.model_validate(
                    {"appearance": {"palette_light": light, "palette_dark": dark}}
                ).appearance
                self.assertEqual((appearance.palette_light, appearance.palette_dark), (light, dark))


class PaletteUpgradeTests(IsolatedAppDataTestCase):
    """v31 added appearance.palette_light / palette_dark; v30 files get the default."""

    def _write_settings(self, payload: dict) -> None:
        self.app_data_dir.mkdir(parents=True, exist_ok=True)
        (self.app_data_dir / "settings.json").write_text(json.dumps(payload), encoding="utf-8")

    def _read_settings(self) -> dict:
        return json.loads((self.app_data_dir / "settings.json").read_text(encoding="utf-8"))

    def test_v30_file_gets_default_palettes_and_keeps_its_choices(self) -> None:
        self._write_settings(
            {"settings_version": 30, "appearance": {"theme": "system", "ui_language": "en"}}
        )

        loaded = settings_module.load_settings()

        self.assertEqual(loaded.appearance.palette_light, "liuguang")
        self.assertEqual(loaded.appearance.palette_dark, "liuguang")
        # A v30 "system" was picked by the user (v30 already defaulted to light).
        self.assertEqual(loaded.appearance.theme, "system")
        self.assertEqual(loaded.appearance.ui_language, "en")

    def test_v30_file_is_stamped_on_next_save(self) -> None:
        self._write_settings({"settings_version": 30, "appearance": {"theme": "dark"}})

        loaded = settings_module.load_settings()
        loaded.appearance.palette_light = "celadon"
        loaded.appearance.palette_dark = "amber"
        settings_module.save_settings(loaded)

        persisted = self._read_settings()
        self.assertEqual(persisted["settings_version"], settings_module.SETTINGS_SCHEMA_VERSION)
        self.assertEqual(persisted["appearance"]["palette_light"], "celadon")
        self.assertEqual(persisted["appearance"]["palette_dark"], "amber")
        self.assertEqual(persisted["appearance"]["theme"], "dark")
        reloaded = settings_module.load_settings().appearance
        self.assertEqual((reloaded.palette_light, reloaded.palette_dark), ("celadon", "amber"))


class PaletteDefaultMigrationTests(IsolatedAppDataTestCase):
    """v40 files stored the old default "graphite"; v41 reads that as 「流光」 (liuguang)."""

    def _write_settings(self, payload: dict) -> None:
        self.app_data_dir.mkdir(parents=True, exist_ok=True)
        (self.app_data_dir / "settings.json").write_text(json.dumps(payload), encoding="utf-8")

    def _read_settings(self) -> dict:
        return json.loads((self.app_data_dir / "settings.json").read_text(encoding="utf-8"))

    def test_old_graphite_becomes_liuguang(self) -> None:
        self._write_settings(
            {"settings_version": 40, "appearance": {"palette_light": "graphite", "palette_dark": "graphite"}}
        )

        appearance = settings_module.load_settings().appearance

        self.assertEqual((appearance.palette_light, appearance.palette_dark), ("liuguang", "liuguang"))

    def test_old_deliberate_choice_is_kept(self) -> None:
        self._write_settings(
            {"settings_version": 40, "appearance": {"palette_light": "graphite", "palette_dark": "amber"}}
        )

        appearance = settings_module.load_settings().appearance

        self.assertEqual((appearance.palette_light, appearance.palette_dark), ("liuguang", "amber"))

    def test_old_file_without_palettes_gets_liuguang(self) -> None:
        self._write_settings({"settings_version": 40, "appearance": {"theme": "dark"}})

        appearance = settings_module.load_settings().appearance

        self.assertEqual((appearance.palette_light, appearance.palette_dark), ("liuguang", "liuguang"))
        self.assertEqual(appearance.theme, "dark")

    def test_current_graphite_is_a_deliberate_pick(self) -> None:
        # Once the file is v41, graphite can only have been chosen in the settings page.
        self._write_settings(
            {
                "settings_version": settings_module.SETTINGS_SCHEMA_VERSION,
                "appearance": {"palette_light": "graphite", "palette_dark": "graphite"},
            }
        )

        appearance = settings_module.load_settings().appearance

        self.assertEqual((appearance.palette_light, appearance.palette_dark), ("graphite", "graphite"))

    def test_migrated_palette_is_persisted_with_v41(self) -> None:
        self._write_settings(
            {"settings_version": 40, "appearance": {"palette_light": "graphite", "palette_dark": "celadon"}}
        )

        loaded = settings_module.load_settings()
        settings_module.save_settings(loaded)

        persisted = self._read_settings()
        self.assertEqual(persisted["settings_version"], 41)
        self.assertEqual(persisted["appearance"]["palette_light"], "liuguang")
        self.assertEqual(persisted["appearance"]["palette_dark"], "celadon")
        reloaded = settings_module.load_settings().appearance
        self.assertEqual((reloaded.palette_light, reloaded.palette_dark), ("liuguang", "celadon"))


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
