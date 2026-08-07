"""Regressions for task-manager history cost, memory retention and shutdown.

Three defects live here, all in ``api/task_manager.py``:

* every SSE event triggered a full history rewrite, so a long task paid O(n^2);
* finished tasks were never released, so ``_tasks`` and the runners it pinned
  grew for as long as the sidecar ran;
* ``shutdown`` had no caller at all, so a closing window killed live runners
  and left the history stuck on "running".
"""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

import api.task_manager as task_manager_module
from api.task_manager import (
    ApiTask,
    MAX_RETAINED_TERMINAL_TASKS,
    RetiredRunner,
    TaskNotFoundError,
    TranslationTaskManager,
)
from core.task_history import TaskHistoryStore
from tests.app_data_isolation import IsolatedAppDataTestCase


class _Lease:
    def __init__(self) -> None:
        self.released = False

    def release(self) -> None:
        self.released = True

    def scheduler_for(self, group):
        return None


class _Runner:
    """Runner stand-in that records the stop request and pins a heavy payload."""

    def __init__(self) -> None:
        self.stopped = threading.Event()
        # Stands in for the scanned file list plus the settings deep copy that
        # a real runner keeps alive.
        self.heavy_payload = ["file"] * 32

    def start(self) -> None:
        return None

    def stop(self) -> None:
        self.stopped.set()

    def needs_poll(self) -> bool:
        return not self.stopped.is_set()

    def get_message(self, timeout: float = 0.05):
        return None


class _UnstoppableRunner(_Runner):
    def stop(self) -> None:
        raise RuntimeError("runner will not stop")


