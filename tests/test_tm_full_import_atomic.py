"""整库导入要么全部写进去，要么一条不写（#281 第 2 条）。

以前每个语言方向各提交一次：写到第二组出错时第一组已经落库，自定义语言也已经
存进设置，界面却报「导入失败」；用户再导一次，前一组全成了「重复」。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from api.app import create_app
from core import tm_manager
from core.language_registry import CustomTargetLang, build_custom_target_lang_code
from settings import AppSettings


class FullImportAtomicTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        old_db, old_backups = tm_manager.DB_PATH, tm_manager.BACKUPS_DIR
        tm_manager.DB_PATH = root / "tm.db"
        tm_manager.BACKUPS_DIR = root / "backups"
        self.addCleanup(setattr, tm_manager, "DB_PATH", old_db)
        self.addCleanup(setattr, tm_manager, "BACKUPS_DIR", old_backups)
        tm_manager.init_db()

    def _backup(self) -> dict:
        custom = CustomTargetLang(
            name="Project dialect",
            description="Project terminology",
            code=build_custom_target_lang_code("Project dialect"),
        )
        return {
            "format_version": "tm-full-v1",
            "custom_target_langs": [custom.model_dump()],
            "entries": [
                {"source_text": "first", "target_text": "第一", "lang_pair": "en-zh"},
                {"source_text": "second", "target_text": "Zweite", "lang_pair": "en-de"},
                {"source_text": "third", "target_text": "dialect", "lang_pair": f"en-{custom.code}"},
            ],
            "conflict_candidates": [],
        }

    def _count(self) -> int:
        with tm_manager._get_conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM tm_entries").fetchone()[0]

    def test_a_failure_halfway_leaves_neither_entries_nor_custom_languages(self) -> None:
        settings = AppSettings()
        saved: list[list] = []

        def _save(value: AppSettings) -> None:
            saved.append(list(value.custom_target_langs))

        real_import = tm_manager.import_entries
        calls = {"n": 0}

        def _fail_on_second_pair(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("disk full")
            return real_import(*args, **kwargs)

        with patch("api.routes.tm.load_settings", return_value=settings), patch(
            "api.routes.tm.save_settings", side_effect=_save
        ), patch("api.routes.tm.tm_manager.import_entries", side_effect=_fail_on_second_pair):
            client = TestClient(create_app(), raise_server_exceptions=False)
            response = client.post("/api/tm/import/full", json=self._backup())

        self.assertNotEqual(response.status_code, 200)
        self.assertEqual(calls["n"], 2)
        # 第一组已经「写过」，但跟着整体回滚了。
        self.assertEqual(self._count(), 0)
        # 自定义语言先存进去、出错后又换回导入前的空列表。
        self.assertEqual(len(saved[0]), 1)
        self.assertEqual(saved[-1], [])

    def test_a_successful_import_still_writes_everything(self) -> None:
        settings = AppSettings()
        with patch("api.routes.tm.load_settings", return_value=settings), patch(
            "api.routes.tm.save_settings"
        ):
            client = TestClient(create_app())
            response = client.post("/api/tm/import/full", json=self._backup())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["inserted"], 3)
        self.assertEqual(self._count(), 3)
        # 事务结束后连接不再共用：之后的普通写入照常各自提交。
        tm_manager.insert_manual_entry("later", "以后", "en-zh", sync_reverse=False)
        self.assertEqual(self._count(), 4)


if __name__ == "__main__":
    unittest.main()
