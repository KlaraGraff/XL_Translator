from __future__ import annotations

import os
import threading
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QApplication

from native_app.workers import CallableWorker, DaemonWorker, TaskResourceRegistry


class _BlockingWorker(DaemonWorker):
    def __init__(self, release: threading.Event, parent=None) -> None:
        super().__init__(parent)
        self.started = threading.Event()
        self.release = release

    def run(self) -> None:
        self.started.set()
        self.release.wait(2)


class NativeWorkerLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_blocked_worker_dispose_is_bounded_and_detaches_from_owner(self) -> None:
        owner = QObject()
        release = threading.Event()
        worker = _BlockingWorker(release, owner)
        worker.start()
        self.assertTrue(worker.started.wait(0.5))

        started_at = time.monotonic()
        stopped = worker.dispose(timeout_ms=20)
        elapsed = time.monotonic() - started_at

        self.assertFalse(stopped)
        self.assertLess(elapsed, 0.25)
        self.assertTrue(worker.isInterruptionRequested())
        self.assertIsNone(worker.parent())
        self.assertTrue(worker.isRunning())

        owner.deleteLater()
        self.app.processEvents()
        release.set()
        self.assertTrue(worker.wait(500))

    def test_callable_worker_runs_blocking_action_off_gui_thread(self) -> None:
        gui_thread_id = threading.get_ident()
        worker_thread_ids: list[int] = []
        worker = CallableWorker(lambda: worker_thread_ids.append(threading.get_ident()) or "ok")
        results: list[str] = []
        worker.resultReady.connect(results.append)

        worker.start()
        self.assertTrue(worker.wait(500))
        self.app.processEvents()

        self.assertEqual(results, ["ok"])
        self.assertNotEqual(worker_thread_ids, [gui_thread_id])

    def test_resource_registry_atomically_rejects_parallel_conflicts(self) -> None:
        registry = TaskResourceRegistry()
        barrier = threading.Barrier(3)
        leases = []
        lock = threading.Lock()

        def acquire(owner: str) -> None:
            barrier.wait()
            lease = registry.acquire(
                owner_key=owner,
                owner_label=owner,
                resources={"shared-api"},
            )
            with lock:
                leases.append(lease)

        first = threading.Thread(target=acquire, args=("first",))
        second = threading.Thread(target=acquire, args=("second",))
        first.start()
        second.start()
        barrier.wait()
        first.join(0.5)
        second.join(0.5)

        acquired = [lease for lease in leases if lease is not None]
        self.assertEqual(len(acquired), 1)
        self.assertEqual(len(registry.reservations()), 1)
        self.assertTrue(acquired[0].release())
        self.assertFalse(acquired[0].release())
        self.assertEqual(registry.reservations(), ())

    def test_resource_registry_allows_disjoint_known_resources(self) -> None:
        registry = TaskResourceRegistry()
        first = registry.acquire(
            owner_key="excel",
            owner_label="Excel",
            resources={"api-a"},
        )
        second = registry.acquire(
            owner_key="pdf",
            owner_label="PDF",
            resources={"api-b"},
        )

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(len(registry.reservations()), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
