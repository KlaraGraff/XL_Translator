"""A data-version mismatch must never leave the app refusing to save.

Older data is taken over or upgraded in place; only data this build genuinely
cannot read is set aside, and then it is backed up and the user is told where
the copy went.  Every test reads the file back from disk, because the defect
these cover was a status that reported one thing while the write path did
another.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import settings as settings_module
from api.app import create_app
from core import maintenance, tm_manager


# The v2 schema: identical ``tm_entries``, without the two tables v3 added.
_V2_ENTRIES_DDL = """
CREATE TABLE tm_entries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_text   TEXT    NOT NULL,
    source_hash   TEXT    NOT NULL DEFAULT '',
    target_text   TEXT    NOT NULL,
    lang_pair     TEXT    NOT NULL,
    word_type     TEXT    NOT NULL,
    source_engine TEXT    DEFAULT '',
    pinned        INTEGER NOT NULL DEFAULT 0,
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_text, lang_pair)
);
CREATE TABLE tm_meta (
    meta_key   TEXT PRIMARY KEY,
    meta_value TEXT NOT NULL
);
"""

# A v2 shape whose rows never got a hash — the column sits at its default of
# ''.  This is what a real legacy database looks like: the backfill exists
# precisely because these rows were written before hashing did.
_V2_UNHASHED_DDL = _V2_ENTRIES_DDL

# A shape one column short of current, where the missing column can be grafted
# on: ``source_engine TEXT DEFAULT ''`` is exactly what ADD COLUMN accepts.
_ADDABLE_COLUMN_DDL = """
CREATE TABLE tm_entries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_text   TEXT    NOT NULL,
    source_hash   TEXT    NOT NULL DEFAULT '',
    target_text   TEXT    NOT NULL,
    lang_pair     TEXT    NOT NULL,
    word_type     TEXT    NOT NULL,
    pinned        INTEGER NOT NULL DEFAULT 0,
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_text, lang_pair)
);
CREATE TABLE tm_meta (
    meta_key   TEXT PRIMARY KEY,
    meta_value TEXT NOT NULL
);
"""

# A pre-v3 shape that really is lossy: ``source_hash`` never existed.
_MISSING_COLUMN_DDL = """
CREATE TABLE tm_entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_text TEXT NOT NULL,
    target_text TEXT NOT NULL,
    lang_pair   TEXT NOT NULL,
    word_type   TEXT NOT NULL
);
CREATE TABLE tm_meta (
    meta_key   TEXT PRIMARY KEY,
    meta_value TEXT NOT NULL
);
"""


# chmod 000 does not deny root, and does not mean this on Windows.  Evaluated
# at import time, so ``os.name`` has to be checked before ``os.geteuid``,
# which does not exist there at all.
_needs_permission_walls = unittest.skipIf(
    os.name == "nt" or os.geteuid() == 0,
    "a permission wall cannot be built here",
)


class _IsolatedDataDirTestCase(unittest.TestCase):
    """Keep settings, TM, backups and the recovery notice in a temp directory."""

    def setUp(self) -> None:
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db_path = self.root / "tm.db"
        self.settings_path = self.root / "settings.json"
        self.backups_dir = self.root / "backups"
        for patcher in (
            patch.object(settings_module, "APP_DATA_DIR", self.root),
            patch.object(settings_module, "SETTINGS_PATH", self.settings_path),
            patch.object(settings_module, "KEYS_PATH", self.root / "keys.json"),
            patch.object(settings_module, "RECOVERY_PATH", self.root / "recovery.json"),
            patch.object(settings_module, "BACKUPS_DIR", self.backups_dir),
            patch.object(tm_manager, "DB_PATH", self.db_path),
            patch.object(tm_manager, "BACKUPS_DIR", self.backups_dir),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_legacy_db(self, ddl: str, version: int, entries: int = 0) -> None:
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.executescript(ddl)
            conn.execute(
                "INSERT INTO tm_meta (meta_key, meta_value) VALUES (?, ?)",
                [tm_manager.TM_SCHEMA_VERSION_KEY, str(version)],
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(tm_entries)")}
            for index in range(entries):
                if "source_hash" in columns:
                    conn.execute(
                        "INSERT INTO tm_entries "
                        "(source_text, source_hash, target_text, lang_pair, word_type) "
                        "VALUES (?, ?, ?, ?, 'auto')",
                        [f"term-{index}", f"hash-{index}", f"词-{index}", "en-zh"],
                    )
                else:
                    conn.execute(
                        "INSERT INTO tm_entries "
                        "(source_text, target_text, lang_pair, word_type) "
                        "VALUES (?, ?, ?, 'auto')",
                        [f"term-{index}", f"词-{index}", "en-zh"],
                    )
            conn.commit()
        finally:
            conn.close()

    def db_tables(self) -> set[str]:
        conn = sqlite3.connect(str(self.db_path))
        try:
            return {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        finally:
            conn.close()

    def db_version(self) -> int:
        conn = sqlite3.connect(str(self.db_path))
        try:
            row = conn.execute(
                "SELECT meta_value FROM tm_meta WHERE meta_key = ?",
                [tm_manager.TM_SCHEMA_VERSION_KEY],
            ).fetchone()
            return int(row[0])
        finally:
            conn.close()


class TmSchemaUpgradeTests(_IsolatedDataDirTestCase):
    def test_v2_database_is_upgraded_additively_without_losing_entries(self) -> None:
        self.write_legacy_db(_V2_ENTRIES_DDL, version=2, entries=7)

        status_before = tm_manager.get_schema_status()
        tm_manager.init_db()

        self.assertEqual(status_before["state"], "upgraded")
        self.assertTrue(status_before["can_write"])
        self.assertEqual(status_before["stored_version"], 2)

        self.assertEqual(tm_manager.count_entries(), 7)
        self.assertEqual(self.db_version(), tm_manager.TM_SCHEMA_VERSION)
        self.assertLessEqual(
            {"tm_entries", "tm_meta", "tm_conflict_candidates", "tm_cleaning_suggestions"},
            self.db_tables(),
        )
        # An in-place upgrade keeps everything, so there is nothing to report.
        self.assertEqual(settings_module.read_recovery_record(), {})
        self.assertFalse(self.backups_dir.exists())
        self.assertEqual(tm_manager.get_schema_status()["state"], "current")

    def test_entries_still_usable_after_upgrade(self) -> None:
        self.write_legacy_db(_V2_ENTRIES_DDL, version=2, entries=3)
        tm_manager.init_db()

        self.assertTrue(tm_manager.insert_manual_entry("term-0", "改过的译文", "en-zh"))
        self.assertTrue(tm_manager.insert_manual_entry("term-new", "新词", "en-zh"))

        self.assertEqual(tm_manager.count_entries(), 4)
        self.assertEqual(
            tm_manager.lookup_batch(["term-0", "term-new"], "en-zh"),
            {"term-0": "改过的译文", "term-new": "新词"},
        )

    def test_legacy_rows_without_hashes_upgrade_instead_of_crashing(self) -> None:
        """The unique hash index must be built after the backfill, not before.

        A real legacy database carries ``source_hash`` at its column default of
        '' on every row.  Creating ``idx_hash_lang`` (UNIQUE) first fails on the
        second row, and the failure lands *after* the version has been stamped:
        the status says writable while every TM call raises.
        """
        self.write_legacy_db(_V2_UNHASHED_DDL, version=2, entries=5)
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute("UPDATE tm_entries SET source_hash = ''")
            conn.commit()
        finally:
            conn.close()

        tm_manager.init_db()

        self.assertEqual(tm_manager.count_entries(), 5)
        self.assertEqual(self.db_version(), tm_manager.TM_SCHEMA_VERSION)
        # Every row got a distinct hash, so lookups actually resolve.
        self.assertEqual(
            tm_manager.lookup_batch(["term-0", "term-4"], "en-zh"),
            {"term-0": "词-0", "term-4": "词-4"},
        )
        self.assertEqual(settings_module.read_recovery_record(), {})
        self.assertFalse(self.backups_dir.exists())

    def test_a_column_that_can_be_grafted_on_keeps_every_entry(self) -> None:
        """A missing column is one ALTER TABLE, not a reason to wipe the memory."""
        self.write_legacy_db(_ADDABLE_COLUMN_DDL, version=2, entries=6)

        status_before = tm_manager.get_schema_status()
        tm_manager.init_db()

        self.assertEqual(status_before["state"], "upgraded")
        self.assertEqual(tm_manager.count_entries(), 6)
        conn = sqlite3.connect(str(self.db_path))
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(tm_entries)")}
        finally:
            conn.close()
        self.assertIn("source_engine", columns)
        self.assertEqual(settings_module.read_recovery_record(), {})
        self.assertFalse(self.backups_dir.exists())

    def test_a_locked_database_is_left_alone_rather_than_rebuilt(self) -> None:
        """A held write lock is another writer working, not a broken file.

        Rebuilding on a lock would delete the user's whole memory at the one
        moment the app is busiest — a task flushing its TM batch is exactly
        what holds this lock.
        """
        self.write_legacy_db(_V2_ENTRIES_DDL, version=2, entries=5)
        tm_manager.init_db()
        self.assertEqual(tm_manager.count_entries(), 5)

        holder = sqlite3.connect(str(self.db_path))
        try:
            holder.execute("PRAGMA journal_mode = DELETE")
            holder.execute("BEGIN EXCLUSIVE")
            holder.execute(
                "INSERT INTO tm_entries "
                "(source_text, source_hash, target_text, lang_pair, word_type) "
                "VALUES ('locked', 'locked-hash', '占用中', 'en-zh', 'auto')"
            )

            # Shortened only so the test does not sit out the real timeout.
            with patch.object(tm_manager, "_INSPECT_BUSY_TIMEOUT_MS", 200):
                state, _version = tm_manager._inspect_db()
                status = tm_manager.get_schema_status()

            self.assertEqual(state, "busy")
            self.assertTrue(status["can_write"])
            self.assertTrue(self.db_path.is_file())
        finally:
            holder.rollback()
            holder.close()

        # Nothing was backed up, nothing was rebuilt, nothing was announced.
        self.assertEqual(tm_manager.count_entries(), 5)
        self.assertFalse(self.backups_dir.exists())
        self.assertEqual(settings_module.read_recovery_record(), {})

    def test_duplicate_source_rows_keep_every_entry_instead_of_dead_ending(self) -> None:
        """A table without ``UNIQUE(source_text, lang_pair)`` can hold two of a row.

        No recomputation makes those two hashes distinct, so the unique index
        can never be built.  Refusing to open the memory over an index that is
        only a lookup shortcut would be the worst trade available: the entries
        are all still there and all still correct.
        """
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.executescript(_V2_ENTRIES_DDL.replace("UNIQUE(source_text, lang_pair)", "id2 INTEGER"))
            conn.execute(
                "INSERT INTO tm_meta (meta_key, meta_value) VALUES (?, '2')",
                [tm_manager.TM_SCHEMA_VERSION_KEY],
            )
            for target in ("你好", "您好"):
                conn.execute(
                    "INSERT INTO tm_entries "
                    "(source_text, target_text, lang_pair, word_type) "
                    "VALUES ('hello', ?, 'en-zh', 'auto')",
                    [target],
                )
            conn.commit()
        finally:
            conn.close()

        tm_manager.init_db()

        self.assertEqual(tm_manager.count_entries(), 2)
        # Which of the two duplicates answers is the legacy table's business;
        # that a lookup answers at all is this build's.
        self.assertIn(tm_manager.lookup_batch(["hello"], "en-zh")["hello"], {"你好", "您好"})
        self.assertFalse(self.backups_dir.exists())
        self.assertEqual(settings_module.read_recovery_record(), {})

    @_needs_permission_walls
    def test_a_database_the_os_will_not_open_is_never_deleted(self) -> None:
        """The TM twin of the settings rule: no backup, no rebuild.

        A permission wall — or Windows AV holding the file — makes the copy
        impossible, and a rebuild that cannot copy first is indistinguishable
        from deleting the user's whole memory.
        """
        self.write_legacy_db(_V2_ENTRIES_DDL, version=2, entries=5)
        tm_manager.init_db()
        self.assertEqual(tm_manager.count_entries(), 5)
        original = self.db_path.read_bytes()

        self.db_path.chmod(0o000)

        def _restore_mode() -> None:
            if self.db_path.exists():
                self.db_path.chmod(0o600)

        self.addCleanup(_restore_mode)

        status = tm_manager.get_schema_status()
        self.assertEqual(status["state"], "unreadable")
        self.assertFalse(status["can_write"])

        with self.assertRaises(tm_manager.TmSchemaError):
            tm_manager.init_db()

        _restore_mode()
        self.assertEqual(self.db_path.read_bytes(), original)
        self.assertEqual(tm_manager.count_entries(), 5)
        self.assertEqual(settings_module.read_recovery_record(), {})

    @_needs_permission_walls
    def test_the_maintenance_clear_is_the_way_out_of_an_unopenable_database(self) -> None:
        """Every automatic path refuses; the button the user presses must not.

        Otherwise the refusal above is its own dead end — exactly what this
        whole change set exists to remove.
        """
        self.write_legacy_db(_V2_ENTRIES_DDL, version=2, entries=5)
        tm_manager.init_db()
        self.db_path.chmod(0o000)
        self.addCleanup(
            lambda: self.db_path.exists() and self.db_path.chmod(0o600)
        )

        result = maintenance.clear_tm()

        self.assertEqual(result.category, "tm")
        self.assertEqual(tm_manager.count_entries(), 0)
        self.assertEqual(tm_manager.get_schema_status()["state"], "current")

    def test_database_missing_a_required_column_is_backed_up_and_rebuilt(self) -> None:
        self.write_legacy_db(_MISSING_COLUMN_DDL, version=1, entries=4)

        status_before = tm_manager.get_schema_status()
        tm_manager.init_db()

        self.assertEqual(status_before["state"], "unusable")
        self.assertFalse(status_before["can_write"])

        self.assertEqual(tm_manager.count_entries(), 0)
        self.assertEqual(self.db_version(), tm_manager.TM_SCHEMA_VERSION)

        event = settings_module.read_recovery_record()["tm"]
        self.assertEqual(event["stored_version"], 1)
        backup = Path(event["backup_path"])
        self.assertTrue(backup.is_file())
        # The backup is a real database, not a stub: the rows are recoverable.
        conn = sqlite3.connect(str(backup))
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tm_entries").fetchone()[0], 4)
        finally:
            conn.close()

    def test_newer_database_is_backed_up_and_rebuilt(self) -> None:
        self.write_legacy_db(
            _V2_ENTRIES_DDL, version=tm_manager.TM_SCHEMA_VERSION + 4, entries=2
        )

        status_before = tm_manager.get_schema_status()
        tm_manager.init_db()

        self.assertEqual(status_before["state"], "unusable")
        self.assertEqual(
            status_before["stored_version"], tm_manager.TM_SCHEMA_VERSION + 4
        )
        self.assertEqual(tm_manager.count_entries(), 0)
        self.assertEqual(
            settings_module.read_recovery_record()["tm"]["stored_version"],
            tm_manager.TM_SCHEMA_VERSION + 4,
        )

    def test_corrupt_database_file_is_backed_up_and_rebuilt(self) -> None:
        self.db_path.write_bytes(b"this is not a sqlite database at all")

        self.assertEqual(tm_manager.get_schema_status()["state"], "unusable")
        tm_manager.init_db()

        self.assertEqual(tm_manager.count_entries(), 0)
        event = settings_module.read_recovery_record()["tm"]
        self.assertEqual(
            Path(event["backup_path"]).read_bytes(),
            b"this is not a sqlite database at all",
        )

    def test_missing_database_is_created_without_a_recovery_notice(self) -> None:
        self.assertEqual(tm_manager.get_schema_status()["state"], "missing")
        tm_manager.init_db()

        self.assertEqual(tm_manager.get_schema_status()["state"], "current")
        self.assertEqual(settings_module.read_recovery_record(), {})


class DataHealthReportTests(_IsolatedDataDirTestCase):
    def health(self) -> dict:
        return maintenance.data_health()

    def test_clean_install_reports_current(self) -> None:
        report = self.health()

        self.assertEqual(report["settings"]["state"], "current")
        self.assertEqual(report["tm"]["state"], "current")
        self.assertEqual(report["settings"]["backup_path"], "")
        self.assertEqual(report["tm"]["backup_path"], "")

    def test_old_but_usable_data_reports_adopted_and_upgraded(self) -> None:
        self.settings_path.write_text(
            json.dumps(
                {
                    "settings_version": settings_module.SETTINGS_SCHEMA_VERSION - 2,
                    "custom_prompt": "keep-me",
                }
            ),
            encoding="utf-8",
        )
        self.write_legacy_db(_V2_ENTRIES_DDL, version=2, entries=5)

        report = self.health()

        self.assertEqual(report["settings"]["state"], "adopted")
        self.assertEqual(
            report["settings"]["stored_version"],
            settings_module.SETTINGS_SCHEMA_VERSION - 2,
        )
        self.assertEqual(report["settings"]["backup_path"], "")
        self.assertEqual(report["tm"]["state"], "upgraded")
        self.assertEqual(report["tm"]["stored_version"], 2)
        self.assertEqual(report["tm"]["backup_path"], "")
        self.assertEqual(tm_manager.count_entries(), 5)

    def test_rebuilt_data_reports_recreated_until_dismissed(self) -> None:
        self.settings_path.write_text("not json", encoding="utf-8")
        self.db_path.write_bytes(b"not a database")

        report = self.health()

        for scope in ("settings", "tm"):
            self.assertEqual(report[scope]["state"], "recreated")
            self.assertTrue(Path(report[scope]["backup_path"]).is_file())

        # The notice survives a restart: it is the only way the user learns
        # where the backup went.
        self.assertEqual(self.health()["settings"]["state"], "recreated")

        maintenance.dismiss_recovery_notice()
        after = self.health()
        self.assertEqual(after["settings"]["state"], "current")
        self.assertEqual(after["tm"]["state"], "current")
        self.assertEqual(after["settings"]["backup_path"], "")


class DataHealthApiTests(_IsolatedDataDirTestCase):
    def client(self) -> TestClient:
        return TestClient(create_app())

    def test_health_endpoint_reports_and_notice_can_be_cleared(self) -> None:
        self.settings_path.write_text("{oops", encoding="utf-8")
        self.write_legacy_db(_V2_ENTRIES_DDL, version=2, entries=6)

        with self.client() as client:
            report = client.get("/api/data/health").json()

            self.assertEqual(report["settings"]["state"], "recreated")
            self.assertEqual(
                report["settings"]["current_version"],
                settings_module.SETTINGS_SCHEMA_VERSION,
            )
            self.assertTrue(report["settings"]["backup_path"])
            self.assertEqual(report["tm"]["state"], "upgraded")
            self.assertEqual(report["tm"]["stored_version"], 2)
            self.assertEqual(
                report["tm"]["current_version"], tm_manager.TM_SCHEMA_VERSION
            )
            self.assertEqual(report["tm"]["backup_path"], "")

            self.assertEqual(client.delete("/api/data/health/notice").status_code, 200)

            cleared = client.get("/api/data/health").json()
            self.assertEqual(cleared["settings"]["state"], "current")
            self.assertEqual(cleared["settings"]["backup_path"], "")

        self.assertEqual(tm_manager.count_entries(), 6)

    @_needs_permission_walls
    def test_health_says_unreadable_rather_than_current_when_a_file_is_blocked(self) -> None:
        """Nothing was lost and nothing can be saved — both have to be said.

        Reporting "current" here would leave the user looking at a healthy
        settings page while every save comes back 409, which is the same
        status-disagrees-with-the-write-path defect this whole change removes.
        """
        self.settings_path.write_text(json.dumps({"custom_prompt": "keep-me"}), encoding="utf-8")
        self.settings_path.chmod(0o000)
        self.addCleanup(
            lambda: self.settings_path.exists() and self.settings_path.chmod(0o600)
        )

        with self.client() as client:
            report = client.get("/api/data/health").json()

        self.assertEqual(report["settings"]["state"], "unreadable")
        self.assertEqual(report["settings"]["backup_path"], "")
        # Refused, not overwritten: the file is byte-for-byte what it was.
        self.settings_path.chmod(0o600)
        self.assertEqual(
            json.loads(self.settings_path.read_text(encoding="utf-8")),
            {"custom_prompt": "keep-me"},
        )

    def test_settings_writes_are_not_rejected_on_an_older_settings_file(self) -> None:
        self.settings_path.write_text(
            json.dumps(
                {
                    "settings_version": settings_module.SETTINGS_SCHEMA_VERSION - 2,
                    "custom_prompt": "keep-me",
                }
            ),
            encoding="utf-8",
        )

        with self.client() as client:
            response = client.put(
                "/api/models/throughput/translation",
                json={"concurrency": 2},
            )

        self.assertNotEqual(response.status_code, 409)
        self.assertLess(response.status_code, 400, response.text)
        persisted = json.loads(self.settings_path.read_text(encoding="utf-8"))
        self.assertEqual(
            persisted["settings_version"], settings_module.SETTINGS_SCHEMA_VERSION
        )
        self.assertEqual(persisted["custom_prompt"], "keep-me")


if __name__ == "__main__":
    unittest.main()
