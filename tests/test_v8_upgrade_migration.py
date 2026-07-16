from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import settings as settings_module
from config import SETTINGS_SCHEMA_VERSION
from core.data_migration import inspect_data_migration, migrate_legacy_data


class V8UpgradeMigrationTests(unittest.TestCase):
    def test_v74_data_directory_migrates_without_losing_settings_keys_or_tm(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = root / ".xl_translator"
            target = root / "Translator"
            legacy.mkdir()
            (legacy / "settings.json").write_text(
                json.dumps(
                    {
                        "settings_version": 24,
                        "source_lang": "zh",
                        "target_lang": "fr",
                        "engine": {
                            "cloud_provider": "custom_openai",
                            "cloud_model": "v74-model",
                            "cloud_base_url": "https://example.test/v1",
                        },
                        "pdf": {"target_lang": "en"},
                    }
                ),
                encoding="utf-8",
            )
            (legacy / "keys.json").write_text(
                '{"custom_openai::https://example.test/v1":"legacy-key"}',
                encoding="utf-8",
            )
            with closing(sqlite3.connect(legacy / "tm.db")) as connection:
                connection.execute("create table marker (value text)")
                connection.execute("insert into marker values ('v7.4 memory')")
                connection.commit()

            plan = inspect_data_migration(
                app_data_dir=target,
                legacy_data_dir=legacy,
                legacy_launcher_dir=legacy,
            )
            result = migrate_legacy_data(plan)
            self.assertEqual(len(result.migrated), 3)

            with patch.multiple(
                settings_module,
                APP_DATA_DIR=target,
                SETTINGS_PATH=target / "settings.json",
                KEYS_PATH=target / "keys.json",
                BACKUPS_DIR=target / "backups",
            ):
                settings = settings_module.load_settings()
                keys = settings_module.load_keys()

            self.assertEqual(settings.settings_version, SETTINGS_SCHEMA_VERSION)
            self.assertEqual(settings.target_lang, "fr")
            self.assertEqual(settings.pdf.target_lang, "en")
            self.assertEqual(settings.engine.cloud_model, "v74-model")
            self.assertEqual(settings.appearance.theme, "system")
            self.assertEqual(keys["custom_openai::https://example.test/v1"], "legacy-key")
            with closing(sqlite3.connect(target / "tm.db")) as connection:
                row = connection.execute("select value from marker").fetchone()
            self.assertEqual(row[0], "v7.4 memory")
            self.assertTrue(list((target / "backups" / "settings").glob("settings*.json")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
