from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from api.launcher import _parent_process_is_alive


class ApiLauncherTests(unittest.TestCase):
    def test_current_process_is_alive(self) -> None:
        self.assertTrue(_parent_process_is_alive(os.getpid()))

    def test_missing_process_is_not_alive(self) -> None:
        with patch("api.launcher.psutil.pid_exists", return_value=False):
            self.assertFalse(_parent_process_is_alive(987_654))

    def test_probe_error_means_process_is_alive(self) -> None:
        with patch("api.launcher.psutil.pid_exists", side_effect=OSError):
            self.assertTrue(_parent_process_is_alive(987_654))

    def test_probe_never_sends_a_signal(self) -> None:
        # A liveness probe must not reach for os.kill: on Windows any signal
        # other than CTRL_C/CTRL_BREAK terminates the target process.
        with patch("api.launcher.os.kill", side_effect=AssertionError("os.kill called")):
            self.assertTrue(_parent_process_is_alive(os.getpid()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
