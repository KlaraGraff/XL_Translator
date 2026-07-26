"""A lost SELECT-then-INSERT race must not fail a whole TM batch.

Two tasks can commit the same new (source_text, lang_pair) concurrently; the
loser's INSERT hits the UNIQUE constraint. That used to abort and roll back
the loser's entire batch after every translation had already succeeded.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import tm_manager


class TmConcurrentUpsertTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.old_db = tm_manager.DB_PATH
        tm_manager.DB_PATH = root / "tm.db"
        tm_manager.init_db()

    def tearDown(self) -> None:
        tm_manager.DB_PATH = self.old_db
        self.temp_dir.cleanup()

    def test_a_concurrently_inserted_entry_reroutes_through_conflict_rules(self) -> None:
        # The "winner" commits first from another task's connection.
        tm_manager.insert_manual_entry("race", "先到", "en-zh")

        real_fetch = tm_manager._fetch_entry_by_source
        calls = {"count": 0}

        def _fetch_missing_once(conn, source_text, lang_pair):
            calls["count"] += 1
            if calls["count"] == 1:
                # Simulate the race: the winner's commit lands after our
                # existence check but before our INSERT.
                return None
            return real_fetch(conn, source_text, lang_pair)

        with patch.object(
            tm_manager, "_fetch_entry_by_source", side_effect=_fetch_missing_once
        ):
            with tm_manager._get_conn() as conn:
                wrote = tm_manager._upsert_entry(
                    conn,
                    "race",
                    "后到",
                    "en-zh",
                    word_type=tm_manager.AUTO_WORD_TYPE,
                )

        # The loser must neither raise nor overwrite: the automatic result is
        # rerouted through the normal priority rules against the winner.
        self.assertFalse(wrote)
        with tm_manager._get_conn() as conn:
            row = tm_manager._fetch_entry_by_source(conn, "race", "en-zh")
        self.assertEqual(row["target_text"], "先到")


if __name__ == "__main__":
    unittest.main(verbosity=2)