class TaskManagerLifecycleTests(IsolatedAppDataTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.app_data_dir.mkdir(parents=True, exist_ok=True)
        self.history_path = self.app_data_dir / "task_history.json"
        self.manager = TranslationTaskManager(
            history_store=TaskHistoryStore(self.history_path),
        )

    def _register(self, task_id: str, *, surface: str = "excel") -> ApiTask:
        task = ApiTask(
            task_id=task_id,
            surface=surface,
            source_path="",
            source_label=task_id,
            runner=_Runner(),
            lease=_Lease(),
            created_at=time.time(),
        )
        self.manager._tasks[task_id] = task
        return task

    # --- history write throttling -------------------------------------

    def test_progress_events_do_not_each_rewrite_the_history_file(self) -> None:
        task = self._register("throttled")
        writes: list[str] = []
        real_upsert = self.manager._history.upsert

        def counting_upsert(record):
            writes.append(str(record.get("state") or ""))
            return real_upsert(record)

        with patch.object(self.manager._history, "upsert", counting_upsert):
            for index in range(500):
                self.manager._append_event(
                    task, "log", {"level": "INFO", "message": f"line {index}"}
                )
        # 500 same-state events inside one interval must not be 500 rewrites.
        self.assertLessEqual(len(writes), 3)
        self.assertGreaterEqual(len(writes), 1)

    def test_history_flush_cost_stays_flat_as_events_pile_up(self) -> None:
        """Guard the O(n^2) shape, not the absolute speed of this machine."""
        task = self._register("timed")
        started = time.perf_counter()
        for index in range(200):
            self.manager._append_event(
                task, "log", {"level": "INFO", "message": f"early {index}"}
            )
        first_window = time.perf_counter() - started
        for index in range(1800):
            self.manager._append_event(
                task, "log", {"level": "INFO", "message": f"filler {index}"}
            )
        started = time.perf_counter()
        for index in range(200):
            self.manager._append_event(
                task, "log", {"level": "INFO", "message": f"late {index}"}
            )
        last_window = time.perf_counter() - started
        # Before the fix the same 200 events cost ~40x more once 2000 events
        # were already in the payload.  A generous ceiling keeps this from
        # flapping on a loaded machine while still catching a per-event write.
        self.assertLess(last_window, max(first_window * 8, 0.5))

    def test_state_change_is_persisted_even_inside_the_throttle_window(self) -> None:
        task = self._register("state-change")
        self.manager._append_event(task, "log", {"level": "INFO", "message": "one"})
        with task.condition:
            task.state = "stopping"
        self.manager._append_event(task, "stopping", {"state": "stopping"})
        record = self.manager._history_record("state-change")
        self.assertIsNotNone(record)
        self.assertEqual(record["state"], "stopping")

    def test_flush_history_writes_out_what_the_throttle_held_back(self) -> None:
        task = self._register("held-back")
        for index in range(50):
            self.manager._append_event(
                task, "log", {"level": "INFO", "message": f"line {index}"}
            )
        self.assertTrue(task.history_dirty)
        self.manager.flush_history()
        self.assertFalse(task.history_dirty)
        record = self.manager._history_record("held-back")
        self.assertIsNotNone(record)
        self.assertEqual(len(record["logs"]), 50)

    def test_terminal_event_is_persisted_before_readers_are_woken(self) -> None:
        """An SSE consumer must never outrun the write of the final summary."""
        task = self._register("terminal-order")
        seen_state: list[str | None] = []

        def watcher() -> None:
            with task.condition:
                while not task.terminal:
                    task.condition.wait(timeout=2)
            record = self.manager._history_record("terminal-order")
            seen_state.append(None if record is None else record.get("state"))

        thread = threading.Thread(target=watcher)
        thread.start()
        with task.condition:
            task.state = "succeeded"
            task.terminal = True
            self.manager._append_event(task, "done", {"ok": True})
        thread.join(timeout=5)
        self.assertEqual(seen_state, ["succeeded"])

    # --- terminal task retention --------------------------------------

    def test_finished_task_releases_its_runner(self) -> None:
        task = self._register("retired")
        with task.condition:
            task.state = "succeeded"
            task.terminal = True
            self.manager._append_event(task, "done", {"ok": True})
        self.manager._retire_terminal_task(task)
        self.assertIsInstance(task.runner, RetiredRunner)
        # The stand-in still answers the protocol, so a late stop cannot raise.
        task.runner.stop()
        self.assertFalse(task.runner.needs_poll())

    def test_pdf_task_keeps_its_runner_for_page_comparison(self) -> None:
        task = self._register("pdf-task", surface="pdf")
        with task.condition:
            task.state = "succeeded"
            task.terminal = True
            self.manager._append_event(task, "done", {"ok": True})
        self.manager._retire_terminal_task(task)
        self.assertIsInstance(task.runner, _Runner)

    def test_event_backlog_is_trimmed_but_keeps_the_terminal_event(self) -> None:
        task = self._register("trimmed")
        for index in range(task_manager_module.TERMINAL_EVENT_TAIL + 400):
            self.manager._append_event(
                task, "log", {"level": "INFO", "message": f"line {index}"}
            )
        with task.condition:
            task.state = "succeeded"
            task.terminal = True
            self.manager._append_event(task, "done", {"ok": True})
        self.manager._retire_terminal_task(task)
        self.assertLessEqual(len(task.events), task_manager_module.TERMINAL_EVENT_TAIL)
        self.assertEqual(task.events[-1]["type"], "done")

    def test_only_the_most_recent_finished_tasks_stay_in_memory(self) -> None:
        total = MAX_RETAINED_TERMINAL_TASKS + 5
        for index in range(total):
            task = self._register(f"task-{index:02d}")
            with task.condition:
                task.state = "succeeded"
                task.terminal = True
                task.updated_at = 1000.0 + index
                self.manager._append_event(task, "done", {"ok": True})
            self.manager._retire_terminal_task(task)
        self.assertEqual(len(self.manager._tasks), MAX_RETAINED_TERMINAL_TASKS)
        # The oldest ones went, the newest stayed.
        self.assertNotIn("task-00", self.manager._tasks)
        self.assertIn(f"task-{total - 1:02d}", self.manager._tasks)

    def test_evicted_task_is_still_answerable_from_the_persisted_summary(self) -> None:
        total = MAX_RETAINED_TERMINAL_TASKS + 3
        for index in range(total):
            task = self._register(f"gone-{index:02d}")
            with task.condition:
                task.state = "succeeded"
                task.terminal = True
                task.updated_at = 2000.0 + index
                task.result = {"output": f"artifact-{index}"}
                self.manager._append_event(task, "done", task.result)
            self.manager._retire_terminal_task(task)
        self.assertNotIn("gone-00", self.manager._tasks)
        status = self.manager.task_status("gone-00")
        self.assertEqual(status["task_id"], "gone-00")
        self.assertEqual(status["state"], "succeeded")
        # Opening the artifact from the task center still works after eviction.
        self.assertEqual(status["result"]["output"], "artifact-0")
        self.assertEqual(self.manager.task_results("gone-00")["task_id"], "gone-00")

    def test_unknown_task_still_raises_rather_than_returning_a_stub(self) -> None:
        with self.assertRaises(TaskNotFoundError):
            self.manager.task_status("never-existed")

    def test_active_tasks_are_never_evicted(self) -> None:
        live = self._register("live")
        for index in range(MAX_RETAINED_TERMINAL_TASKS + 4):
            task = self._register(f"done-{index:02d}")
            with task.condition:
                task.state = "succeeded"
                task.terminal = True
                task.updated_at = 3000.0 + index
                self.manager._append_event(task, "done", {"ok": True})
            self.manager._retire_terminal_task(task)
        self.assertIn("live", self.manager._tasks)
        self.assertIsInstance(live.runner, _Runner)

    # --- graceful shutdown ---------------------------------------------

    def test_begin_shutdown_signals_runners_without_waiting(self) -> None:
        task = self._register("stopping")
        started = time.perf_counter()
        self.manager.begin_shutdown()
        self.assertLess(time.perf_counter() - started, 1.0)
        self.assertTrue(task.runner.stopped.is_set())
        self.assertEqual(task.state, "stopping")
        self.assertFalse(task.terminal)

    def test_shutdown_returns_when_the_runner_finishes_on_its_own(self) -> None:
        task = self._register("cooperative")

        def finish_soon() -> None:
            time.sleep(0.1)
            with task.condition:
                task.state = "succeeded"
                task.terminal = True
                self.manager._append_event(task, "done", {"ok": True})

        thread = threading.Thread(target=finish_soon)
        thread.start()
        self.manager.shutdown(timeout=5)
        thread.join(timeout=5)
        self.assertEqual(task.state, "succeeded")
        record = self.manager._history_record("cooperative")
        self.assertEqual(record["state"], "succeeded")

    def test_shutdown_marks_a_stuck_task_interrupted_instead_of_running(self) -> None:
        task = self._register("stuck")
        self.manager.shutdown(timeout=0.2)
        self.assertTrue(task.terminal)
        self.assertEqual(task.state, "interrupted")
        self.assertTrue(task.lease.released)
        record = self.manager._history_record("stuck")
        self.assertEqual(record["state"], "interrupted")
        self.assertFalse(record["result"]["recovery"]["can_resume"])

    def test_a_runner_that_refuses_to_stop_does_not_break_shutdown(self) -> None:
        task = self._register("obstinate")
        task.runner = _UnstoppableRunner()
        self.manager.shutdown(timeout=0.2)
        self.assertEqual(task.state, "interrupted")

    def test_shutdown_flushes_summaries_the_throttle_held_back(self) -> None:
        task = self._register("dirty-at-exit")
        for index in range(30):
            self.manager._append_event(
                task, "log", {"level": "INFO", "message": f"line {index}"}
            )
        self.assertTrue(task.history_dirty)
        self.manager.shutdown(timeout=0.2)
        record = self.manager._history_record("dirty-at-exit")
        # The interrupted event is not a log line, so the 30 held-back log
        # lines are exactly what the flush had to rescue.
        self.assertEqual(len(record["logs"]), 30)
        self.assertEqual(record["state"], "interrupted")

    def test_open_sse_stream_ends_on_shutdown_instead_of_blocking_it(self) -> None:
        task = self._register("streaming")
        self.manager._append_event(task, "log", {"level": "INFO", "message": "hello"})
        stream = self.manager.iter_sse("streaming")
        self.assertIn("hello", next(stream))

        finished = threading.Event()

        def drain() -> None:
            for _chunk in stream:
                pass
            finished.set()

        thread = threading.Thread(target=drain, daemon=True)
        thread.start()
        self.manager.begin_shutdown()
        # Without the shutdown check the generator parks on the 15s keepalive
        # wait, and uvicorn's graceful shutdown waits on the open connection.
        self.assertTrue(finished.wait(timeout=5))
        thread.join(timeout=5)

    def test_restarted_manager_closes_history_left_running(self) -> None:
        task = self._register("orphan")
        self.manager._append_event(task, "log", {"level": "INFO", "message": "mid-run"})
        self.manager.flush_history()
        self.assertEqual(self.manager._history_record("orphan")["state"], "running")

        restarted = TranslationTaskManager(
            history_store=TaskHistoryStore(self.history_path),
        )
        self.assertEqual(restarted._history_record("orphan")["state"], "interrupted")
