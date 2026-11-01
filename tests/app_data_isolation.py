"""Shared app-data isolation for tests that start a real task manager.

``TranslationTaskManager`` persists task-center summaries, task logs and TM
rows.  Constructed without isolation it writes them into the developer's own
installation, where they later show up as phantom tasks in the app.  Patching
``settings.APP_DATA_DIR`` is enough: the history store and the task logger both
resolve their file from it when they are created.
"""

from __future__ import annotations

import os

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from core import diagnostics, tm_manager
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
            patch.object(settings_module, "RECOVERY_PATH", self.app_data_dir / "recovery.json"),
            patch.object(settings_module, "BACKUPS_DIR", self.app_data_dir / "backups"),
            patch.object(tm_manager, "BACKUPS_DIR", self.app_data_dir / "backups"),
            patch.object(tm_manager, "DB_PATH", self.app_data_dir / "tm.db"),
            # 任务结束时会写一份匿名诊断记录（「导出诊断」导出的就是它）。
            patch.object(diagnostics, "DIAGNOSTICS_DIR", self.app_data_dir / "diagnostics"),
            patch.object(diagnostics, "DIAGNOSTIC_RECORDS_DIR", self.app_data_dir / "diagnostics" / "records"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        # 开发机上若设了 MIMO_API_KEY 之类的环境变量，取密钥会落到它上面，
        # 「没填密钥」的用例就变成拿真密钥去连。这里一律清掉，只认测试自己存的。
        api_key_env = patch.dict(
            os.environ,
            {name: "" for name in os.environ if name.endswith("_API_KEY") or name.endswith("_API_KEY_ID")},
        )
        api_key_env.start()
        self.addCleanup(api_key_env.stop)
