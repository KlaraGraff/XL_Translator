"""Regressions for deleting a single task record and exporting its diagnostics.

Two entry points added for the task center's 「删除记录」 and 「导出诊断」 buttons:

* ``TranslationTaskManager.delete_task_record`` — removes one summary without
  touching translated output, and refuses while the task is still running;
* ``GET /api/diagnostics/task/{task_id}.zip`` — resolves the archive belonging
  to one task even though records deliberately store only an anonymous locator.
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import settings as settings_module
from api.app import create_app
from api.task_manager import (
    ApiTask,
    TaskConflictError,
    TaskNotFoundError,
    TranslationTaskManager,
)
from core import diagnostics, tm_manager
from core.task_history import TaskHistoryStore
from settings import AppSettings
from tests.app_data_isolation import IsolatedAppDataTestCase


class _Lease:
    def release(self) -> None:
        return None

    def scheduler_for(self, group):
        return None


class _Runner:
    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def needs_poll(self) -> bool:
        return False

    def get_message(self, timeout: float = 0.05):
        return None


class DeleteTaskRecordTests(IsolatedAppDataTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.app_data_dir.mkdir(parents=True, exist_ok=True)
        self.manager = TranslationTaskManager(
            history_store=TaskHistoryStore(self.app_data_dir / "task_history.json"),
        )

    def _register(self, task_id: str, *, terminal: bool, state: str) -> ApiTask:
        task = ApiTask(
            task_id=task_id,
            surface="excel",
            source_path="",
            source_label=task_id,
            runner=_Runner(),
            lease=_Lease(),
            created_at=time.time(),
        )
        task.state = state
        task.terminal = terminal
        self.manager._tasks[task_id] = task
        return task

    def test_running_task_record_cannot_be_deleted(self) -> None:
        self._register("live", terminal=False, state="running")
        with self.assertRaises(TaskConflictError) as raised:
            self.manager.delete_task_record("live")
        self.assertEqual(raised.exception.reason, "task_active")
        self.assertIn("live", self.manager._tasks)

    def test_unknown_task_record_reports_not_found(self) -> None:
        with self.assertRaises(TaskNotFoundError):
            self.manager.delete_task_record("never-existed")

    def test_deleting_one_record_keeps_the_others_newest_first(self) -> None:
        # upsert 插到表头，所以先写的最旧；删除后剩下的顺序必须原样保留。
        for task_id in ("oldest", "middle", "newest"):
            self.manager._history.upsert({"task_id": task_id, "state": "done"})
        self.assertEqual(
            [item["task_id"] for item in self.manager._history.records()],
            ["newest", "middle", "oldest"],
        )

        result = self.manager.delete_task_record("middle")

        self.assertEqual(result["task_id"], "middle")
        self.assertEqual(result["removed_count"], 1)
        self.assertFalse(result["outputs_affected"])
        self.assertEqual(
            [item["task_id"] for item in self.manager._history.records()],
            ["newest", "oldest"],
        )

    def test_terminal_task_is_dropped_from_memory_and_history(self) -> None:
        self._register("finished", terminal=True, state="done")
        self.manager._history.upsert({"task_id": "finished", "state": "done"})

        self.manager.delete_task_record("finished")

        self.assertNotIn("finished", self.manager._tasks)
        self.assertEqual(self.manager._history.records(), [])
        # 删除只清记录：结果查询随之 404，而不是留下一条读不出来的幽灵。
        with self.assertRaises(TaskNotFoundError):
            self.manager.task_status("finished")

    def test_a_concurrent_finish_is_never_swallowed_by_a_delete(self) -> None:
        """The rewrite happens under the store's lock, so nothing lands mid-way.

        The old implementation cleared the file and then re-inserted every kept
        record one at a time.  A task finishing during that window wrote into a
        half-empty file and was then overwritten by the backfill.  Here the
        concurrent writer runs while ``remove`` is inside ``_write_locked``: it
        must block, not interleave.
        """
        for index in range(20):
            self.manager._history.upsert({"task_id": f"task-{index}", "state": "done"})
        store = self.manager._history
        original_write = store._write_locked
        finished: list[threading.Thread] = []

        def _write_then_race(records):
            if finished:
                return original_write(records)
            worker = threading.Thread(
                target=store.upsert,
                args=({"task_id": "just-finished", "state": "done"},),
            )
            finished.append(worker)
            worker.start()
            worker.join(timeout=0.5)
            # 抢锁的线程必须还在等：写到一半的文件不能被别人看见。
            self.assertTrue(worker.is_alive())
            return original_write(records)

        with patch.object(store, "_write_locked", _write_then_race):
            self.manager.delete_task_record("task-7")
        finished[0].join(timeout=5)

        remaining = [item["task_id"] for item in store.records()]
        self.assertIn("just-finished", remaining)
        self.assertNotIn("task-7", remaining)
        self.assertEqual(len(remaining), 20)

    def test_pending_history_writes_survive_the_rewrite(self) -> None:
        """Another task's throttled summary must not be rolled back by the delete."""
        pending = self._register("pending", terminal=True, state="done")
        pending.history_dirty = True
        self.manager._history.upsert({"task_id": "doomed", "state": "error"})

        self.manager.delete_task_record("doomed")

        surviving = {item["task_id"] for item in self.manager._history.records()}
        self.assertEqual(surviving, {"pending"})


class TaskDiagnosticExportTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for patcher in (
            patch.multiple(
                settings_module,
                APP_DATA_DIR=self.root / "app-data",
                SETTINGS_PATH=self.root / "app-data" / "settings.json",
                KEYS_PATH=self.root / "app-data" / "keys.json",
            ),
            patch.object(tm_manager, "DB_PATH", self.root / "app-data" / "tm.db"),
            patch.object(diagnostics, "DIAGNOSTIC_RECORDS_DIR", self.root / "diagnostics"),
            patch.object(diagnostics, "LOG_PATH", self.root / "app-data" / "app.log"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.manager = TranslationTaskManager(settings_loader=AppSettings)
        self.client = TestClient(create_app(task_manager=self.manager))

    def _archive(self, task_id: str) -> None:
        diagnostics.archive_task_diagnostics(
            surface="excel",
            phase="translate",
            task_id=task_id,
            settings=AppSettings(),
            selected_files=[object()],
            logs=[{"level": "INFO", "message": "started"}],
            status="error",
        )

    def test_export_returns_only_the_requested_task_archive(self) -> None:
        self._archive("task-alpha")
        self._archive("task-beta")

        response = self.client.get("/api/diagnostics/task/task-alpha.zip")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/zip")
        # 归档目录名里带的是匿名定位符，绝不能是任务 id 本身。
        disposition = response.headers.get("content-disposition", "")
        self.assertNotIn("task-alpha", disposition)
        self.assertIn(diagnostics._anonymous_locator("task-alpha"), disposition)
        self.assertTrue(response.content)

    def test_task_without_diagnostics_reports_404(self) -> None:
        self._archive("task-alpha")
        self.assertEqual(
            self.client.get("/api/diagnostics/task/task-without-record.zip").status_code,
            404,
        )

    def test_delete_route_rejects_a_running_task_with_409(self) -> None:
        task = ApiTask(
            task_id="live",
            surface="excel",
            source_path="",
            source_label="live",
            runner=_Runner(),
            lease=_Lease(),
            created_at=time.time(),
        )
        task.state = "running"
        self.manager._tasks["live"] = task

        self.assertEqual(self.client.delete("/api/tasks/live").status_code, 409)

        task.terminal = True
        self.assertEqual(self.client.delete("/api/tasks/live").status_code, 200)
        self.assertEqual(self.client.get("/api/tasks/live").status_code, 404)


if __name__ == "__main__":  # pragma: no cover - convenience runner
    unittest.main()
