from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from api.launcher import _parent_process_is_alive


class ApiLauncherTests(unittest.TestCase):
    def test_current_process_is_alive(self) -> None:
        self.assertTrue(_parent_process_is_alive(os.getpid()))

    def test_missing_process_is_not_alive(self) -> None:
        with patch("api.launcher.os.kill", side_effect=ProcessLookupError):
            self.assertFalse(_parent_process_is_alive(987_654))

    def test_permission_error_means_process_is_alive(self) -> None:
        with patch("api.launcher.os.kill", side_effect=PermissionError):
            self.assertTrue(_parent_process_is_alive(987_654))


if __name__ == "__main__":
    unittest.main(verbosity=2)
