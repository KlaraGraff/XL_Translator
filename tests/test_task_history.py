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


class TaskHistoryRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "task_history.json"
        recovery = patch.object(settings_module, "RECOVERY_PATH", self.root / "recovery.json")
        recovery.start()
        self.addCleanup(recovery.stop)

    def backups(self) -> list[Path]:
        return list((self.root / "backups" / "task_history").glob("*.json"))

    def test_corrupt_json_wrong_shape_and_invalid_items_keep_private_original_bytes(self) -> None:
        from core.task_history import TASK_HISTORY_RECOVERY_SCOPE

        for raw, expected in [
            (b'{broken\xff', []),
            (b'{"task_id":"wrong-shape"}', []),
            (b'[null, 42, {"task_id":"kept"}]', [{"task_id": "kept"}]),
        ]:
            with self.subTest(raw=raw):
                self.path.write_bytes(raw)
                previous = set(self.backups())
                store = TaskHistoryStore(self.path)
                self.assertEqual(store.records(), expected)
                backup = (set(self.backups()) - previous).pop()
                self.assertEqual(backup.read_bytes(), raw)
                if os.name != "nt":
                    self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
                event = settings_module.read_recovery_record()[TASK_HISTORY_RECOVERY_SCOPE]
                self.assertEqual(event["backup_path"], str(backup))
                store.upsert({"task_id": "new"})
                self.assertEqual(store.records(), [{"task_id": "new"}, *expected])
                self.assertEqual(set(self.backups()), previous | {backup})

    def test_valid_legacy_list_unchanged_no_backup_or_notice(self) -> None:
        raw = b'[ {"task_id":"legacy", "unknown_field":{"x":1}} ]\n'
        self.path.write_bytes(raw)
        self.assertEqual(TaskHistoryStore(self.path).records()[0]["unknown_field"], {"x": 1})
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertEqual(self.backups(), [])
        self.assertEqual(settings_module.read_recovery_record(), {})

    def test_backup_uses_windows_owner_acl_helper_without_reencoding_bytes(self) -> None:
        store = TaskHistoryStore(self.path)
        with patch.object(settings_module, "restrict_windows_file_to_owner", return_value=True) as restrict:
            backup = store._backup_raw_locked(b'\xff\xfe original bytes')
        restrict.assert_called_once_with(backup)
        self.assertEqual(backup.read_bytes(), b'\xff\xfe original bytes')
        with (
            patch.object(os, "name", "nt"),
            patch.object(settings_module, "restrict_windows_file_to_owner", return_value=False),
        ):
            with self.assertRaisesRegex(PermissionError, "仅当前用户"):
                store._backup_raw_locked(b'private')
        self.assertEqual(self.backups(), [backup])

    def test_malformed_old_progress_is_backed_up_and_does_not_break_manager_startup(self) -> None:
        import json

        from api.task_manager import TranslationTaskManager

        records = [
            {"task_id": "bad", "state": "running", "file_progress": {"revision": "oops", "files": 7}},
            {"task_id": "bad-dict", "state": "paused", "file_progress": {"revision": {}, "files": {"x": "bad"}}},
            {"task_id": "good", "state": "running", "file_progress": {"revision": 4, "files": [{"state": "running"}]}},
            {"task_id": "finished", "state": "done", "unknown": "retained"},
        ]
        raw = json.dumps(records).encode()
        self.path.write_bytes(raw)
        store = TaskHistoryStore(self.path)
        TranslationTaskManager(history_store=store)
        recovered = store.records()
        self.assertEqual(self.backups()[0].read_bytes(), raw)
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual(len(recovered), 4)
        self.assertEqual(recovered[0]["state"], "interrupted")
        self.assertEqual(recovered[0]["file_progress"], {"revision": 1, "files": []})
        self.assertEqual(recovered[1]["file_progress"]["files"], [])
        self.assertEqual(recovered[2]["file_progress"]["revision"], 5)
        self.assertEqual(recovered[3]["unknown"], "retained")

    def test_read_or_backup_failure_preserves_original_and_does_not_break_startup(self) -> None:
        from core.task_history import TaskHistoryError

        raw = b'{broken'
        for failure in ("read", "backup", "rebuild"):
            with self.subTest(failure=failure):
                self.path.write_bytes(raw)
                store = TaskHistoryStore(self.path)
                target = {
                    "read": patch.object(Path, "read_bytes", side_effect=PermissionError("read denied")),
                    "backup": patch.object(store, "_backup_raw_locked", side_effect=PermissionError("backup denied")),
                    "rebuild": patch.object(store, "_write_locked", side_effect=PermissionError("replace denied")),
                }[failure]
                with target:
                    self.assertEqual(store.mark_active_interrupted(), [])
                    self.assertEqual(store.health_status()["state"], "unreadable")
                    with self.assertRaisesRegex(TaskHistoryError, "原文件未被覆盖"):
                        store.upsert({"task_id": "new"})
                self.assertEqual(self.path.read_bytes(), raw)

    def test_multiple_instances_recover_once_and_keep_concurrent_updates(self) -> None:
        from concurrent.futures import ThreadPoolExecutor

        self.path.write_bytes(b'{broken')
        barrier = threading.Barrier(8)

        def work(index: int) -> None:
            store = TaskHistoryStore(self.path)
            barrier.wait()
            store.upsert({"task_id": str(index)})

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(work, range(8)))
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual(len(TaskHistoryStore(self.path).records()), 8)

    def test_explicit_clear_does_not_backup_corruption_or_restore_old_backup(self) -> None:
        store = TaskHistoryStore(self.path)
        self.path.write_bytes(b'{broken')
        self.assertEqual(store.clear(), 0)
        self.assertEqual(self.backups(), [])
        self.path.write_bytes(b'{broken')
        store.records()
        self.assertEqual(len(self.backups()), 1)
        store.clear()
        self.assertEqual(settings_module.read_recovery_record(), {})
        self.assertEqual(TaskHistoryStore(self.path).records(), [])
        self.assertFalse(self.path.exists())
        self.assertEqual(len(self.backups()), 1)

    def test_data_health_reports_recovery_blocked_reason_and_dismissal(self) -> None:
        from core import maintenance

        self.path.write_bytes(b'{broken')
        with (
            patch.object(settings_module, "APP_DATA_DIR", self.root),
            patch.object(maintenance, "get_settings_schema_status", return_value={"state": "current"}),
            patch.object(maintenance, "recover_settings_file_if_needed"),
            patch.object(maintenance.tm_manager, "get_schema_status", return_value={"state": "current"}),
            patch.object(maintenance.tm_manager, "init_db"),
        ):
            health = maintenance.data_health()["task_history"]
            self.assertEqual(health["state"], "recreated")
            self.assertTrue(Path(health["backup_path"]).exists())
            self.path.write_bytes(b'{broken again')
            with patch.object(TaskHistoryStore, "_backup_raw_locked", side_effect=PermissionError("backup denied")):
                blocked = maintenance.data_health()["task_history"]
                self.assertEqual(blocked["state"], "unreadable")
                self.assertIn("backup denied", blocked["reason"])
            maintenance.dismiss_recovery_notice(["task_history"])
            self.assertEqual(settings_module.read_recovery_record(), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
