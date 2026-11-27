"""记忆库搜索按字面找：「%」「_」是用户要找的字，不是通配符。

以前搜索词直接拼进 LIKE：搜「%」搜出全部词条，搜「0%」搜出「50」。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import tm_manager


class TmSearchLiteralTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for patcher in (
            patch.object(tm_manager, "DB_PATH", root / "tm.db"),
            patch.object(tm_manager, "BACKUPS_DIR", root / "backups"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        tm_manager.init_db()
        for source, target in (
            ("100%", "100 %"),
            ("a_b", "A-B"),
            ("50", "fifty"),
            ("axb", "A times B"),
            ("C:\\图纸", "C:\\drawings"),
            ("普通词条", "ordinary"),
        ):
            result = tm_manager.insert_manual_entry_detailed(source, target, "zh-en")
            self.assertEqual(result.status, "written", result.message)

    def _sources(self, keyword: str) -> set[str]:
        rows, total = tm_manager.search_entries("zh-en", keyword)
        self.assertEqual(total, len(rows))
        return {str(row["source_text"]) for row in rows}

    def test_percent_and_underscore_are_searched_literally(self) -> None:
        self.assertEqual(self._sources("%"), {"100%"})
        self.assertEqual(self._sources("0%"), {"100%"})
        self.assertEqual(self._sources("_"), {"a_b"})
        self.assertEqual(self._sources("a_b"), {"a_b"})
        self.assertEqual(self._sources("\\"), {"C:\\图纸"})
        self.assertEqual(self._sources("普通"), {"普通词条"})
        self.assertEqual(len(self._sources("")), 6)

    def test_counts_next_to_the_list_use_the_same_match(self) -> None:
        self.assertEqual(tm_manager.get_filter_counts("zh-en", "%")["all"], 1)
        pins = tm_manager.get_pin_count("zh-en", "_")
        self.assertEqual(pins["pinned"] + pins["unpinned"], 1)

    def test_keyword_scoped_bulk_actions_touch_only_the_matches(self) -> None:
        self.assertEqual(tm_manager.set_all_pinned("zh-en", True, "_"), 1)
        self.assertEqual(tm_manager.delete_unpinned_entries("zh-en", "%"), 1)
        self.assertEqual(self._sources(""), {"a_b", "50", "axb", "C:\\图纸", "普通词条"})


if __name__ == "__main__":
    unittest.main()
