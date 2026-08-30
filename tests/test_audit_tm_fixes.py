"""2026-08-29 代码审计 · TM 板块（A7-tm）缺陷的回归测试。

每条用例都钉住「用户能看见的行为」：库里到底写没写、界面拿到的是不是真实
结果、旧库的数据还读不读得出来，而不是某个内部函数被调用了几次。
读库一律走独立连接，避免把「返回值说成功」当成「盘上真的对」。
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from core import tm_cleaner, tm_manager
from core.tm_cleaner import (
    CleanSuggestion,
    apply_suggestions,
    apply_suggestions_detailed,
)
from core.tm_text import normalize_tm_text_for_compare, normalize_tm_text_for_storage


class IsolatedTmTestCase(unittest.TestCase):
    """把 TM 指到临时库，绝不碰开发者本机的数据。"""

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
        tm_manager.init_db()

    def rows(self, sql: str, params: list | None = None) -> list[sqlite3.Row]:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql, params or []).fetchall()
        finally:
            conn.close()

    def write(self, sql: str, params: list | None = None) -> int:
        """按旧版本的写法直接插库，用来伪造历史数据。"""
        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.execute(sql, params or [])
            conn.commit()
            return int(cursor.lastrowid or 0)
        finally:
            conn.close()

    def seed_legacy_entry(
        self,
        source: str,
        target: str,
        lang_pair: str = "en-zh",
        word_type: str = "auto",
        pinned: int = 0,
    ) -> int:
        """伪造一条旧库条目：哈希按「换行折成空格」的旧存储形态算。"""
        legacy_source = normalize_tm_text_for_compare(source)
        return self.write(
            "INSERT INTO tm_entries (source_text, source_hash, target_text, lang_pair, "
            "word_type, source_engine, pinned) VALUES (?, ?, ?, ?, ?, 'legacy', ?)",
            [
                legacy_source,
                tm_manager._make_hash(legacy_source, lang_pair),
                target,
                lang_pair,
                word_type,
                pinned,
            ],
        )


class LegacyWordTypeTests(IsolatedTmTestCase):
    """中-4：旧库 word_type 必须先归一化再判断，别把老数据整批排除。"""

    def test_legacy_term_entry_enters_deep_cleaning(self) -> None:
        self.seed_legacy_entry("old term", "旧词条", word_type="term")
        self.seed_legacy_entry("new term", "新词条", word_type="auto")

        sources = {item["source_text"] for item in tm_manager.get_all_entries_for_cleaning("en-zh")}

        self.assertIn("old term", sources)
        self.assertIn("new term", sources)

    def test_pinned_and_manual_entries_still_excluded_from_cleaning(self) -> None:
        self.seed_legacy_entry("pinned term", "固定", word_type="term", pinned=1)
        self.seed_legacy_entry("manual term", "人工", word_type="manual")

        sources = {item["source_text"] for item in tm_manager.get_all_entries_for_cleaning("en-zh")}

        self.assertNotIn("pinned term", sources)
        self.assertNotIn("manual term", sources)

    def test_legacy_import_rows_count_as_manual_in_stats(self) -> None:
        self.seed_legacy_entry("imported", "导入的", word_type="import")
        self.seed_legacy_entry("auto row", "自动的", word_type="auto")

        stats = tm_manager.get_stats("en-zh")

        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["manual"], 1)
        self.assertEqual(stats["auto"], 1)


class NewlinePreservationTests(IsolatedTmTestCase):
    """中-5：多行单元格的换行是内容，入库不能折成空格；旧库照样命中。"""

    def test_storage_keeps_newlines_while_compare_flattens(self) -> None:
        self.assertEqual(normalize_tm_text_for_storage("甲\r\n乙  丙\t丁"), "甲\n乙 丙 丁")
        self.assertEqual(normalize_tm_text_for_compare("甲\r\n乙  丙\t丁"), "甲 乙 丙 丁")

    def test_multiline_cell_replays_with_newlines(self) -> None:
        tm_manager.insert_batch([("line1\nline2", "第一行\n第二行")], "en-zh", 500, "test")

        stored = self.rows("SELECT source_text, target_text FROM tm_entries")[0]
        self.assertEqual(stored["source_text"], "line1\nline2")
        self.assertEqual(stored["target_text"], "第一行\n第二行")

        hit = tm_manager.lookup_batch(["line1\nline2"], "en-zh")
        self.assertEqual(hit["line1\nline2"], "第一行\n第二行")

    def test_legacy_flattened_entry_still_hits_for_multiline_query(self) -> None:
        # 旧库里存的是折成空格的单行形态，用户重跑同一份文档时查的是带换行的原文。
        self.seed_legacy_entry("line1\nline2", "旧译文")

        hit = tm_manager.lookup_batch(["line1\nline2"], "en-zh")

        self.assertEqual(hit["line1\nline2"], "旧译文")

    def test_multiline_write_does_not_duplicate_legacy_row(self) -> None:
        self.seed_legacy_entry("line1\nline2", "旧译文")

        tm_manager.insert_batch([("line1\nline2", "旧译文")], "en-zh", 500, "test")

        stored = self.rows("SELECT source_text, target_text FROM tm_entries")
        self.assertEqual(len(stored), 1, "换行写法不能和旧的单行写法各存一条")

    def test_same_text_with_newlines_upgrades_legacy_row_without_conflict(self) -> None:
        self.seed_legacy_entry("line1\nline2", "第一行 第二行")

        tm_manager.insert_batch([("line1\nline2", "第一行\n第二行")], "en-zh", 500, "test")

        stored = self.rows("SELECT target_text FROM tm_entries")
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["target_text"], "第一行\n第二行")
        self.assertEqual(
            self.rows("SELECT id FROM tm_conflict_candidates"),
            [],
            "只差换行写法不是译文冲突，不该登记待裁决候选",
        )


class BackupRestoreHashTests(IsolatedTmTestCase):
    """中-5 补漏：备份还原（preserve_status=True）写的哈希必须与全仓一个口径。"""

    def restore(self, entries: list[dict], *, mode: str = "overwrite") -> dict[str, int]:
        """走 /api/tm/full-restore 那条路：保留备份里的状态与时间戳。"""
        return tm_manager.import_entries(entries, "en-zh", mode, preserve_status=True)

    def test_restored_multiline_entry_uses_match_hash(self) -> None:
        self.restore(
            [
                {
                    "source_text": "line1\nline2",
                    "target_text": "第一行\n第二行",
                    "word_type": "auto",
                    "source_engine": "backup",
                }
            ]
        )

        stored = self.rows("SELECT source_text, source_hash FROM tm_entries")
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["source_text"], "line1\nline2")
        self.assertEqual(
            stored[0]["source_hash"],
            tm_manager._match_hash("line1\nline2", "en-zh"),
            "还原出来的行必须按匹配哈希登记，否则谁也查不到它",
        )

    def test_restored_multiline_entry_is_reachable_by_flattened_write(self) -> None:
        self.restore(
            [
                {
                    "source_text": "line1\nline2",
                    "target_text": "第一行\n第二行",
                    "word_type": "auto",
                    "source_engine": "backup",
                }
            ]
        )

        # 同一句原文的旧单行写法：必须命中还原出来的那行，不能再写一条。
        tm_manager.insert_batch([("line1 line2", "另一个译文")], "en-zh", 500, "test")

        stored = self.rows("SELECT source_text, target_text FROM tm_entries")
        self.assertEqual(len(stored), 1, "还原后的多行条目与旧单行写法不能各存一条")
        self.assertEqual(
            tm_manager.lookup_batch(["line1 line2"], "en-zh").get("line1 line2"),
            "第一行\n第二行",
        )

    def test_restore_onto_legacy_row_keeps_backup_metadata(self) -> None:
        # 旧库里是折成单行的写法，备份里是带换行的写法：还原时靠匹配哈希命中，
        # 元数据不能因为「原文逐字对不上」而静默丢失。
        self.seed_legacy_entry("line1\nline2", "旧译文")

        self.restore(
            [
                {
                    "source_text": "line1\nline2",
                    "target_text": "备份译文",
                    "word_type": "manual",
                    "source_engine": "backup",
                    "created_at": "2020-01-02 03:04:05",
                    "updated_at": "2020-01-02 03:04:06",
                }
            ]
        )

        stored = self.rows(
            "SELECT source_engine, created_at, target_text FROM tm_entries"
        )
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["target_text"], "备份译文")
        self.assertEqual(stored[0]["source_engine"], "backup")
        self.assertEqual(stored[0]["created_at"], "2020-01-02 03:04:05")


class ManualEntryResultTests(IsolatedTmTestCase):
    """中-6 / 低-TM：手工新增与编辑必须如实回报结果和真实原因。"""

    def test_blocked_by_pinned_entry_is_reported_not_silently_saved(self) -> None:
        tm_manager.insert_manual_entry("hello", "你好", "en-zh")
        entry_id = self.rows("SELECT id FROM tm_entries")[0]["id"]
        tm_manager.pin_entry(entry_id, True)

        result = tm_manager.insert_manual_entry_detailed("hello", "改成这个", "en-zh")

        self.assertFalse(result.changed)
        self.assertEqual(result.status, "blocked_pinned")
        self.assertTrue(result.message)
        self.assertEqual(self.rows("SELECT target_text FROM tm_entries")[0]["target_text"], "你好")

    def test_identical_entry_reports_unchanged(self) -> None:
        tm_manager.insert_manual_entry("hello", "你好", "en-zh")

        result = tm_manager.insert_manual_entry_detailed("hello", "你好", "en-zh")

        self.assertFalse(result.changed)
        self.assertEqual(result.status, "unchanged")

    def test_editing_pinned_entry_reports_pinned_not_source_conflict(self) -> None:
        tm_manager.insert_manual_entry("hello", "你好", "en-zh")
        entry_id = self.rows("SELECT id FROM tm_entries")[0]["id"]
        tm_manager.pin_entry(entry_id, True)

        result = tm_manager.update_entry_full_detailed(entry_id, "hello", "改译文")

        self.assertEqual(result.status, "pinned")
        self.assertIn("固定", result.message)
        self.assertNotIn("冲突", result.message)

    def test_editing_into_existing_source_still_reports_conflict(self) -> None:
        tm_manager.insert_manual_entry("hello", "你好", "en-zh")
        tm_manager.insert_manual_entry("world", "世界", "en-zh")
        target_id = self.rows("SELECT id FROM tm_entries WHERE source_text = 'world'")[0]["id"]

        result = tm_manager.update_entry_full_detailed(target_id, "hello", "别的译文")

        self.assertEqual(result.status, "conflict")


class CleaningSuggestionLifecycleTests(IsolatedTmTestCase):
    """高-7：建议确认写入后必须销账，旧建议不能一直挂在待审列表里。"""

    def _seed_suggestion(
        self,
        source: str = "hello",
        target: str = "你好",
        new_target: str = "更好的译文",
    ) -> dict:
        tm_manager.insert_batch([(source, target)], "en-zh", 500, "test")
        entry = tm_manager.get_all_entries_for_cleaning("en-zh")[0]
        tm_manager.persist_cleaning_suggestions(
            [
                {
                    "entry_id": entry["id"],
                    "source_text": entry["source_text"],
                    "old_target": entry["target_text"],
                    "new_target": new_target,
                    "lang_pair": "en-zh",
                    "version": entry["version"],
                }
            ]
        )
        return tm_manager.list_cleaning_suggestions("en-zh")[0]

    def test_applied_suggestion_leaves_pending_list(self) -> None:
        row = self._seed_suggestion()

        applied = apply_suggestions(
            [
                CleanSuggestion(
                    entry_id=int(row["entry_id"]),
                    source_text=str(row["source_text"]),
                    old_target=str(row["old_target"]),
                    new_target=str(row["new_target"]),
                    lang_pair="en-zh",
                    expected_version=str(row["expected_version"]),
                    suggestion_id=int(row["id"]),
                )
            ]
        )

        self.assertEqual(applied, 1)
        self.assertEqual(tm_manager.list_cleaning_suggestions("en-zh"), [])
        self.assertEqual(
            self.rows("SELECT status FROM tm_cleaning_suggestions")[0]["status"],
            "applied",
        )

    def test_suggestion_without_version_still_gets_concurrency_check(self) -> None:
        # 旧客户端只回传内容，服务端要按内容找回建议行、补上 expected_version，
        # 否则审阅期间被人工改过的译文会被这次确认无声覆盖。
        row = self._seed_suggestion()
        tm_manager.update_entry_full(int(row["entry_id"]), "hello", "人工改过的译文")

        applied = apply_suggestions(
            [
                CleanSuggestion(
                    entry_id=int(row["entry_id"]),
                    source_text=str(row["source_text"]),
                    old_target=str(row["old_target"]),
                    new_target=str(row["new_target"]),
                    lang_pair="en-zh",
                )
            ]
        )

        self.assertEqual(applied, 0)
        self.assertEqual(
            self.rows("SELECT target_text FROM tm_entries")[0]["target_text"],
            "人工改过的译文",
        )
        self.assertEqual(
            self.rows("SELECT status FROM tm_cleaning_suggestions")[0]["status"],
            "stale",
        )

    def test_expire_marks_suggestions_whose_entry_changed(self) -> None:
        row = self._seed_suggestion()
        tm_manager.update_entry_full(int(row["entry_id"]), "hello", "另一个译文")

        expired = tm_manager.expire_stale_cleaning_suggestions("en-zh")

        self.assertEqual(expired, 1)
        self.assertEqual(tm_manager.list_cleaning_suggestions("en-zh"), [])

    def test_expire_keeps_suggestions_that_still_match(self) -> None:
        self._seed_suggestion()

        self.assertEqual(tm_manager.expire_stale_cleaning_suggestions("en-zh"), 0)
        self.assertEqual(len(tm_manager.list_cleaning_suggestions("en-zh")), 1)

    def test_repeated_runs_do_not_stack_identical_suggestions(self) -> None:
        row = self._seed_suggestion()
        payload = [
            {
                "entry_id": int(row["entry_id"]),
                "source_text": str(row["source_text"]),
                "old_target": str(row["old_target"]),
                "new_target": str(row["new_target"]),
                "lang_pair": "en-zh",
                "version": str(row["expected_version"]),
            }
        ]

        created = tm_manager.persist_cleaning_suggestions(payload)

        self.assertEqual(created, 0)
        self.assertEqual(len(tm_manager.list_cleaning_suggestions("en-zh")), 1)

    def _apply(self, row: dict, *, with_version: bool = True) -> dict[str, int]:
        return apply_suggestions_detailed(
            [
                CleanSuggestion(
                    entry_id=int(row["entry_id"]),
                    source_text=str(row["source_text"]),
                    old_target=str(row["old_target"]),
                    new_target=str(row["new_target"]),
                    lang_pair="en-zh",
                    expected_version=str(row["expected_version"]) if with_version else "",
                    suggestion_id=int(row["id"]),
                )
            ]
        )

    def test_unchanged_is_reported_apart_from_skipped(self) -> None:
        # 建议的新译文与库里现有译文一模一样：这不是「被别人改过或被固定」，
        # 混进 skipped 会让用户以为自己的词条出了状况。
        row = self._seed_suggestion(new_target="你好")

        result = self._apply(row)

        self.assertEqual(
            {k: v for k, v in result.items() if k != "outcomes"},
            {"applied": 0, "unchanged": 1, "skipped": 0},
        )
        self.assertEqual(result["outcomes"], [{"suggestion_id": row["id"], "outcome": "unchanged"}])

    def test_real_skip_is_counted_as_skipped(self) -> None:
        row = self._seed_suggestion()
        tm_manager.update_entry_full(int(row["entry_id"]), "hello", "人工改过的译文")

        result = self._apply(row)

        self.assertEqual(
            {k: v for k, v in result.items() if k != "outcomes"},
            {"applied": 0, "unchanged": 0, "skipped": 1},
        )
        self.assertEqual(result["outcomes"], [{"suggestion_id": row["id"], "outcome": "stale"}])

    def test_written_suggestion_is_counted_as_applied(self) -> None:
        row = self._seed_suggestion()

        result = self._apply(row)

        self.assertEqual(
            {k: v for k, v in result.items() if k != "outcomes"},
            {"applied": 1, "unchanged": 0, "skipped": 0},
        )
        self.assertEqual(result["outcomes"], [{"suggestion_id": row["id"], "outcome": "updated"}])

    def test_rejected_suggestion_stays_pending(self) -> None:
        # 前端注释暗示 accepted=false 的建议原样留在待审列表，这里落实成回归钉子：
        # 不写入、不结算，建议表状态照旧 pending，行为对应「留到下次复核」的产品口径。
        row = self._seed_suggestion()

        result = apply_suggestions_detailed(
            [
                CleanSuggestion(
                    entry_id=int(row["entry_id"]),
                    source_text=str(row["source_text"]),
                    old_target=str(row["old_target"]),
                    new_target=str(row["new_target"]),
                    lang_pair="en-zh",
                    expected_version=str(row["expected_version"]),
                    suggestion_id=int(row["id"]),
                    accepted=False,
                )
            ]
        )

        self.assertEqual(result, {"applied": 0, "unchanged": 0, "skipped": 0, "outcomes": []})
        self.assertEqual(
            self.rows("SELECT status FROM tm_cleaning_suggestions")[0]["status"],
            "pending",
        )
        self.assertEqual(
            self.rows("SELECT target_text FROM tm_entries")[0]["target_text"],
            "你好",
        )
        self.assertEqual(len(tm_manager.list_cleaning_suggestions("en-zh")), 1)


class SuggestionOutcomePairingTests(IsolatedTmTestCase):
    """互审整改（2026-08-30）：逐条去向必须按提交行序配对，不能按 entry_id 归并。

    同一词条可以挂多条 pending 建议（不同新译文），写入时 entry_id 会同时落进
    多个桶（先到的 updated、后到的被乐观并发拦成 stale）。按 entry_id 反查会把
    没写入的那条也标成「已写入」，结算时两条建议还会一起被标错状态。
    附带钉住：pinned/missing 的逐条去向、译文为空（invalid）不落库且保持
    pending、失效汇总只数「当前待审窗口」内的失效行。
    """

    def _seed_entry(self, source: str = "hello", target: str = "你好") -> dict:
        tm_manager.insert_batch([(source, target)], "en-zh", 500, "test")
        return tm_manager.get_all_entries_for_cleaning("en-zh")[-1]

    def _persist(self, entry: dict, new_target: str) -> dict:
        tm_manager.persist_cleaning_suggestions(
            [
                {
                    "entry_id": entry["id"],
                    "source_text": entry["source_text"],
                    "old_target": entry["target_text"],
                    "new_target": new_target,
                    "lang_pair": "en-zh",
                    "version": entry["version"],
                }
            ]
        )
        return [
            row
            for row in tm_manager.list_cleaning_suggestions("en-zh")
            if str(row["new_target"]) == new_target
        ][0]

    def _suggestion(self, row: dict, *, new_target: str | None = None) -> CleanSuggestion:
        return CleanSuggestion(
            entry_id=int(row["entry_id"]),
            source_text=str(row["source_text"]),
            old_target=str(row["old_target"]),
            new_target=str(row["new_target"]) if new_target is None else new_target,
            lang_pair="en-zh",
            expected_version=str(row["expected_version"]),
            suggestion_id=int(row["id"]),
        )

    def test_duplicate_entry_suggestions_get_row_level_outcomes(self) -> None:
        # 同一词条两条建议一起勾选：先到的写入，后到的因为版本已变被拦下。
        # 逐条去向必须一条 updated、一条 stale——不能两条都报「已写入」。
        entry = self._seed_entry()
        row_a = self._persist(entry, "译文甲")
        row_b = self._persist(entry, "译文乙")

        result = apply_suggestions_detailed(
            [self._suggestion(row_a), self._suggestion(row_b)]
        )

        self.assertEqual(
            {k: v for k, v in result.items() if k != "outcomes"},
            {"applied": 1, "unchanged": 0, "skipped": 1},
        )
        self.assertEqual(
            result["outcomes"],
            [
                {"suggestion_id": row_a["id"], "outcome": "updated"},
                {"suggestion_id": row_b["id"], "outcome": "stale"},
            ],
        )
        self.assertEqual(
            self.rows("SELECT target_text FROM tm_entries")[0]["target_text"],
            "译文甲",
        )
        statuses = {
            row["id"]: row["status"]
            for row in self.rows("SELECT id, status FROM tm_cleaning_suggestions")
        }
        self.assertEqual(statuses[row_a["id"]], "applied")
        self.assertEqual(statuses[row_b["id"]], "stale")

    def test_pinned_entry_reports_pinned_outcome(self) -> None:
        entry = self._seed_entry()
        row = self._persist(entry, "更好的译文")
        tm_manager.pin_entry(int(entry["id"]), True)

        result = apply_suggestions_detailed([self._suggestion(row)])

        self.assertEqual(
            {k: v for k, v in result.items() if k != "outcomes"},
            {"applied": 0, "unchanged": 0, "skipped": 1},
        )
        self.assertEqual(
            result["outcomes"], [{"suggestion_id": row["id"], "outcome": "pinned"}]
        )
        self.assertEqual(
            self.rows("SELECT status FROM tm_cleaning_suggestions")[0]["status"],
            "stale",
        )

    def test_deleted_entry_reports_missing_outcome(self) -> None:
        entry = self._seed_entry()
        row = self._persist(entry, "更好的译文")
        tm_manager.delete_entry(int(entry["id"]))

        result = apply_suggestions_detailed([self._suggestion(row)])

        self.assertEqual(
            {k: v for k, v in result.items() if k != "outcomes"},
            {"applied": 0, "unchanged": 0, "skipped": 1},
        )
        self.assertEqual(
            result["outcomes"], [{"suggestion_id": row["id"], "outcome": "missing"}]
        )
        self.assertEqual(
            self.rows("SELECT status FROM tm_cleaning_suggestions")[0]["status"],
            "stale",
        )

    def test_empty_target_reports_invalid_and_stays_pending(self) -> None:
        # 用户在面板里把译文改成空白再写入：什么都不该发生——不落库、
        # 逐条去向如实报 invalid、建议保持 pending 留到下次复核。
        entry = self._seed_entry()
        row = self._persist(entry, "更好的译文")

        result = apply_suggestions_detailed(
            [self._suggestion(row, new_target="   ")]
        )

        self.assertEqual(
            {k: v for k, v in result.items() if k != "outcomes"},
            {"applied": 0, "unchanged": 0, "skipped": 1},
        )
        self.assertEqual(
            result["outcomes"], [{"suggestion_id": row["id"], "outcome": "invalid"}]
        )
        self.assertEqual(
            self.rows("SELECT status FROM tm_cleaning_suggestions")[0]["status"],
            "pending",
        )
        self.assertEqual(
            self.rows("SELECT target_text FROM tm_entries")[0]["target_text"],
            "你好",
        )

    def test_stale_count_only_covers_current_review_window(self) -> None:
        # 失效汇总不数陈年旧账：只有「现存最早 pending 建议之后」失效的行才计入；
        # pending 清零时计数归零，面板不会永久顶着一句失效提示。
        entry_a = self._seed_entry("old", "旧词条")
        row_a = self._persist(entry_a, "旧建议")
        tm_manager.mark_cleaning_suggestions([int(row_a["id"])], "stale")
        # 伪造成很久以前失效的历史行。
        self.write(
            "UPDATE tm_cleaning_suggestions SET updated_at = '2000-01-01 00:00:00', "
            "created_at = '2000-01-01 00:00:00' WHERE id = ?",
            [int(row_a["id"])],
        )

        # 没有任何 pending：窗口不存在，历史失效行不计。
        self.assertEqual(tm_manager.count_stale_suggestions_in_review_window("en-zh"), 0)

        entry_b = self._seed_entry("fresh", "新词条")
        self._persist(entry_b, "新建议")
        # 有 pending 了，但历史失效行仍在窗口之外。
        self.assertEqual(tm_manager.count_stale_suggestions_in_review_window("en-zh"), 0)

        entry_c = self._seed_entry("live", "在审词条")
        row_c = self._persist(entry_c, "在审建议")
        tm_manager.mark_cleaning_suggestions([int(row_c["id"])], "stale")
        # 待审期间新失效的行要计入。
        self.assertEqual(tm_manager.count_stale_suggestions_in_review_window("en-zh"), 1)


class OuterNoiseStrippingTests(unittest.TestCase):
    """中-7：成对引号之间的正文不是外层噪声，绝不能被剥掉。"""

    def test_paired_quotes_inside_text_are_preserved(self) -> None:
        for text in (
            "「甲」 与 「乙」",
            '"甲" 与 "乙"',
            "**甲** 与 **乙**",
            "《书》与《报》",
            "“甲”和“乙”",
        ):
            with self.subTest(text=text):
                self.assertEqual(tm_cleaner._normalize_clean_target(text), text)

    def test_true_outer_wrapper_is_still_stripped(self) -> None:
        self.assertEqual(tm_cleaner._normalize_clean_target("「整句都在引号里」"), "整句都在引号里")
        self.assertEqual(tm_cleaner._normalize_clean_target("**整句加粗**"), "整句加粗")
        self.assertEqual(tm_cleaner._normalize_clean_target('"整句都在引号里"'), "整句都在引号里")


class BulkPinChunkingTests(IsolatedTmTestCase):
    """低-TM：整库固定/解固不能被 SQLite 的绑定变量上限打断。

    开发机上的 SQLite 变量上限有 25 万，装机版跑的旧运行时只有 999，
    本机跑不出用户遇到的 "too many SQL variables"。这里把连接的上限压到
    999，用发布环境的口径复现：一句 IN (...) 下发 1200 个 id 必然抛错。
    """

    def setUp(self) -> None:
        super().setUp()
        original_get_conn = tm_manager._get_conn

        @contextmanager
        def limited_conn():
            with original_get_conn() as conn:
                conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
                yield conn

        patcher = patch.object(tm_manager, "_get_conn", limited_conn)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _seed(self, count: int) -> list[int]:
        pairs = [(f"src {index}", f"译文 {index}") for index in range(count)]
        tm_manager.insert_batch(pairs, "en-zh", 500, "test")
        return [int(row["id"]) for row in self.rows("SELECT id FROM tm_entries ORDER BY id")]

    def test_bulk_pin_beyond_sqlite_variable_limit(self) -> None:
        ids = self._seed(1200)
        self.assertEqual(len(ids), 1200)

        tm_manager.bulk_pin_entries(ids, True)

        pinned = self.rows("SELECT COUNT(*) AS c FROM tm_entries WHERE pinned = 1")[0]["c"]
        self.assertEqual(pinned, 1200)

    def test_set_all_pinned_beyond_sqlite_variable_limit(self) -> None:
        self._seed(1200)

        changed = tm_manager.set_all_pinned("en-zh", True)

        self.assertEqual(changed, 1200)
        pinned = self.rows("SELECT COUNT(*) AS c FROM tm_entries WHERE pinned = 1")[0]["c"]
        self.assertEqual(pinned, 1200)

    def test_mark_suggestions_beyond_sqlite_variable_limit(self) -> None:
        ids = self._seed(1200)
        tm_manager.persist_cleaning_suggestions(
            [
                {
                    "entry_id": entry_id,
                    "source_text": f"src {index}",
                    "old_target": f"译文 {index}",
                    "new_target": f"更好的译文 {index}",
                    "lang_pair": "en-zh",
                    "version": "v1",
                }
                for index, entry_id in enumerate(ids)
            ]
        )
        suggestion_ids = [int(row["id"]) for row in tm_manager.list_cleaning_suggestions("en-zh")]
        self.assertEqual(len(suggestion_ids), 1200)

        marked = tm_manager.mark_cleaning_suggestions(suggestion_ids, "applied")

        self.assertEqual(marked, 1200)


class ApplyPayloadContractTests(unittest.TestCase):
    """高-7 前端契约：确认写入的报文必须带上建议主键与乐观并发版本。"""

    def test_payload_model_carries_suggestion_id_and_version(self) -> None:
        from api.app import TmSuggestionPayload

        payload = TmSuggestionPayload(
            entry_id=7,
            new_target="更好的译文",
            suggestion_id=3,
            lang_pair="en-zh",
            expected_version="7|abc|旧译文|2026-08-29",
        )

        self.assertEqual(payload.suggestion_id, 3)
        self.assertEqual(payload.expected_version, "7|abc|旧译文|2026-08-29")

    def test_library_view_sends_suggestion_id_and_version(self) -> None:
        source = Path(__file__).resolve().parents[1] / "ui" / "src" / "views" / "library.ts"
        text = source.read_text(encoding="utf-8")
        apply_call = text.split("/api/tm/clean/apply")[0]

        self.assertIn("suggestion_id: num(suggestion.id)", apply_call)
        self.assertIn("expected_version: text(suggestion.expected_version)", apply_call)

    def test_library_view_reports_unchanged_separately(self) -> None:
        source = Path(__file__).resolve().parents[1] / "ui" / "src" / "views" / "library.ts"
        text = source.read_text(encoding="utf-8")
        after_apply = text.split("/api/tm/clean/apply")[1]

        self.assertIn("与库中译文相同", after_apply)
        self.assertIn("result.skipped", after_apply)


if __name__ == "__main__":
    unittest.main()
