"""Concurrency and isolation contracts for task-center history persistence."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import core.task_logger as task_logger_module
import settings as settings_module
from app_meta import APP_NAME
from core.task_history import TaskHistoryStore, default_history_path


class TaskHistoryStoreTests(unittest.TestCase):
    def test_independent_stores_share_a_path_lock_and_preserve_updates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "task_history.json"
            first = TaskHistoryStore(path)
            second = TaskHistoryStore(path)
            barrier = threading.Barrier(2)
            errors: list[Exception] = []

            def write_records(store: TaskHistoryStore, task_id: str) -> None:
                try:
                    barrier.wait()
                    for sequence in range(40):
                        store.upsert({"task_id": task_id, "sequence": sequence})
                except Exception as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            workers = [
                threading.Thread(target=write_records, args=(first, "first")),
                threading.Thread(target=write_records, args=(second, "second")),
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()

            self.assertEqual(errors, [])
            records = {record["task_id"]: record for record in first.records()}
            self.assertEqual(set(records), {"first", "second"})
            self.assertEqual(records["first"]["sequence"], 39)
            self.assertEqual(records["second"]["sequence"], 39)


class AppDataIsolationTests(unittest.TestCase):
    """Default app-data paths must follow the isolation a test asks for.

    A path resolved at import time ignores ``settings.APP_DATA_DIR`` patches, so
    fixture tasks land in the real user data directory and then surface as
    phantom results and logs in the shipped app.
    """

    def test_default_task_history_and_log_paths_follow_patched_app_data_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "app-data"
            with patch.object(settings_module, "APP_DATA_DIR", root):
                self.assertEqual(default_history_path(), root / "task_history.json")
                self.assertEqual(task_logger_module.task_log_path(), root / "app.log")

                store = TaskHistoryStore()
                store.upsert({"task_id": "isolated", "state": "done"})
                self.assertEqual(
                    [record["task_id"] for record in store.records()],
                    ["isolated"],
                )
            self.assertTrue((root / "task_history.json").exists())

    def test_importing_the_test_package_moves_app_data_out_of_the_user_home(self) -> None:
        """The package hook retargets app data when a runner sets no override.

        It only fires for import styles that load ``tests`` as a package, so the
        per-module isolation stays mandatory; this pins the safety net itself.
        """
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as home:
            environment = {**os.environ, "HOME": home, "PYTHONPATH": str(repo_root)}
            environment.pop("TRANSLATOR_APP_DATA_DIR", None)
            completed = subprocess.run(
                [sys.executable, "-c", "import tests, config; print(config.APP_DATA_DIR)"],
                cwd=repo_root,
                env=environment,
                capture_output=True,
                text=True,
                check=True,
            )
            resolved = Path(completed.stdout.strip())
            self.assertNotIn(Path(home), resolved.parents)
            self.assertFalse((Path(home) / "Library" / "Application Support" / APP_NAME).exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
