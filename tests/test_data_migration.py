from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from core.data_migration import (
    inspect_data_migration,
    mark_migration_skipped,
    migrate_legacy_data,
    migrate_non_conflicting_legacy_data,
)


def _write_sqlite_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(str(path))) as conn:
        with conn:
            conn.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
            conn.execute("INSERT INTO sample (value) VALUES ('legacy')")


def _read_sqlite_value(path: Path) -> str:
    with closing(sqlite3.connect(str(path))) as conn:
        row = conn.execute("SELECT value FROM sample WHERE id = 1").fetchone()
    return str(row[0])


class DataMigrationTests(unittest.TestCase):
    def test_migrates_primary_files_and_sqlite_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / ".xl_translator"
            target = root / "data" / "Translator"
            legacy.mkdir(parents=True)
            (legacy / "settings.json").write_text('{"target_lang":"fr"}', encoding="utf-8")
            (legacy / "keys.json").write_text('{"custom_openai":"secret"}', encoding="utf-8")
            _write_sqlite_db(legacy / "tm.db")

            plan = inspect_data_migration(
                app_data_dir=target,
                legacy_data_dir=legacy,
                legacy_launcher_dir=legacy,
            )
            self.assertEqual(plan.status, "ready")

            progress: list[tuple[int, int, str]] = []
            result = migrate_legacy_data(
                plan,
                progress=lambda current, total, message: progress.append(
                    (current, total, message)
                ),
            )

            self.assertEqual(_read_sqlite_value(target / "tm.db"), "legacy")
            self.assertEqual(
                json.loads((target / "settings.json").read_text(encoding="utf-8")),
                {"target_lang": "fr"},
            )
            self.assertTrue((target / "keys.json").exists())
            marker = json.loads((target / "migration.json").read_text(encoding="utf-8"))
            self.assertEqual(marker["status"], "migrated")
            self.assertEqual(len(result.migrated), 3)
            self.assertTrue(progress)
            self.assertEqual(progress[-1][2], "迁移完成")

    def test_support_files_are_optional_and_preserved_under_legacy_support(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / ".xl_translator"
            target = root / "data" / "Translator"
            legacy.mkdir(parents=True)
            _write_sqlite_db(legacy / "tm.db")
            (legacy / "app.log").write_text("old log", encoding="utf-8")
            (legacy / "diagnostics" / "records").mkdir(parents=True)
            (legacy / "diagnostics" / "records" / "manifest.json").write_text(
                "{}",
                encoding="utf-8",
            )

            plan = inspect_data_migration(
                app_data_dir=target,
                legacy_data_dir=legacy,
                legacy_launcher_dir=legacy,
            )
            migrate_legacy_data(plan, include_support_files=True)

            support_root = target / "legacy_support" / legacy.name
            self.assertEqual((support_root / "app.log").read_text(encoding="utf-8"), "old log")
            self.assertTrue(
                (support_root / "diagnostics" / "records" / "manifest.json").exists()
            )

    def test_existing_target_primary_data_is_reported_as_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / ".xl_translator"
            target = root / "data" / "Translator"
            _write_sqlite_db(legacy / "tm.db")
            _write_sqlite_db(target / "tm.db")

            plan = inspect_data_migration(
                app_data_dir=target,
                legacy_data_dir=legacy,
                legacy_launcher_dir=legacy,
            )

            self.assertEqual(plan.status, "conflict")
            self.assertIn(target / "tm.db", plan.conflicts)

    def test_conflict_migration_still_restores_missing_keys_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / ".xl_translator"
            target = root / "data" / "Translator"
            legacy.mkdir(parents=True)
            target.mkdir(parents=True)
            (legacy / "settings.json").write_text('{"target_lang":"fr"}', encoding="utf-8")
            (legacy / "keys.json").write_text('{"custom_openai":"secret"}', encoding="utf-8")
            (target / "settings.json").write_text('{"target_lang":"en"}', encoding="utf-8")

            plan = inspect_data_migration(
                app_data_dir=target,
                legacy_data_dir=legacy,
                legacy_launcher_dir=legacy,
            )

            self.assertEqual(plan.status, "conflict")
            result = migrate_non_conflicting_legacy_data(plan)

            self.assertEqual(
                json.loads((target / "settings.json").read_text(encoding="utf-8")),
                {"target_lang": "en"},
            )
            self.assertEqual(
                json.loads((target / "keys.json").read_text(encoding="utf-8")),
                {"custom_openai": "secret"},
            )
            marker = json.loads((target / "migration.json").read_text(encoding="utf-8"))
            self.assertEqual(marker["status"], "partial_migrated")
            self.assertEqual(len(result.migrated), 1)

    def test_marked_migration_can_restore_missing_keys_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / ".xl_translator"
            target = root / "data" / "Translator"
            legacy.mkdir(parents=True)
            target.mkdir(parents=True)
            (legacy / "settings.json").write_text('{"target_lang":"fr"}', encoding="utf-8")
            (legacy / "keys.json").write_text('{"custom_openai":"secret"}', encoding="utf-8")
            (target / "settings.json").write_text('{"target_lang":"en"}', encoding="utf-8")
            (target / "migration.json").write_text(
                '{"status":"migrated"}',
                encoding="utf-8",
            )

            plan = inspect_data_migration(
                app_data_dir=target,
                legacy_data_dir=legacy,
                legacy_launcher_dir=legacy,
            )

            self.assertEqual(plan.status, "marked")
            result = migrate_non_conflicting_legacy_data(plan)

            self.assertEqual(
                json.loads((target / "settings.json").read_text(encoding="utf-8")),
                {"target_lang": "en"},
            )
            self.assertEqual(
                json.loads((target / "keys.json").read_text(encoding="utf-8")),
                {"custom_openai": "secret"},
            )
            marker = json.loads((target / "migration.json").read_text(encoding="utf-8"))
            self.assertEqual(marker["status"], "partial_migrated")
            self.assertEqual(len(result.migrated), 1)

    def test_skip_marker_suppresses_future_prompts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / ".xl_translator"
            target = root / "data" / "Translator"
            _write_sqlite_db(legacy / "tm.db")

            plan = inspect_data_migration(
                app_data_dir=target,
                legacy_data_dir=legacy,
                legacy_launcher_dir=legacy,
            )
            mark_migration_skipped(plan)

            next_plan = inspect_data_migration(
                app_data_dir=target,
                legacy_data_dir=legacy,
                legacy_launcher_dir=legacy,
            )
            self.assertEqual(next_plan.status, "skipped")


if __name__ == "__main__":
    unittest.main(verbosity=2)
