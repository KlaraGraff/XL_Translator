"""Shared app-data isolation for tests that start a real task manager.

``TranslationTaskManager`` persists task-center summaries, task logs and TM
rows.  Constructed without isolation it writes them into the developer's own
installation, where they later show up as phantom tasks in the app.  Patching
``settings.APP_DATA_DIR`` is enough: the history store and the task logger both
resolve their file from it when they are created.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from core import tm_manager
import settings as settings_module


class IsolatedAppDataTestCase(unittest.TestCase):
    """Point one test case's app data at a temporary directory."""

    def setUp(self) -> None:
        super().setUp()
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.app_data_dir = Path(temporary.name) / "app-data"
        for patcher in (
            patch.object(settings_module, "APP_DATA_DIR", self.app_data_dir),
            patch.object(settings_module, "SETTINGS_PATH", self.app_data_dir / "settings.json"),
            patch.object(settings_module, "KEYS_PATH", self.app_data_dir / "keys.json"),
            patch.object(tm_manager, "DB_PATH", self.app_data_dir / "tm.db"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
