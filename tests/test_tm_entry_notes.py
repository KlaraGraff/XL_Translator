"""记忆库词条的备注、采用方式、使用次数，以及旧库升级到「同一原文多条」布局。"""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from core import tm_manager

# 升级前（V10）的表：同一原文在同一语言对只能有一条。
_V10_DDL = """
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
"""


class TmEntryNotesTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db_path = self.root / "tm.db"
        self.backups_dir = self.root / "backups"
        for patcher in (
            patch.object(tm_manager, "DB_PATH", self.db_path),
            patch.object(tm_manager, "BACKUPS_DIR", self.backups_dir),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def make_v10_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(_V10_DDL)
            conn.execute(
                "INSERT INTO tm_entries (id, source_text, target_text, lang_pair, word_type, pinned) "
                "VALUES (7, '甲', 'Partie A', 'zh-fr', 'manual', 1)"
            )
            conn.execute(
                "INSERT INTO tm_entries (id, source_text, target_text, lang_pair, word_type) "
                "VALUES (9, '卫生间', 'toilettes', 'zh-fr', 'auto')"
            )

    def test_v10_database_is_backed_up_then_rebuilt_keeping_every_row(self):
        self.make_v10_db()
        tm_manager.init_db()
        backups = [path.name for path in self.backups_dir.rglob("tm_before_layout2_*") if path.is_file()]
        self.assertTrue(backups, "rebuilding the table must leave a copy first")
        self.assertTrue(tm_manager.entries_layout_supports_notes())
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT id, source_text, target_text, pinned, note FROM tm_entries ORDER BY id").fetchall()
            indexes = {row[1] for row in conn.execute("PRAGMA index_list(tm_entries)")}
        self.assertEqual(rows, [(7, "甲", "Partie A", 1, ""), (9, "卫生间", "toilettes", 0, "")])
        # 旧版应用打开新库时 CREATE UNIQUE INDEX IF NOT EXISTS idx_hash_lang 是空操作。
        self.assertIn("idx_hash_lang", indexes)
        self.assertIn("idx_hash_lang_note", indexes)
        # 再次启动不再备份。
        before = len(list(self.backups_dir.rglob("*")))
        tm_manager.init_db()
        self.assertEqual(len(list(self.backups_dir.rglob("*"))), before)

    def test_concurrent_first_open_upgrades_once_without_errors(self):
        # 记忆库页面打开时并发的几个请求都会先 init_db()：旧库只能升级一次，谁也不能报错。
        self.make_v10_db()
        barrier = threading.Barrier(5)
        errors: list[str] = []

        def open_page() -> None:
            try:
                barrier.wait()
                tm_manager.init_db()
                tm_manager.search_entries("zh-fr")
            except Exception as exc:  # noqa: BLE001 - collected for the assertion
                errors.append(repr(exc))

        threads = [threading.Thread(target=open_page) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertTrue(tm_manager.entries_layout_supports_notes())
        backups = [path for path in self.backups_dir.rglob("tm_before_layout2_*.db") if path.is_file()]
        self.assertEqual(len(backups), 1)

    def test_failed_backup_leaves_the_old_layout_working(self):
        self.make_v10_db()
        with patch.object(tm_manager, "_backup_db", side_effect=OSError("disk full")):
            tm_manager.init_db()
        self.assertFalse(tm_manager.entries_layout_supports_notes())
        self.assertEqual(tm_manager.lookup_batch(["卫生间"], "zh-fr"), {"卫生间": "toilettes"})
        result = tm_manager.insert_manual_entry_detailed("甲", "A", "zh-fr", note="配电回路编号")
        self.assertEqual(result.status, "layout_pending")
        self.assertTrue(tm_manager.insert_manual_entry_detailed("楼梯", "escalier", "zh-fr"))

    def test_noted_variants_live_beside_the_general_entry(self):
        tm_manager.init_db()
        self.assertTrue(tm_manager.insert_manual_entry_detailed("甲", "Partie A", "zh-fr", note="合同甲方"))
        self.assertTrue(tm_manager.insert_manual_entry_detailed("甲", "A", "zh-fr", note="配电回路编号"))
        self.assertTrue(tm_manager.insert_manual_entry_detailed("门", "porte", "zh-fr"))
        # 整段直接贴回只认没有备注的通用条目（CAD、PDF 仍走这条路）。
        self.assertEqual(tm_manager.lookup_batch(["甲", "门"], "zh-fr"), {"甲": None, "门": "porte"})
        rows = {(row["source_text"], row["note"]): row for row in tm_manager.list_entries_for_target("fr")}
        self.assertEqual(rows[("甲", "合同甲方")]["effective_usage"], "scene")
        self.assertEqual(rows[("门", "")]["effective_usage"], "always")
        self.assertEqual(rows[("门", "")]["origin"], "manual")
        counts = tm_manager.get_filter_counts("zh-fr")
        self.assertEqual((counts["all"], counts["manual"], counts["scene"], counts["always"]), (3, 3, 2, 1))
        found, total = tm_manager.search_entries("zh-fr", "合同", entry_filter="scene")
        self.assertEqual((total, found[0]["target_text"]), (1, "Partie A"))

    def test_usage_choice_and_pinned_entries_can_be_edited_from_the_page(self):
        tm_manager.init_db()
        tm_manager.insert_manual_entry_detailed("预埋件", "pièce noyée", "zh-fr")
        entry = tm_manager.search_entries("zh-fr")[0][0]
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE tm_entries SET pinned = 1 WHERE id = ?", [entry["id"]])
        refused = tm_manager.update_entry_full_detailed(entry["id"], "预埋件", "pièce à sceller")
        self.assertFalse(refused)
        updated = tm_manager.update_entry_full_detailed(
            entry["id"], "预埋件", "pièce à sceller", usage="segment", scope="公建", allow_pinned=True
        )
        self.assertTrue(updated, updated.message)
        row = tm_manager.search_entries("zh-fr")[0][0]
        self.assertEqual((row["target_text"], row["effective_usage"], row["scope"], row["pinned"]), ("pièce à sceller", "segment", "公建", 1))
        self.assertFalse(tm_manager.delete_entry(row["id"]))
        self.assertTrue(tm_manager.delete_entry(row["id"], allow_pinned=True))

    def test_scene_entries_are_saved_once_per_new_translation(self):
        tm_manager.init_db()
        tm_manager.insert_batch([("甲", "Partie A")], "zh-fr", 200)
        self.assertEqual(tm_manager.insert_scene_entry("甲", "Partie A", "zh-fr", "合同"), 0)
        self.assertEqual(tm_manager.insert_scene_entry("甲", "classe A", "zh-fr", ""), 0)
        new_id = tm_manager.insert_scene_entry("甲", "classe A", "zh-fr", "防火门耐火等级")
        self.assertGreater(new_id, 0)
        self.assertEqual(tm_manager.insert_scene_entry("甲", "classe A", "zh-fr", "防火门耐火等级"), 0)
        row = next(r for r in tm_manager.list_entries_for_target("fr") if r["id"] == new_id)
        self.assertEqual((row["origin"], row["effective_usage"]), ("auto", "scene"))

    def test_task_usage_updates_counts_and_month_totals(self):
        tm_manager.init_db()
        tm_manager.insert_manual_entry_detailed("门", "porte", "zh-fr")
        entry_id = tm_manager.search_entries("zh-fr")[0][0]["id"]
        tm_manager.record_task_usage({entry_id: 3}, not_applied_by_pair={"zh-fr": 1})
        tm_manager.record_task_usage({entry_id: 2})
        row = tm_manager.search_entries("zh-fr")[0][0]
        self.assertEqual(row["use_count"], 5)
        self.assertTrue(row["last_used_at"])
        self.assertEqual(tm_manager.get_month_usage("zh-fr"), {"month_uses": 5, "month_not_applied": 1})
        stats = tm_manager.get_stats("zh-fr")
        self.assertEqual((stats["month_uses"], stats["origin_manual"]), (5, 1))


    def test_task_report_counts_manual_entries_that_were_used(self):
        # 普通模式那一行「本次涉及的 N 条手动维护词条均已采用」：句中照用的和按场景选中的都算。
        from types import SimpleNamespace

        from core.term_session import TaskTermSession
        from engines.base_engine import TranslationEngine

        class Engine(TranslationEngine):
            engine_name = "test/mock"

            def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
                answers = {"门框应与预埋件可靠连接": "Le cadre est fixé à la pièce noyée", "甲": "Partie A"}
                return {text: answers[text] for text in texts}

        tm_manager.init_db()
        tm_manager.insert_manual_entry_detailed("预埋件", "pièce noyée", "zh-fr")
        tm_manager.insert_manual_entry_detailed("甲", "Partie A", "zh-fr", note="合同甲方")
        tm_manager.insert_manual_entry_detailed("甲", "A", "zh-fr", note="配电回路编号")
        tm_settings = SimpleNamespace(apply_in_sentences=True, term_retry_attempts=2, term_batch_budget=20)
        session = TaskTermSession(tm_settings, target_lang="fr", smart_mode=True)
        texts = ["门框应与预埋件可靠连接", "甲"]
        hits = session.resolve({text: {"zh-fr"} for text in texts}, profile="施工合同附件")
        self.assertEqual(hits, {})
        session.wrap(Engine()).translate_batch(texts, "fr", "system", "zh")
        report = session.finish(pair_for_text=lambda _text: "zh-fr")
        self.assertEqual(report["not_applied"], [])
        self.assertEqual(report["manual_terms_hit"], 2)

    def test_changed_manual_term_is_not_bypassed_by_an_old_whole_sentence(self):
        from types import SimpleNamespace

        from core.term_session import TaskTermSession

        tm_manager.init_db()
        tm_manager.insert_batch([("门框应与预埋件可靠连接", "Le cadre est fixé à l'embedded part")], "zh-fr", 200)
        tm_manager.insert_batch([("卫生间地面做防水", "Sol des toilettes étanché")], "zh-fr", 200)
        tm_manager.insert_manual_entry_detailed("预埋件", "pièce noyée", "zh-fr")
        session = TaskTermSession(SimpleNamespace(apply_in_sentences=True), target_lang="fr", smart_mode=True)
        texts = ["门框应与预埋件可靠连接", "卫生间地面做防水"]
        hits = session.resolve({text: {"zh-fr"} for text in texts}, profile="")
        # 旧句子里还是改之前的译法：不再整段贴回，交给模型带着词条重译。不含词条的照常复用。
        self.assertEqual(hits, {"卫生间地面做防水": "Sol des toilettes étanché"})
        # 送模型时旧整句不再当候选，按句中的「预埋件」注入和核对。
        [hit] = session.index.scan("门框应与预埋件可靠连接")
        self.assertEqual((hit.source, hit.usage, hit.expected, hit.whole), ("预埋件", "always", "pièce noyée", False))

        from engines.base_engine import TranslationEngine

        prompts: list[str] = []

        class Engine(TranslationEngine):
            engine_name = "test/mock"

            def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
                prompts.append(system_prompt)
                fixed = "以下各条的上次译文未采用设定译法" in system_prompt
                return {text: "Le cadre est fixé à la pièce noyée" if fixed else "Le cadre est fixé à l'embedded part" for text in texts}

        session.wrap(Engine()).translate_batch(["门框应与预埋件可靠连接"], "fr", "system", "zh")
        self.assertNotIn("embedded part", prompts[0])
        self.assertIn("预埋件 → pièce noyée", prompts[0])
        report = session.finish(pair_for_text=lambda _text: "zh-fr")
        self.assertEqual((report["applied_after_retry"], report["scene_choices"], report["manual_terms_hit"]), (1, [], 1))

        # 重译结果入库：旧整句被新译文顶替，不留冲突候选；下次直接整段复用，不再重译。
        session.retire_stale_entries()
        tm_manager.insert_batch([("门框应与预埋件可靠连接", "Le cadre est fixé à la pièce noyée")], "zh-fr", 200)
        self.assertEqual(tm_manager.list_conflict_candidates(), [])
        again = TaskTermSession(SimpleNamespace(apply_in_sentences=True), target_lang="fr", smart_mode=True)
        hits = again.resolve({text: {"zh-fr"} for text in texts}, profile="")
        self.assertEqual(hits["门框应与预埋件可靠连接"], "Le cadre est fixé à la pièce noyée")

    def test_stale_retire_leaves_manual_and_changed_rows_alone(self):
        tm_manager.init_db()
        tm_manager.insert_batch([("旧句一", "old one"), ("旧句二", "old two")], "zh-fr", 200)
        tm_manager.insert_manual_entry_detailed("旧句三", "mine", "zh-fr")
        rows = {row["source_text"]: row for row in tm_manager.get_entries_by_ids(range(1, 10))}
        retired = tm_manager.retire_stale_auto_entries(
            {
                int(rows["旧句一"]["id"]): "old one",
                int(rows["旧句二"]["id"]): "changed since",  # 期间被改过：不删
                int(rows["旧句三"]["id"]): "mine",  # 手动维护：不删
            }
        )
        self.assertEqual(retired, 1)
        left = {row["source_text"] for row in tm_manager.get_entries_by_ids(range(1, 10))}
        self.assertEqual(left, {"旧句二", "旧句三"})

    def test_auto_scene_entry_is_only_offered_where_its_note_fits_the_document(self):
        from types import SimpleNamespace

        from core.term_session import TaskTermSession

        tm_manager.init_db()
        self.assertGreater(tm_manager.insert_scene_entry("甲", "Classe A", "zh-fr", "防火门耐火等级 A 级"), 0)
        tm_settings = SimpleNamespace(apply_in_sentences=True)
        contract = TaskTermSession(tm_settings, target_lang="fr", smart_mode=True)
        contract.resolve({"甲": {"zh-fr"}}, profile="施工合同及配电系统说明，甲方为业主，乙方为承包商。")
        self.assertNotIn("甲", contract.whole.scene_texts)
        self.assertEqual(contract.index.entries, [])
        fire = TaskTermSession(tm_settings, target_lang="fr", smart_mode=True)
        fire.resolve({"甲": {"zh-fr"}}, profile="建筑消防设计说明，甲/乙/丙指防火门和防火分区的耐火等级。")
        self.assertIn("甲", fire.whole.scene_texts)

    def test_api_round_trip_with_note_usage_filter_and_pinned_edit(self):
        from fastapi.testclient import TestClient

        import settings as settings_module
        from api.app import create_app

        with (
            patch.object(settings_module, "APP_DATA_DIR", self.root),
            patch.object(settings_module, "SETTINGS_PATH", self.root / "settings.json"),
            patch.object(settings_module, "KEYS_PATH", self.root / "keys.json"),
            patch.object(settings_module, "RECOVERY_PATH", self.root / "recovery.json"),
            patch.object(settings_module, "BACKUPS_DIR", self.backups_dir),
        ):
            client = TestClient(create_app())
            created = client.post(
                "/api/tm/entries",
                json={"source_text": "甲", "target_text": "A", "lang_pair": "zh-fr", "note": "配电回路编号"},
            )
            self.assertEqual(created.status_code, 201, created.text)
            client.post("/api/tm/entries", json={"source_text": "门", "target_text": "porte", "lang_pair": "zh-fr"})
            listed = client.get("/api/tm/entries", params={"lang_pair": "zh-fr", "entry_filter": "scene"}).json()
            self.assertEqual([row["source_text"] for row in listed["entries"]], ["甲"])
            self.assertEqual(listed["filter_counts"]["all"], 2)
            entry = listed["entries"][0]
            self.assertEqual((entry["note"], entry["effective_usage"], entry["origin"]), ("配电回路编号", "scene", "manual"))
            client.post(f"/api/tm/entries/{entry['id']}/pin", json={"pinned": True})
            updated = client.put(
                f"/api/tm/entries/{entry['id']}",
                json={"source_text": "甲", "target_text": "A", "usage": "always", "allow_pinned": True},
            )
            self.assertEqual(updated.status_code, 200, updated.text)
            self.assertEqual(client.delete(f"/api/tm/entries/{entry['id']}").status_code, 409)
            self.assertEqual(
                client.delete(f"/api/tm/entries/{entry['id']}", params={"allow_pinned": True}).status_code, 200
            )


if __name__ == "__main__":
    unittest.main()
