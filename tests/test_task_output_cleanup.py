"""任务失败或停止时，没写进任何东西的输出文件夹要清掉；本机接管任务能要回来源路径。"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from api.task_manager import ApiTask, TaskNotFoundError, TranslationTaskManager, _drop_empty_output_dir
from core.task_history import TaskHistoryStore
from settings import AppSettings


class DropEmptyOutputDirTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_empty_generated_folder_is_removed_and_dropped_from_result(self) -> None:
        folder = self.root / "资料_翻译输出_20261011_120000"
        (folder / "sub").mkdir(parents=True)
        (folder / ".DS_Store").write_bytes(b"")
        result = _drop_empty_output_dir({"output_dir": str(folder), "translated_dir": str(folder / "sub")})
        self.assertFalse(folder.exists())
        self.assertEqual(result["output_dir"], "")
        self.assertEqual(result["translated_dir"], "")

    def test_folder_with_any_file_is_kept(self) -> None:
        folder = self.root / "资料_翻译输出_20261011_120000"
        (folder / "sub").mkdir(parents=True)
        (folder / "sub" / "报告.md").write_text("x", encoding="utf-8")
        result = _drop_empty_output_dir({"output_dir": str(folder)})
        self.assertTrue(folder.exists())
        self.assertEqual(result["output_dir"], str(folder))

    def test_folder_not_generated_by_the_app_is_never_touched(self) -> None:
        folder = self.root / "我自己的输出"
        folder.mkdir()
        result = _drop_empty_output_dir({"output_dir": str(folder)})
        self.assertTrue(folder.exists())
        self.assertEqual(result["output_dir"], str(folder))


class _Lease:
    def release(self) -> None:
        return None


class _Runner:
    def stop(self) -> None:
        return None


class TaskSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.manager = TranslationTaskManager(
            settings_loader=AppSettings,
            history_store=TaskHistoryStore(self.root / "task_history.json"),
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _task(self) -> ApiTask:
        task = ApiTask(
            task_id="t-1", surface="excel", source_path=str(self.root / "src"), source_label="1 个文件",
            runner=_Runner(), lease=_Lease(), created_at=time.time(),
        )
        task.file_sources["f1"] = str(self.root / "src" / "a.xlsx")
        self.manager._tasks[task.task_id] = task
        return task

    def test_live_task_returns_its_source_but_status_stays_path_free(self) -> None:
        task = self._task()
        source = self.manager.task_source(task.task_id)
        self.assertEqual(source["source_path"], str(self.root / "src"))
        self.assertEqual(source["files"], [{"file_id": "f1", "path": str(self.root / "src" / "a.xlsx")}])
        self.assertNotIn(str(self.root / "src"), str(self.manager.task_status(task.task_id)))

    def test_unknown_task_has_no_source(self) -> None:
        with self.assertRaises(TaskNotFoundError):
            self.manager.task_source("missing")

    def test_stopped_task_with_empty_output_folder_cleans_it_up(self) -> None:
        folder = self.root / "src_翻译输出_20261011_120000"
        folder.mkdir()
        task = self._task()
        self.manager._finish_if_needed(task, "stopped", "stopped", {"message": "已停止", "output_dir": str(folder)})
        self.assertFalse(folder.exists())
        self.assertEqual(task.result["output_dir"], "")

    def test_completed_task_keeps_its_folder_even_when_empty(self) -> None:
        folder = self.root / "src_翻译输出_20261011_120000"
        folder.mkdir()
        task = self._task()
        self.manager._finish_if_needed(task, "done", "done", {"output_dir": str(folder)})
        self.assertTrue(folder.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
