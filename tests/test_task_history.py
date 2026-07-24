"""Concurrency contracts for task-center history persistence."""

from __future__ import annotations

import threading
import tempfile
import unittest
from pathlib import Path

from core.task_history import TaskHistoryStore


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
