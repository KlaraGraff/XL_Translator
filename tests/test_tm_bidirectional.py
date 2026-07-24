from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from core import tm_manager
from core.language_registry import build_custom_target_lang_code


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
        with closing(sqlite3.connect(str(tm_manager.DB_PATH))) as conn:
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

    def test_manual_insert_creates_reverse_entry_when_explicit(self) -> None:
        self.assertTrue(
            tm_manager.insert_manual_entry(
                "合同", "Contract", "zh-en", sync_reverse=True
            )
        )

        self.assertEqual(
            tm_manager.lookup_batch(["合同"], "zh-en")["合同"],
            "Contract",
        )
        self.assertEqual(
            tm_manager.lookup_batch(["Contract"], "en-zh")["Contract"],
            "合同",
        )

    def test_manual_edit_moves_reverse_entry(self) -> None:
        tm_manager.insert_manual_entry("合同", "Contract", "zh-en", sync_reverse=True)
        row = self._row("合同", "zh-en")
        assert row is not None

        self.assertTrue(
            tm_manager.update_entry_full(
                row["id"], "合同", "Agreement", sync_reverse=True
            )
        )

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

    def test_bulk_update_from_cleaning_stays_forward_only(self) -> None:
        tm_manager.insert_batch(
            [("楼梯", "Stair")],
            "zh-en",
            max_len=25,
            engine_name="engine",
            sync_reverse=False,
        )
        row = self._row("楼梯", "zh-en")
        assert row is not None

        self.assertEqual(
            tm_manager.bulk_update([(row["id"], "Staircase")], sync_reverse=False),
            1,
        )

        self.assertIsNone(tm_manager.lookup_batch(["Stair"], "en-zh")["Stair"])
        self.assertIsNone(tm_manager.lookup_batch(["Staircase"], "en-zh")["Staircase"])

    def test_direct_automatic_upsert_stays_forward_only(self) -> None:
        tm_manager.insert_batch(
            [("楼梯", "Stair")],
            "zh-en",
            max_len=25,
            engine_name="engine",
            sync_reverse=False,
        )

        tm_manager.insert_batch(
            [("楼梯", "Staircase")],
            "zh-en",
            max_len=25,
            engine_name="engine",
            sync_reverse=False,
        )

        self.assertIsNone(tm_manager.lookup_batch(["Stair"], "en-zh")["Stair"])
        self.assertIsNone(tm_manager.lookup_batch(["Staircase"], "en-zh")["Staircase"])

    def test_auto_write_does_not_overwrite_manual_or_pinned_entries(self) -> None:
        tm_manager.insert_manual_entry("Contract", "合同文本", "en-zh", sync_reverse=False)
        written = tm_manager.insert_batch(
            [("合同", "Contract")],
            "zh-en",
            max_len=25,
            engine_name="engine",
            sync_reverse=False,
        )

        self.assertEqual(written, 1)
        self.assertEqual(
            tm_manager.lookup_batch(["Contract"], "en-zh")["Contract"],
            "合同文本",
        )

        tm_manager.insert_manual_entry("土建", "Civil works", "zh-en", sync_reverse=False)
        written = tm_manager.insert_batch(
            [("土建", "Construction")],
            "zh-en",
            max_len=25,
            engine_name="engine",
            sync_reverse=False,
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
        tm_manager.insert_manual_entry("门", "Door", "zh-en", sync_reverse=True)
        row = self._row("门", "zh-en")
        reverse = self._row("Door", "en-zh")
        assert row is not None
        assert reverse is not None

        tm_manager.pin_entry(row["id"], True)
        self.assertEqual(self._row("Door", "en-zh")["pinned"], 1)
        tm_manager.pin_entry(reverse["id"], False)
        self.assertEqual(self._row("门", "zh-en")["pinned"], 0)

        tm_manager.pin_entry(row["id"], False)
        tm_manager.delete_entry(row["id"])
        self.assertIsNone(self._row("门", "zh-en"))
        self.assertIsNotNone(self._row("Door", "en-zh"))

        tm_manager.insert_manual_entry("Beam", "梁", "en-zh", sync_reverse=True)
        protected_reverse = self._row("Beam", "en-zh")
        current = self._row("梁", "zh-en")
        assert protected_reverse is not None
        assert current is not None
        with closing(sqlite3.connect(str(tm_manager.DB_PATH))) as conn:
            conn.execute(
                "UPDATE tm_entries SET pinned = 1 WHERE id = ?",
                [protected_reverse["id"]],
            )
            conn.execute(
                "UPDATE tm_entries SET pinned = 0 WHERE id = ?",
                [current["id"]],
            )
            conn.commit()
        tm_manager.pin_entry(current["id"], False)
        tm_manager.delete_entry(current["id"])

        self.assertIsNone(self._row("梁", "zh-en"))
        self.assertEqual(self._row("Beam", "en-zh")["target_text"], "梁")

    def test_import_creates_reverse_entries_when_explicit(self) -> None:
        result = tm_manager.import_entries(
            [{"source_text": "钢筋", "target_text": "Rebar"}],
            "zh-en",
            "overwrite",
            sync_reverse=True,
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

    def test_custom_target_language_never_creates_reverse_entry(self) -> None:
        custom_code = build_custom_target_lang_code("Custom Target")
        lang_pair = f"en-{custom_code}"
        reverse_pair = f"{custom_code}-en"

        self.assertTrue(
            tm_manager.insert_manual_entry(
                "source term",
                "target term",
                lang_pair,
                sync_reverse=True,
            )
        )

        self.assertIsNone(tm_manager.lookup_batch(["target term"], reverse_pair)["target term"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
