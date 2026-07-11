from __future__ import annotations

import os
import time
import unittest
import uuid

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from native_app.single_instance import SingleInstanceCoordinator


class NativeSingleInstanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_secondary_instance_notifies_primary(self) -> None:
        server_name = f"translator-test-{uuid.uuid4().hex}"
        activations: list[str] = []
        primary = SingleInstanceCoordinator(
            lambda: activations.append("activate"),
            server_name=server_name,
        )
        secondary = SingleInstanceCoordinator(lambda: None, server_name=server_name)
        self.addCleanup(primary.close)
        self.addCleanup(secondary.close)

        self.assertTrue(primary.claim_or_notify(timeout_ms=100))
        self.assertFalse(secondary.claim_or_notify(timeout_ms=250))

        deadline = time.monotonic() + 0.5
        while not activations and time.monotonic() < deadline:
            self.app.processEvents()
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
        secondary = SingleInstanceCoordinator(lambda: None, server_name=server_name)

        self.assertTrue(primary.claim_or_notify(timeout_ms=100))
        self.assertFalse(secondary.claim_or_notify(timeout_ms=250))
        deadline = time.monotonic() + 0.5
        while not activations and time.monotonic() < deadline:
            self.app.processEvents()

        primary.close()
        secondary.close()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

        self.assertEqual(activations, ["activate"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
