"""File lifecycle regressions: identity, terminal truth, privacy and recovery."""

from __future__ import annotations

import json
import queue
import time
from pathlib import Path
from types import SimpleNamespace

from api.task_manager import ApiTask, TranslationTaskManager
from core.file_progress import FileProgressReporter, file_progress_entry, file_progress_id
from core.task_history import TaskHistoryStore
from core.task_runner import DoneMsg, StoppedMsg
from tests.app_data_isolation import IsolatedAppDataTestCase


class _Lease:
    def release(self) -> None:
        pass


class _Runner:
    def stop(self) -> None:
        pass


class FileProgressTests(IsolatedAppDataTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.files = [
            SimpleNamespace(path=self.app_data_dir / "one" / "report.pdf"),
            SimpleNamespace(path=self.app_data_dir / "two" / "report.pdf"),
            SimpleNamespace(path=self.app_data_dir / "three.pdf"),
        ]
        self.messages = queue.Queue()
        self.reporter = FileProgressReporter(self.messages, self.files, self.app_data_dir)
        self.manager = TranslationTaskManager(history_store=TaskHistoryStore(self.app_data_dir / "history.json"))
        self.task = ApiTask(
            task_id="file-state-test", surface="pdf", source_path=str(self.app_data_dir),
            source_label="3 files", runner=_Runner(), lease=_Lease(), created_at=time.time(),
        )
        for item in self.files:
            entry = file_progress_entry(item, self.app_data_dir)
            self.task.file_progress[entry["file_id"]] = entry
            self.task.file_sources[entry["file_id"]] = str(item.path)
        self.manager._tasks[self.task.task_id] = self.task

    def _emit(self, index: int, state: str, phase: str, **kwargs) -> dict:
        self.reporter.emit(self.files[index].path, state, phase, **kwargs)
        self.manager._handle_message(self.task, self.messages.get_nowait())
        return self.task.file_progress[file_progress_id(self.files[index].path)]

    def test_two_same_names_have_independent_progress_and_no_source_paths(self) -> None:
        first = self._emit(0, "active", "translate", completed=2, total=5)
        second = self._emit(1, "waiting", "translate", total=1)
        self.assertNotEqual(first["file_id"], second["file_id"])
        self.assertEqual(first["state"], "active")
        self.assertEqual(second["state"], "waiting")
        snapshot = self.manager.task_status(self.task.task_id)
        serialized = json.dumps(snapshot)
        self.assertNotIn(str(self.app_data_dir), serialized)
        self.assertNotIn("file_sources", serialized)
        self.assertEqual(first["revision"], self.task.events[0]["id"])

    def test_generated_needs_review_is_not_failed_and_results_keep_ids(self) -> None:
        self._emit(0, "active", "generate")
        self._emit(1, "failed", "prepare", result={"error": "Cannot read"})
        result = [
            {"name": "report.pdf", "source_path": str(self.files[0].path),
             "status": "needs_review", "success": False, "output": "/output/report.pdf"},
            {"name": "report.pdf", "source_path": str(self.files[1].path),
             "status": "failed", "success": False, "error": "Cannot read"},
            {"name": "three.pdf", "status": "unstarted", "success": False},
        ]
        self.manager._handle_message(self.task, DoneMsg("/output", result, 1, 0, 0))
        states = [entry["state"] for entry in self.task.file_progress.values()]
        self.assertEqual(states, ["generated", "failed", "unstarted"])
        ids = [entry["file_id"] for entry in self.task.result["file_results"]]
        self.assertEqual(len(set(ids)), 3)
        serialized = json.dumps(self.task.result)
        self.assertNotIn(str(self.app_data_dir), serialized)
        self.assertEqual(self.task.events[-1]["type"], "done")

    def test_stop_preserves_output_and_never_marks_remaining_files_generated(self) -> None:
        self._emit(0, "generated", "generate", result={"output": "/output/report.pdf"})
        self._emit(1, "active", "translate")
        self.manager._handle_message(self.task, StoppedMsg(message="Stopped"))
        self.assertEqual(
            [entry["state"] for entry in self.task.file_progress.values()],
            ["generated", "stopped", "unstarted"],
        )
        # A late in-flight message cannot revive the stopped terminal snapshot.
        self.reporter.emit(self.files[1].path, "active", "translate")
        self.manager._handle_message(self.task, self.messages.get_nowait())
        self.assertEqual(list(self.task.file_progress.values())[1]["state"], "stopped")

    def test_restart_marks_only_unfinished_files_interrupted(self) -> None:
        self._emit(0, "generated", "generate", result={"output": "/output/report.pdf"})
        self._emit(1, "active", "translate")
        self.manager.mark_active_tasks_interrupted()
        self.assertEqual(
            [entry["state"] for entry in self.task.file_progress.values()],
            ["generated", "interrupted", "interrupted"],
        )
        stored = self.manager._history.records()[0]
        self.assertEqual(stored["file_progress"]["files"][0]["state"], "generated")

    def test_history_restart_handles_new_snapshots_and_legacy_records(self) -> None:
        self.manager._history.upsert({"task_id": "legacy", "state": "running"})
        self._emit(0, "generated", "generate", result={"output": "/output/report.pdf"})
        self._emit(1, "active", "translate")
        self.manager._persist_task(self.task)
        self.manager._history.mark_active_interrupted()
        records = {row["task_id"]: row for row in self.manager._history.records()}
        self.assertEqual(records["legacy"]["state"], "interrupted")
        progress = records[self.task.task_id]["file_progress"]
        self.assertEqual([row["state"] for row in progress["files"]], ["generated", "interrupted", "interrupted"])

    def test_shared_phase_does_not_revive_failed_files(self) -> None:
        self.reporter.emit(self.files[0].path, "failed", "extract")
        self.messages.get_nowait()
        self.reporter.emit_many("active", "translate")
        emitted = []
        while not self.messages.empty():
            emitted.append(self.messages.get_nowait().file_id)
        self.assertNotIn(file_progress_id(self.files[0].path), emitted)
        self.assertEqual(len(emitted), 2)

    def test_relative_source_path_matches_without_name_guess(self) -> None:
        result = self.manager._complete_file_progress(self.task, {
            "files": [{"name": "report", "source_relative_path": "two/report.pdf", "output": "/output/two.pdf"}],
        }, "stopped")
        entry = result["files"][0]
        self.assertEqual(entry["file_id"], file_progress_id(self.files[1].path))
        self.assertEqual(list(self.task.file_progress.values())[1]["state"], "generated")

    def test_ids_match_scanner_paths_with_relative_components(self) -> None:
        path = self.files[0].path
        self.assertEqual(file_progress_id(path), file_progress_id(path.parent / "." / path.name))
        self.assertNotEqual(file_progress_id(path), file_progress_id(Path(str(path) + "x")))
