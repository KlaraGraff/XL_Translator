from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest
import uuid

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from native_app.single_instance import SingleInstanceCoordinator


SECONDARY_INSTANCE_SCRIPT = """
import sys
from PySide6.QtCore import QCoreApplication
from native_app.single_instance import SingleInstanceCoordinator

app = QCoreApplication([])
coordinator = SingleInstanceCoordinator(lambda: None, server_name=sys.argv[1])
is_primary = coordinator.claim_or_notify(timeout_ms=int(sys.argv[2]))
coordinator.close()
raise SystemExit(1 if is_primary else 0)
"""


class NativeSingleInstanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _notify_from_secondary_process(
        self,
        server_name: str,
        activations: list[str],
        *,
        close_primary: SingleInstanceCoordinator | None = None,
    ) -> None:
        process = subprocess.Popen(
            [sys.executable, "-c", SECONDARY_INSTANCE_SCRIPT, server_name, "3000"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 5
        while process.poll() is None and time.monotonic() < deadline:
            self.app.processEvents()
            if activations and close_primary is not None:
                close_primary.close()
                close_primary = None
        if process.poll() is None:
            process.kill()
        stdout, stderr = process.communicate(timeout=2)
        self.assertEqual(
            process.returncode,
            0,
            f"secondary instance failed\nstdout: {stdout}\nstderr: {stderr}",
        )
        while not activations and time.monotonic() < deadline:
            self.app.processEvents()

    def test_secondary_instance_notifies_primary(self) -> None:
        server_name = f"translator-test-{uuid.uuid4().hex}"
        activations: list[str] = []
        primary = SingleInstanceCoordinator(
            lambda: activations.append("activate"),
            server_name=server_name,
        )
        self.addCleanup(primary.close)

        self.assertTrue(primary.claim_or_notify(timeout_ms=100))
        self._notify_from_secondary_process(server_name, activations)
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
        self._notify_from_secondary_process(
            server_name,
            activations,
            close_primary=primary,
        )
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

        self.assertEqual(activations, ["activate"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
