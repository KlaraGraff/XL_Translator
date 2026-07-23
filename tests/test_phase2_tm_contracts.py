"""Phase 2 TM contracts against an isolated SQLite database.

These tests intentionally describe the frozen T2A/T2B/T2C behavior.  A small
set of ``expectedFailure`` cases records known gaps in the current migration;
they should be removed when the corresponding production contract lands.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path

from core import tm_manager
from core.language_registry import build_custom_target_lang_code
from core.tm_cleaner import TmCleaningBatchError, apply_suggestions, run_cleaning


class _FakeCleanerEngine:
    engine_name = "fake-cloud"

    def __init__(self, response: str = "[]", error_on_call: bool = False) -> None:
        self.response = response
        self.error_on_call = error_on_call

    def chat(self, *, system: str, user: str) -> str:
        del system, user
        if self.error_on_call:
            raise RuntimeError("provider unavailable")
        return self.response


class Phase2TmContractTests(unittest.TestCase):
    """T2A/T2B/T2C behavior with no real user database or settings involved."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.old_db_path = tm_manager.DB_PATH
        self.old_backups_dir = tm_manager.BACKUPS_DIR
        tm_manager.DB_PATH = self.root / "tm.db"
        tm_manager.BACKUPS_DIR = self.root / "backups"
        tm_manager.init_db()

    def tearDown(self) -> None:
        tm_manager.DB_PATH = self.old_db_path
        tm_manager.BACKUPS_DIR = self.old_backups_dir
        self.temp_dir.cleanup()

    def _row(self, source: str, lang_pair: str) -> dict | None:
        with closing(sqlite3.connect(str(tm_manager.DB_PATH))) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT id, source_text, target_text, lang_pair, word_type,
                       source_engine, pinned, updated_at
                FROM tm_entries
                WHERE source_text = ? AND lang_pair = ?
                """,
                [source, lang_pair],
            ).fetchone()
        return dict(row) if row is not None else None

    def test_auto_insert_is_forward_only_by_default(self) -> None:
        """T2A-01: automatic translation must never create a reverse entry."""
        tm_manager.insert_batch(
            [("合同", "Contract")],
            "zh-en",
            max_len=25,
            engine_name="engine",
            sync_reverse=False,
        )
        self.assertEqual(tm_manager.lookup_batch(["合同"], "zh-en")["合同"], "Contract")
        self.assertIsNone(tm_manager.lookup_batch(["Contract"], "en-zh")["Contract"])

    def test_auto_insert_default_does_not_reverse_without_opt_in(self) -> None:
        """T2A-01: the public automatic-write default is forward-only."""
        tm_manager.insert_batch(
            [("合同", "Contract")],
            "zh-en",
            max_len=25,
            engine_name="engine",
        )
        self.assertIsNone(tm_manager.lookup_batch(["Contract"], "en-zh")["Contract"])

    def test_manual_reverse_sync_is_explicit(self) -> None:
        """T2A-02: manual entry supports opt-in and opt-out reverse sync."""
        tm_manager.insert_manual_entry("门", "Door", "zh-en", sync_reverse=False)
        self.assertIsNone(tm_manager.lookup_batch(["Door"], "en-zh")["Door"])

        tm_manager.insert_manual_entry("窗", "Window", "zh-en", sync_reverse=True)
        self.assertEqual(tm_manager.lookup_batch(["Window"], "en-zh")["Window"], "窗")

    def test_manual_default_reverse_sync_is_off(self) -> None:
        """T2A-02: the UI/API default must be unchecked/off."""
        tm_manager.insert_manual_entry("墙", "Wall", "zh-en")
        self.assertIsNone(tm_manager.lookup_batch(["Wall"], "en-zh")["Wall"])

    @unittest.expectedFailure
    def test_reverse_conflict_never_overwrites_existing_translation(self) -> None:
        """T2A-03: a different existing reverse value wins over synchronization."""
        tm_manager.insert_manual_entry("Contract", "旧译", "en-zh", sync_reverse=False)
        tm_manager.insert_manual_entry("合同", "Contract", "zh-en", sync_reverse=True)
        self.assertEqual(tm_manager.lookup_batch(["Contract"], "en-zh")["Contract"], "旧译")

    def test_custom_target_never_creates_unreachable_reverse_entry(self) -> None:
        """T2A-04: custom languages are target-only, including manual sync."""
        code = build_custom_target_lang_code("Engineering")
        pair = f"en-{code}"
        tm_manager.insert_manual_entry("beam", "梁", pair, sync_reverse=True)
        self.assertIsNone(
            tm_manager.lookup_batch(["梁"], f"{code}-en")["梁"]
        )

    def test_automatic_write_preserves_manual_and_pinned_entries(self) -> None:
        """T2A-07/T2A-12: manual and pinned rows outrank automatic writes."""
        tm_manager.insert_manual_entry("合同", "人工译文", "zh-en", sync_reverse=False)
        self.assertEqual(
            tm_manager.insert_batch(
                [("合同", "自动译文")],
                "zh-en",
                max_len=25,
                engine_name="engine",
                sync_reverse=False,
            ),
            0,
        )
        self.assertEqual(tm_manager.lookup_batch(["合同"], "zh-en")["合同"], "人工译文")

        tm_manager.insert_batch(
            [("门", "Door")], "zh-en", max_len=25, engine_name="engine", sync_reverse=False
        )
        row = self._row("门", "zh-en")
        assert row is not None
        tm_manager.pin_entry(row["id"], True)
        self.assertEqual(
            tm_manager.insert_batch(
                [("门", "Gate")],
                "zh-en",
                max_len=25,
                engine_name="engine",
                sync_reverse=False,
            ),
            0,
        )
        self.assertEqual(tm_manager.lookup_batch(["门"], "zh-en")["门"], "Door")

    def test_automatic_length_gate_and_same_pair_lookup(self) -> None:
        """T2A-05/T2A-06: overlong automatic rows are excluded and pairs stay isolated."""
        written = tm_manager.insert_batch(
            [("a" * 26, "too long")],
            "fr-en",
            max_len=25,
            engine_name="engine",
            sync_reverse=False,
        )
        self.assertEqual(written, 0)
        tm_manager.insert_batch(
            [("Bonjour", "Hello")],
            "fr-en",
            max_len=25,
            engine_name="engine",
            sync_reverse=False,
        )
        self.assertEqual(tm_manager.lookup_batch(["Bonjour"], "fr-en")["Bonjour"], "Hello")
        self.assertIsNone(tm_manager.lookup_batch(["Bonjour"], "en-fr")["Bonjour"])

    def test_same_translation_does_not_touch_row_timestamp(self) -> None:
        """T2A-09: an identical result is a zero-change operation."""
        tm_manager.insert_batch(
            [("same", "value")], "en-fr", max_len=25, engine_name="engine", sync_reverse=False
        )
        row = self._row("same", "en-fr")
        assert row is not None
        first_updated_at = row["updated_at"]
        self.assertEqual(
            tm_manager.insert_batch(
                [("same", "value")],
                "en-fr",
                max_len=25,
                engine_name="engine",
                sync_reverse=False,
            ),
            0,
        )
        self.assertEqual(self._row("same", "en-fr")["updated_at"], first_updated_at)

    def test_pagination_and_statistics_cover_all_entries(self) -> None:
        """T2B-03: search exposes every row beyond the first 50."""
        tm_manager.insert_batch(
            [(f"term-{i}", f"译文-{i}") for i in range(55)],
            "en-zh",
            max_len=25,
            engine_name="engine",
            sync_reverse=False,
        )
        page_one, total = tm_manager.search_entries("en-zh", page=1, page_size=50)
        page_two, total_two = tm_manager.search_entries("en-zh", page=2, page_size=50)
        self.assertEqual(total, 55)
        self.assertEqual(total_two, 55)
        self.assertEqual(len(page_one), 50)
        self.assertEqual(len(page_two), 5)
        self.assertTrue({row["id"] for row in page_one}.isdisjoint({row["id"] for row in page_two}))
        self.assertEqual(tm_manager.get_stats("en-zh")["total"], 55)

    def test_single_pair_export_and_import_preserve_status_fields(self) -> None:
        """T2B-06/T2B-07: single-pair exchange preserves source/target/status data."""
        tm_manager.insert_manual_entry("术语", "Term", "zh-en", sync_reverse=False)
        row = self._row("术语", "zh-en")
        assert row is not None
        tm_manager.pin_entry(row["id"], True)
        exported = tm_manager.get_all_entries_for_export("zh-en")
        self.assertEqual(len(exported), 1)
        self.assertEqual(exported[0]["source_text"], "术语")
        self.assertEqual(exported[0]["target_text"], "Term")
        self.assertEqual(exported[0]["word_type"], "manual")
        self.assertEqual(exported[0]["pinned"], 1)

        imported = tm_manager.import_entries(
            [{"source_text": "新增", "target_text": "New", "word_type": "manual", "pinned": 0}],
            "zh-en",
            "skip",
            sync_reverse=False,
        )
        self.assertEqual(imported["inserted"], 1)
        self.assertEqual(tm_manager.lookup_batch(["新增"], "zh-en")["新增"], "New")

    @unittest.expectedFailure
    def test_keep_both_does_not_create_pseudo_backup_source(self) -> None:
        """T2A-08/T2B-07: conflicts become candidates, not '[导入备份]' rows."""
        tm_manager.insert_manual_entry("same", "existing", "en-fr", sync_reverse=False)
        tm_manager.import_entries(
            [{"source_text": "same", "target_text": "candidate"}],
            "en-fr",
            "keep_both",
            sync_reverse=False,
        )
        rows, _ = tm_manager.search_entries("en-fr", keyword="same", page=1, page_size=50)
        self.assertEqual(len(rows), 1)
        self.assertNotIn("[导入备份]", rows[0]["source_text"])

    def test_custom_language_reference_count_is_isolated_by_pair(self) -> None:
        """T2B-02: a custom target reference can be detected before deletion."""
        code = "x-custom-engineering"
        tm_manager.insert_manual_entry(
            "beam", "梁", f"en-{code}", sync_reverse=False
        )
        self.assertEqual(tm_manager.count_entries_referencing_language(code), 1)
        self.assertEqual(tm_manager.count_entries_referencing_language("fr"), 0)

    @unittest.expectedFailure
    def test_cleaning_scope_excludes_manual_and_pinned_rows(self) -> None:
        """T2C-01: only ordinary automatic rows enter routine cleaning."""
        tm_manager.insert_batch(
            [("auto", "自动")], "en-zh", max_len=25, engine_name="engine", sync_reverse=False
        )
        tm_manager.insert_manual_entry("manual", "人工", "en-zh", sync_reverse=False)
        tm_manager.insert_batch(
            [("pinned", "固定")],
            "en-zh",
            max_len=25,
            engine_name="engine",
            sync_reverse=False,
        )
        pinned = self._row("pinned", "en-zh")
        assert pinned is not None
        tm_manager.pin_entry(pinned["id"], True)
        entries = tm_manager.get_all_entries_for_cleaning("en-zh")
        self.assertEqual([entry["source_text"] for entry in entries], ["auto"])

    def test_cleaning_is_suggestion_only_until_user_confirmation(self) -> None:
        """T2C-04: a model suggestion cannot change TM before apply_suggestions."""
        tm_manager.insert_batch(
            [("beam", "旧译")], "en-zh", max_len=25, engine_name="engine", sync_reverse=False
        )
        row = self._row("beam", "en-zh")
        assert row is not None
        engine = _FakeCleanerEngine(
            json.dumps([{"id": row["id"], "suggested": "新译"}], ensure_ascii=False)
        )
        suggestions = run_cleaning("en-zh", engine, batch_size=20, concurrency=1)
        self.assertEqual(tm_manager.lookup_batch(["beam"], "en-zh")["beam"], "旧译")
        self.assertEqual(len(suggestions), 1)
        self.assertEqual(apply_suggestions(suggestions, sync_reverse=False), 1)
        self.assertEqual(tm_manager.lookup_batch(["beam"], "en-zh")["beam"], "新译")

    def test_cleaning_cancelled_before_submission_leaves_tm_unchanged(self) -> None:
        """T2C-03: cancellation before work starts yields no write."""
        tm_manager.insert_batch(
            [("beam", "旧译")], "en-zh", max_len=25, engine_name="engine", sync_reverse=False
        )
        cancel_event = threading.Event()
        cancel_event.set()
        engine = _FakeCleanerEngine(
            json.dumps([{"id": 1, "suggested": "不应写入"}], ensure_ascii=False)
        )
        suggestions = run_cleaning(
            "en-zh", engine, batch_size=1, concurrency=1, cancel_event=cancel_event
        )
        self.assertEqual(suggestions, [])
        self.assertEqual(tm_manager.lookup_batch(["beam"], "en-zh")["beam"], "旧译")

    def test_cleaning_batch_failure_does_not_write_partial_results(self) -> None:
        """T2C-03: batch failure is surfaced and TM remains untouched."""
        tm_manager.insert_batch(
            [("beam", "旧译")], "en-zh", max_len=25, engine_name="engine", sync_reverse=False
        )
        with self.assertRaises(TmCleaningBatchError):
            run_cleaning(
                "en-zh",
                _FakeCleanerEngine(error_on_call=True),
                batch_size=1,
                concurrency=1,
            )
        self.assertEqual(tm_manager.lookup_batch(["beam"], "en-zh")["beam"], "旧译")


if __name__ == "__main__":
    unittest.main(verbosity=2)
