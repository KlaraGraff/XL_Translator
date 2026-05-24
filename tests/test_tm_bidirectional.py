from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from core import tm_manager


class BidirectionalTmTests(unittest.TestCase):
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
        with sqlite3.connect(str(tm_manager.DB_PATH)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT id, source_text, target_text, lang_pair, word_type, pinned
                FROM tm_entries
                WHERE source_text = ? AND lang_pair = ?
                """,
                [source, lang_pair],
            ).fetchone()
        return dict(row) if row is not None else None

    def test_manual_insert_creates_reverse_entry(self) -> None:
        self.assertTrue(tm_manager.insert_manual_entry("合同", "Contract", "zh-en"))

        self.assertEqual(
            tm_manager.lookup_batch(["合同"], "zh-en")["合同"],
            "Contract",
        )
        self.assertEqual(
            tm_manager.lookup_batch(["Contract"], "en-zh")["Contract"],
            "合同",
        )

    def test_manual_edit_moves_reverse_entry(self) -> None:
        tm_manager.insert_manual_entry("合同", "Contract", "zh-en")
        row = self._row("合同", "zh-en")
        assert row is not None

        self.assertTrue(tm_manager.update_entry_full(row["id"], "合同", "Agreement"))

        self.assertEqual(
            tm_manager.lookup_batch(["合同"], "zh-en")["合同"],
            "Agreement",
        )
        self.assertIsNone(
            tm_manager.lookup_batch(["Contract"], "en-zh")["Contract"],
        )
        self.assertEqual(
            tm_manager.lookup_batch(["Agreement"], "en-zh")["Agreement"],
            "合同",
        )

    def test_bulk_update_from_cleaning_moves_reverse_entry(self) -> None:
        tm_manager.insert_batch(
            [("楼梯", "Stair")],
            "zh-en",
            max_len=25,
            engine_name="engine",
        )
        row = self._row("楼梯", "zh-en")
        assert row is not None

        self.assertEqual(tm_manager.bulk_update([(row["id"], "Staircase")]), 1)

        self.assertIsNone(tm_manager.lookup_batch(["Stair"], "en-zh")["Stair"])
        self.assertEqual(
            tm_manager.lookup_batch(["Staircase"], "en-zh")["Staircase"],
            "楼梯",
        )

    def test_auto_write_does_not_overwrite_manual_or_pinned_entries(self) -> None:
        tm_manager.insert_manual_entry("Contract", "合同文本", "en-zh")
        written = tm_manager.insert_batch(
            [("合同", "Contract")],
            "zh-en",
            max_len=25,
            engine_name="engine",
        )

        self.assertEqual(written, 1)
        self.assertEqual(
            tm_manager.lookup_batch(["Contract"], "en-zh")["Contract"],
            "合同文本",
        )

        tm_manager.insert_manual_entry("土建", "Civil works", "zh-en")
        written = tm_manager.insert_batch(
            [("土建", "Construction")],
            "zh-en",
            max_len=25,
            engine_name="engine",
        )

        self.assertEqual(written, 0)
        self.assertEqual(
            tm_manager.lookup_batch(["土建"], "zh-en")["土建"],
            "Civil works",
        )
        self.assertIsNone(
            tm_manager.lookup_batch(["Construction"], "en-zh")["Construction"],
        )

    def test_delete_and_pin_sync_reverse_with_priority_protection(self) -> None:
        tm_manager.insert_manual_entry("门", "Door", "zh-en")
        row = self._row("门", "zh-en")
        reverse = self._row("Door", "en-zh")
        assert row is not None
        assert reverse is not None

        tm_manager.pin_entry(row["id"], True)
        self.assertEqual(self._row("Door", "en-zh")["pinned"], 1)
        tm_manager.pin_entry(reverse["id"], False)
        self.assertEqual(self._row("门", "zh-en")["pinned"], 0)

        tm_manager.delete_entry(row["id"])
        self.assertIsNone(self._row("门", "zh-en"))
        self.assertIsNone(self._row("Door", "en-zh"))

        tm_manager.insert_manual_entry("Beam", "梁", "en-zh")
        protected_reverse = self._row("Beam", "en-zh")
        current = self._row("梁", "zh-en")
        assert protected_reverse is not None
        assert current is not None
        with sqlite3.connect(str(tm_manager.DB_PATH)) as conn:
            conn.execute(
                "UPDATE tm_entries SET pinned = 1 WHERE id = ?",
                [protected_reverse["id"]],
            )
            conn.execute(
                "UPDATE tm_entries SET pinned = 0 WHERE id = ?",
                [current["id"]],
            )
            conn.commit()
        tm_manager.delete_entry(current["id"])

        self.assertIsNone(self._row("梁", "zh-en"))
        self.assertEqual(self._row("Beam", "en-zh")["target_text"], "梁")

    def test_import_creates_reverse_entries(self) -> None:
        result = tm_manager.import_entries(
            [{"source_text": "钢筋", "target_text": "Rebar"}],
            "zh-en",
            "overwrite",
        )

        self.assertEqual(result["inserted"], 2)
        self.assertEqual(
            tm_manager.lookup_batch(["钢筋"], "zh-en")["钢筋"],
            "Rebar",
        )
        self.assertEqual(
            tm_manager.lookup_batch(["Rebar"], "en-zh")["Rebar"],
            "钢筋",
        )

    def test_custom_language_pairs_can_reverse(self) -> None:
        lang_pair = "x-custom-source-x-custom-target"
        reverse_pair = "x-custom-target-x-custom-source"

        self.assertTrue(
            tm_manager.insert_manual_entry(
                "source term",
                "target term",
                lang_pair,
            )
        )

        self.assertEqual(
            tm_manager.lookup_batch(["target term"], reverse_pair)["target term"],
            "source term",
        )

    def test_existing_data_backfill_creates_reverse_entries_once(self) -> None:
        tm_manager.DB_PATH.unlink(missing_ok=True)
        tm_manager._ensure_current_schema()
        with sqlite3.connect(str(tm_manager.DB_PATH)) as conn:
            conn.execute(
                """
                INSERT INTO tm_entries (
                    source_text,
                    source_hash,
                    target_text,
                    lang_pair,
                    word_type,
                    source_engine,
                    pinned
                )
                VALUES (?, ?, ?, ?, 'term', 'legacy', 0)
                """,
                [
                    "楼板",
                    tm_manager._make_hash("楼板", "zh-en"),
                    "Slab",
                    "zh-en",
                ],
            )
            conn.execute(
                "DELETE FROM tm_meta WHERE meta_key = ?",
                [tm_manager.BIDIRECTIONAL_BACKFILL_KEY],
            )
            conn.commit()

        tm_manager._backfill_reverse_entries()
        tm_manager._backfill_reverse_entries()

        self.assertEqual(
            tm_manager.lookup_batch(["Slab"], "en-zh")["Slab"],
            "楼板",
        )
        self.assertEqual(tm_manager.get_stats("en-zh")["total"], 1)
        self.assertTrue(list((tm_manager.BACKUPS_DIR / "tm").glob("*.db")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
