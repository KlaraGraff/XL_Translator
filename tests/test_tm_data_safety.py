"""TM defects where the user loses data without being told.

Every test here asserts the database state read back on a fresh connection,
not just the return value: the shipped bugs all reported success while the
rows on disk stayed wrong.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core import maintenance, tm_manager


class IsolatedTmTestCase(unittest.TestCase):
    """Point the TM at a throwaway database, never the developer's own."""

    def setUp(self) -> None:
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db_path = self.root / "tm.db"
        for patcher in (
            patch.dict(os.environ, {"TRANSLATOR_APP_DATA_DIR": str(self.root)}),
            patch.object(tm_manager, "DB_PATH", self.db_path),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.assertEqual(tm_manager.DB_PATH.parent, self.root)
        tm_manager.init_db()

    def rows(self, sql: str, params: list | None = None) -> list[sqlite3.Row]:
        """Read committed state back through an independent connection."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql, params or []).fetchall()
        finally:
            conn.close()

    def stored(self, lang_pair: str = "en-zh") -> dict[str, sqlite3.Row]:
        return {
            row["source_text"]: row
            for row in self.rows(
                "SELECT source_text, target_text, word_type, pinned, source_engine "
                "FROM tm_entries WHERE lang_pair = ?",
                [lang_pair],
            )
        }


class DeleteAllEntriesTests(IsolatedTmTestCase):
    def _seed(self) -> None:
        tm_manager.insert_manual_entry("beam", "梁", "en-zh")
        tm_manager.insert_manual_entry("column", "柱", "en-zh")
        tm_manager.insert_manual_entry("slab", "板", "en-zh")
        tm_manager.insert_manual_entry("keep", "保留", "en-ja")
        pinned_id = self.rows(
            "SELECT id FROM tm_entries WHERE source_text = 'beam' AND lang_pair = 'en-zh'"
        )[0]["id"]
        tm_manager.pin_entry(int(pinned_id), True)
        self.assertEqual(self.stored()["beam"]["pinned"], 1)

    def test_clearing_a_language_pair_removes_pinned_entries_too(self) -> None:
        self._seed()

        removed = tm_manager.delete_all_entries("en-zh")

        self.assertEqual(removed, 3)
        # The user pressed "clear"; nothing may survive in that scope.
        self.assertEqual(self.stored(), {})
        # ...and the clear stays inside the scope it was given.
        self.assertEqual(set(self.stored("en-ja")), {"keep"})

    def test_unpinned_delete_still_protects_pinned_entries(self) -> None:
        self._seed()

        removed = tm_manager.delete_unpinned_entries("en-zh")

        self.assertEqual(removed, 2)
        self.assertEqual(set(self.stored()), {"beam"})

    def test_clearing_with_a_keyword_removes_matching_pinned_entries(self) -> None:
        self._seed()

        removed = tm_manager.delete_all_entries("en-zh", keyword="beam")

        self.assertEqual(removed, 1)
        self.assertEqual(set(self.stored()), {"column", "slab"})


class ImportPinnedFlagTests(IsolatedTmTestCase):
    def test_entries_without_a_pinned_field_are_not_pinned(self) -> None:
        entries = [
            {"source_text": "missing", "target_text": "缺字段"},
            {"source_text": "null", "target_text": "空值", "pinned": None},
            {"source_text": "zero", "target_text": "零", "pinned": 0},
            {"source_text": "text-false", "target_text": "假", "pinned": "false"},
            {"source_text": "bool-false", "target_text": "布尔假", "pinned": False},
            {"source_text": "one", "target_text": "一", "pinned": 1},
            {"source_text": "text-true", "target_text": "真", "pinned": "true"},
            {"source_text": "bool-true", "target_text": "布尔真", "pinned": True},
        ]

        tm_manager.import_entries(entries, "en-zh", "skip")

        stored = self.stored()
        self.assertEqual(
            {source: row["pinned"] for source, row in stored.items()},
            {
                "missing": 0,
                "null": 0,
                "zero": 0,
                "text-false": 0,
                "bool-false": 0,
                "one": 1,
                "text-true": 1,
                "bool-true": 1,
            },
        )

    def test_imported_entries_stay_deletable(self) -> None:
        # The shipped combination: an import silently pinned everything, and
        # "clear" refused to touch pinned rows, so the library could never be
        # emptied again.
        tm_manager.import_entries(
            [{"source_text": f"w{index}", "target_text": f"译{index}"} for index in range(3)],
            "en-zh",
            "skip",
        )

        self.assertEqual(tm_manager.delete_unpinned_entries("en-zh"), 3)
        self.assertEqual(self.stored(), {})


class OverwriteImportTests(IsolatedTmTestCase):
    def _backup_entries(self) -> list[dict]:
        return [
            {
                "source_text": "auto",
                "target_text": "备份自动译文",
                "word_type": "auto",
                "pinned": 0,
                "source_engine": "translation-model",
                "created_at": "2026-07-20 10:00:00",
                "updated_at": "2026-07-20 10:01:00",
            },
            {
                "source_text": "pinned",
                "target_text": "备份固定译文",
                "word_type": "manual",
                "pinned": 1,
                "source_engine": "manual",
            },
            {
                "source_text": "fresh",
                "target_text": "备份新词",
                "word_type": "auto",
                "pinned": 0,
            },
        ]

    def _seed_current_library(self) -> None:
        with tm_manager._get_conn() as conn:
            tm_manager._upsert_entry(
                conn,
                "auto",
                "库里的旧译文",
                "en-zh",
                word_type=tm_manager.AUTO_WORD_TYPE,
                source_engine="translation-model",
            )
            tm_manager._upsert_entry(
                conn,
                "pinned",
                "库里的固定译文",
                "en-zh",
                word_type=tm_manager.MANUAL_WORD_TYPE,
                source_engine="manual",
                pinned=1,
            )
        self.assertEqual(self.stored()["auto"]["target_text"], "库里的旧译文")

    def test_restoring_a_backup_over_a_non_empty_library_really_overwrites(self) -> None:
        self._seed_current_library()

        result = tm_manager.import_entries(
            self._backup_entries(),
            "en-zh",
            "overwrite",
            preserve_status=True,
        )

        stored = self.stored()
        self.assertEqual(stored["auto"]["target_text"], "备份自动译文")
        self.assertEqual(stored["pinned"]["target_text"], "备份固定译文")
        self.assertEqual(stored["fresh"]["target_text"], "备份新词")
        # The restored row carries the backup's own status, not the status the
        # replaced row happened to have.
        self.assertEqual(stored["auto"]["word_type"], "auto")
        self.assertEqual(stored["auto"]["pinned"], 0)
        self.assertEqual(stored["pinned"]["pinned"], 1)
        self.assertEqual(
            self.rows(
                "SELECT updated_at FROM tm_entries "
                "WHERE source_text = 'auto' AND lang_pair = 'en-zh'"
            )[0]["updated_at"],
            "2026-07-20 10:01:00",
        )
        # Overwrite applies the value; it must not park it as a "candidate"
        # the user would have to review one by one.
        self.assertEqual(self.rows("SELECT id FROM tm_conflict_candidates"), [])

        # The numbers the restore dialog shows must match what happened.
        self.assertEqual(result["inserted"], 3)
        self.assertEqual(result["updated"], 2)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(result["duplicates"], 2)

    def test_overwrite_reapplies_rows_that_already_match_the_backup(self) -> None:
        tm_manager.import_entries(self._backup_entries(), "en-zh", "overwrite", preserve_status=True)

        result = tm_manager.import_entries(
            self._backup_entries(), "en-zh", "overwrite", preserve_status=True
        )

        # Re-running a restore is not "3 skipped": the end state is the
        # backup's, and the report says so.
        self.assertEqual(result, {"inserted": 3, "updated": 3, "skipped": 0, "duplicates": 3})
        self.assertEqual(self.stored()["auto"]["target_text"], "备份自动译文")

    def test_skip_mode_still_leaves_the_library_untouched(self) -> None:
        self._seed_current_library()

        result = tm_manager.import_entries(
            self._backup_entries(),
            "en-zh",
            "skip",
            preserve_status=True,
        )

        stored = self.stored()
        self.assertEqual(stored["auto"]["target_text"], "库里的旧译文")
        self.assertEqual(stored["pinned"]["target_text"], "库里的固定译文")
        self.assertEqual(stored["fresh"]["target_text"], "备份新词")
        self.assertEqual(result, {"inserted": 1, "updated": 0, "skipped": 2, "duplicates": 2})

    def test_keep_both_still_records_a_reviewable_candidate(self) -> None:
        self._seed_current_library()

        result = tm_manager.import_entries(
            self._backup_entries(),
            "en-zh",
            "keep_both",
            preserve_status=True,
        )

        self.assertEqual(self.stored()["auto"]["target_text"], "库里的旧译文")
        candidates = self.rows(
            "SELECT source_text, candidate_target FROM tm_conflict_candidates ORDER BY id"
        )
        self.assertEqual(
            [(row["source_text"], row["candidate_target"]) for row in candidates],
            [("auto", "备份自动译文"), ("pinned", "备份固定译文")],
        )
        self.assertEqual(result["skipped"], 2)


class WalModeTests(IsolatedTmTestCase):
    def test_connections_run_in_wal_mode(self) -> None:
        with tm_manager._get_conn() as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(str(mode).lower(), "wal")

    def test_wal_side_files_are_the_ones_maintenance_clears(self) -> None:
        tm_manager.insert_manual_entry("beam", "梁", "en-zh")
        holder = sqlite3.connect(str(self.db_path))
        self.addCleanup(holder.close)
        try:
            # A live connection is what keeps -wal/-shm on disk; a clean close
            # checkpoints and removes them.  A crash does not.
            holder.execute("SELECT COUNT(*) FROM tm_entries").fetchone()
            produced = {path.name for path in self.root.glob("tm.db*")}
            self.assertIn("tm.db-wal", produced)
            self.assertIn("tm.db-shm", produced)
            listed = {path.name for path in maintenance._tm_paths()}
            self.assertTrue(
                produced <= listed,
                f"维护页未覆盖实际产生的 TM 文件：{produced - listed}",
            )
        finally:
            holder.close()

    def test_an_open_read_transaction_does_not_block_a_writer(self) -> None:
        tm_manager.insert_manual_entry("beam", "梁", "en-zh")

        # The library page paging through entries: a read transaction held
        # open while a finishing task flushes its TM batch.
        reader = sqlite3.connect(str(self.db_path))
        reader.row_factory = sqlite3.Row
        self.addCleanup(reader.close)
        reader.execute("BEGIN DEFERRED")
        before = reader.execute("SELECT COUNT(*) AS total FROM tm_entries").fetchone()["total"]

        started = time.monotonic()
        wrote = tm_manager.insert_manual_entry("column", "柱", "en-zh")
        elapsed = time.monotonic() - started

        # Without WAL this waits out busy_timeout (5s) and then raises
        # "database is locked", which the task runner does not catch.
        self.assertTrue(wrote)
        self.assertLess(elapsed, 2.0, f"写入被读事务阻塞了 {elapsed:.2f}s")
        # The reader keeps its own snapshot instead of seeing a half-written one.
        during = reader.execute("SELECT COUNT(*) AS total FROM tm_entries").fetchone()["total"]
        self.assertEqual(during, before)
        reader.rollback()
        self.assertEqual(len(self.stored()), 2)


class _Sqlite3Shim:
    """Stand in for ``tm_manager``'s ``sqlite3`` global, wrapping connections.

    Patching the real module attribute would also wrap the test's own writer
    connection; replacing only the name ``tm_manager`` looks up keeps the
    interception on the code under test.
    """

    def __init__(self, connect) -> None:
        self._connect = connect

    def connect(self, *args, **kwargs):
        return self._connect(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(sqlite3, name)


class _ConnectionProxy:
    """Run a callback the first time a matching statement is executed."""

    def __init__(self, conn, needle: str, hook) -> None:
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "_needle", needle)
        object.__setattr__(self, "_hook", hook)

    def execute(self, sql, *args, **kwargs):
        cursor = object.__getattribute__(self, "_conn").execute(sql, *args, **kwargs)
        hook = object.__getattribute__(self, "_hook")
        if hook is not None and object.__getattribute__(self, "_needle") in sql:
            object.__setattr__(self, "_hook", None)
            hook()
        return cursor

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_conn"), name)

    def __setattr__(self, name, value):
        setattr(object.__getattribute__(self, "_conn"), name, value)


class FullExportSnapshotTests(IsolatedTmTestCase):
    def test_export_reads_one_snapshot_for_entries_and_conflicts(self) -> None:
        tm_manager.insert_manual_entry("alpha", "甲", "en-zh")
        tm_manager.import_entries(
            [{"source_text": "alpha", "target_text": "另一个甲"}],
            "en-zh",
            "keep_both",
        )

        def write_between_queries() -> None:
            """A background task commits while the export is running."""
            writer = sqlite3.connect(str(self.db_path))
            try:
                writer.execute("PRAGMA busy_timeout = 5000")
                writer.execute(
                    "INSERT INTO tm_entries (source_text, source_hash, target_text, "
                    "lang_pair, word_type, source_engine, pinned) "
                    "VALUES ('beta', 'hash-beta', '乙', 'en-zh', 'auto', 'model', 0)"
                )
                writer.execute(
                    "INSERT INTO tm_conflict_candidates (entry_id, source_text, "
                    "existing_target, candidate_target, lang_pair) "
                    "VALUES (999, 'beta', '乙', '另一个乙', 'en-zh')"
                )
                writer.commit()
            finally:
                writer.close()

        def connect_with_hook(*args, **kwargs):
            return _ConnectionProxy(
                sqlite3.connect(*args, **kwargs),
                "FROM tm_entries",
                write_between_queries,
            )

        with patch.object(tm_manager, "sqlite3", _Sqlite3Shim(connect_with_hook)):
            backup = tm_manager.get_full_export()

        # The interleaved write really did land.
        self.assertEqual(len(self.stored()), 2)
        exported_sources = {row["source_text"] for row in backup["entries"]}
        candidate_sources = {row["source_text"] for row in backup["conflict_candidates"]}
        self.assertEqual(exported_sources, {"alpha"})
        # A candidate for an entry the backup does not contain would make the
        # backup file contradict itself.
        self.assertEqual(candidate_sources, {"alpha"})
        self.assertTrue(candidate_sources <= exported_sources)


if __name__ == "__main__":
    unittest.main()
