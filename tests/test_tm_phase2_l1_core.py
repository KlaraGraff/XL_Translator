"""Focused Phase 2 L1 TM core contracts."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core import tm_cleaner, tm_manager


class _Cleaner:
    engine_name = "phase2-test"

    def chat(self, *, system: str, user: str) -> str:
        del system
        item = json.loads(user)[0]
        return json.dumps([{"id": item["id"], "suggested": "cleaned"}])


class TmPhase2L1CoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.old_db = tm_manager.DB_PATH
        self.old_backups = tm_manager.BACKUPS_DIR
        tm_manager.DB_PATH = root / "tm.db"
        tm_manager.BACKUPS_DIR = root / "backups"
        tm_manager.init_db()

    def tearDown(self) -> None:
        tm_manager.DB_PATH = self.old_db
        tm_manager.BACKUPS_DIR = self.old_backups
        self.temp_dir.cleanup()

    def test_explained_lookup_distinguishes_same_value_and_conflict(self) -> None:
        tm_manager.insert_manual_entry("same", "一致", "en-zh")
        tm_manager.insert_manual_entry("same", "一致", "fr-zh")
        tm_manager.insert_manual_entry("mixed", "甲", "en-zh")
        tm_manager.insert_manual_entry("mixed", "乙", "fr-zh")

        result = tm_manager.lookup_batch_explained(
            ["same", "mixed", "missing"], ["en-zh", "fr-zh"]
        )
        self.assertEqual(result["same"].status, "multi_language_same")
        self.assertEqual(result["same"].translation, "一致")
        self.assertEqual(result["mixed"].status, "conflict")
        self.assertIsNone(result["mixed"].translation)
        self.assertEqual(result["missing"].status, "miss")

    def test_auto_entry_gate_rejects_unknown_language_and_review_items(self) -> None:
        written = tm_manager.insert_auto_entries(
            [
                {"source_text": "ok", "translation": "好", "source_lang": "en"},
                {"source_text": "unknown", "translation": "未知", "source_lang": "und"},
                {
                    "source_text": "review",
                    "translation": "复核",
                    "source_lang": "en",
                    "tm_eligible": False,
                },
            ],
            "zh",
            25,
            allowed_source_langs={"en"},
        )
        self.assertEqual(written, 1)
        self.assertEqual(tm_manager.lookup_batch(["ok"], "en-zh")["ok"], "好")
        self.assertIsNone(tm_manager.lookup_batch(["unknown"], "en-zh")["unknown"])

    def test_cleaning_suggestion_version_is_checked_before_apply(self) -> None:
        tm_manager.insert_batch([("beam", "old")], "en-zh", 25)
        suggestions = tm_cleaner.run_cleaning(
            "en-zh", _Cleaner(), batch_size=1, concurrency=1
        )
        self.assertEqual(len(suggestions), 1)
        self.assertEqual(len(tm_manager.list_cleaning_suggestions()), 1)

        row, _ = tm_manager.search_entries("en-zh")
        self.assertTrue(tm_manager.update_entry_full(row[0]["id"], "beam", "user"))
        self.assertEqual(tm_cleaner.apply_suggestions(suggestions), 0)
        self.assertEqual(tm_manager.lookup_batch(["beam"], "en-zh")["beam"], "user")

    def test_conflict_candidate_can_be_resolved_without_duplicate_source(self) -> None:
        tm_manager.insert_batch([("term", "one")], "en-fr", 25)
        self.assertEqual(tm_manager.insert_batch([("term", "two")], "en-fr", 25), 0)
        candidate = tm_manager.list_conflict_candidates("en-fr")[0]
        self.assertTrue(tm_manager.resolve_conflict_candidate(candidate["id"], "use_candidate"))
        rows, _ = tm_manager.search_entries("en-fr", keyword="term")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["target_text"], "two")
        self.assertEqual(rows[0]["word_type"], tm_manager.MANUAL_WORD_TYPE)


if __name__ == "__main__":
    unittest.main()
