"""跨文件夹多选 / 拖入多个文件夹：每个选中的文件都要进任务（审计 2026-10-02:62）。

界面把第一个文件的上级目录当任务来源（ui/src/views/workspace.ts 的 sourcePathFor），
后端以前只重扫这一个根再按 selected_paths 收窄，其他文件夹里的文件被静默丢掉。
这里按前端的规则喂给真实的任务准备流程。全离线：临时文件夹、本地假模型、隔离数据目录。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

from api.task_manager import TaskConflictError, TranslationTaskManager
from core import tm_manager
from settings import AppSettings, EngineSettings


def _write_book(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    sheet = workbook.active
    sheet["A1"] = "施工内容"
    workbook.save(path)
    workbook.close()


def _parent_dir(path: str) -> str:
    """``parentDirOf`` from ui/src/views/workspace.ts:1824."""
    normalized = path.rstrip("/\\")
    cut = max(normalized.rfind("/"), normalized.rfind("\\"))
    return normalized[:cut] if cut > 0 else normalized


def _source_path_for(picked: list[str]) -> str:
    """``sourcePathFor`` from ui/src/views/workspace.ts:2073."""
    return _parent_dir(picked[0]) if len(picked) > 1 else picked[0]


def _isolated_manager(test: unittest.TestCase, root: Path) -> TranslationTaskManager:
    app_data = root / "app-data"
    for patcher in (
        patch.dict(os.environ, {"TRANSLATOR_APP_DATA_DIR": str(app_data)}),
        patch.object(tm_manager, "DB_PATH", app_data / "tm.db"),
    ):
        patcher.start()
        test.addCleanup(patcher.stop)
    return TranslationTaskManager(
        settings_loader=lambda: AppSettings(
            engine=EngineSettings(mode="local", local_provider="ollama", local_model="test-model")
        )
    )


class CrossDirectoryMultiSelectTests(unittest.TestCase):
    def _prepare(self, root: Path, picked: list[str]):
        return _isolated_manager(self, root)._prepare_task(
            surface="excel",
            source_path=_source_path_for(picked),
            selected_paths=picked,
            options=None,
        )

    def _root(self) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Path(temporary.name).resolve()

    def test_every_selected_file_reaches_the_task(self) -> None:
        root = self._root()
        first = root / "one" / "a.xlsx"
        second = root / "two" / "b.xlsx"
        _write_book(first)
        _write_book(second)

        prepared = self._prepare(root, [str(first), str(second)])

        scheduled = {Path(item.path).name for item in prepared.files}
        self.assertEqual(scheduled, {"a.xlsx", "b.xlsx"}, f"跨目录多选只排进了 {sorted(scheduled)}")
        # 来源根换成共同的上级：两个文件的译文各按自己的相对位置放。
        self.assertEqual(Path(prepared.source_path), root)

    def test_same_named_files_in_two_folders_are_both_kept(self) -> None:
        root = self._root()
        first = root / "one" / "清单.xlsx"
        second = root / "two" / "清单.xlsx"
        _write_book(first)
        _write_book(second)

        prepared = self._prepare(root, [str(first), str(second)])

        self.assertEqual(
            sorted(str(Path(item.path).parent.name) for item in prepared.files),
            ["one", "two"],
        )

    def test_files_beside_the_first_one_are_not_dragged_in(self) -> None:
        """补扫只扫选中的路径：第一个文件夹里没选的文件照旧不进任务。"""
        root = self._root()
        first = root / "one" / "a.xlsx"
        _write_book(root / "one" / "not-picked.xlsx")
        second = root / "two" / "b.xlsx"
        _write_book(root / "two" / "also-not-picked.xlsx")
        _write_book(first)
        _write_book(second)

        prepared = self._prepare(root, [str(first), str(second)])

        self.assertEqual(sorted(Path(item.path).name for item in prepared.files), ["a.xlsx", "b.xlsx"])


class MaintenanceVersusNewTaskTests(unittest.TestCase):
    """同一条审计项的另一半：维护页的「没有活动任务」检查和清理之间不许插进新任务。"""

    def test_a_task_cannot_register_while_a_clean_up_is_running(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        book = root / "a.xlsx"
        _write_book(book)
        manager = _isolated_manager(self, root)
        prepared = manager._prepare_task(
            surface="excel", source_path=str(book), selected_paths=None, options=None
        )

        with manager.maintenance_window() as active:
            self.assertEqual(active, 0)
            with self.assertRaises(TaskConflictError) as caught:
                manager._start_prepared(prepared, expected_revision=None)
            self.assertEqual(caught.exception.reason, "maintenance_active")
            self.assertEqual(manager.active_task_count(), 0)
        self.assertEqual(manager._maintenance_holds, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
