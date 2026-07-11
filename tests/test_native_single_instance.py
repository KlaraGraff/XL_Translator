from __future__ import annotations

import os
import threading
import time
import unittest
import uuid

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from native_app.single_instance import SingleInstanceCoordinator, notify_existing_instance


class NativeSingleInstanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _notify_from_secondary_thread(
        self,
        server_name: str,
        activations: list[str],
        *,
        close_primary: SingleInstanceCoordinator | None = None,
    ) -> bool:
        results: list[bool] = []
        thread = threading.Thread(
            target=lambda: results.append(notify_existing_instance(server_name, 1000)),
        )
        thread.start()
        deadline = time.monotonic() + 2
        while thread.is_alive() and time.monotonic() < deadline:
            self.app.processEvents()
            if activations and close_primary is not None:
                close_primary.close()
                close_primary = None
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, [True])
        while not activations and time.monotonic() < deadline:
            self.app.processEvents()
        return results[0]

    def test_secondary_instance_notifies_primary(self) -> None:
        server_name = f"translator-test-{uuid.uuid4().hex}"
        activations: list[str] = []
        primary = SingleInstanceCoordinator(
            lambda: activations.append("activate"),
            server_name=server_name,
        )
        self.addCleanup(primary.close)

        self.assertTrue(primary.claim_or_notify(timeout_ms=100))
        self.assertTrue(self._notify_from_secondary_thread(server_name, activations))
        self.assertEqual(activations, ["activate"])

    def test_server_name_is_stable_and_nonempty(self) -> None:
        coordinator = SingleInstanceCoordinator(lambda: None)
        self.addCleanup(coordinator.close)
        self.assertTrue(coordinator.server_name)
        self.assertNotIn(" ", coordinator.server_name)

    def test_primary_can_close_before_peer_disconnect_event_is_processed(self) -> None:
        server_name = f"translator-test-{uuid.uuid4().hex}"
        activations: list[str] = []
        primary = SingleInstanceCoordinator(
            lambda: activations.append("activate"),
            server_name=server_name,
        )
        self.assertTrue(primary.claim_or_notify(timeout_ms=100))
        self.assertTrue(
            self._notify_from_secondary_thread(
                server_name,
                activations,
                close_primary=primary,
            )
        )
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

        self.assertEqual(activations, ["activate"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
