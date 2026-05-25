from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.file_scanner import FileItem
from core.task_runner import TaskRunner
from core.word_document import WordFileItem
from core.word_task_runner import WordTaskRunner
from settings import AppSettings


class TaskLoggingDefaultTests(unittest.TestCase):
    def test_excel_task_logger_is_always_enabled(self) -> None:
        settings = AppSettings()
        settings.output.enable_task_log = False

        logger_cls = MagicMock()
        logger_cls.return_value.task_id = "excel-task"
        with patch.dict(TaskRunner.__init__.__globals__, {"TaskLogger": logger_cls}):
            TaskRunner(
                [FileItem(path=Path("source.xlsx"), name="source", size_kb=1.0)],
                settings,
            )

        logger_cls.assert_called_once_with(enabled=True)

    def test_word_task_logger_is_always_enabled(self) -> None:
        settings = AppSettings()
        settings.output.enable_task_log = False

        logger_cls = MagicMock()
        logger_cls.return_value.task_id = "word-task"
        with patch.dict(WordTaskRunner.__init__.__globals__, {"TaskLogger": logger_cls}):
            WordTaskRunner(
                [WordFileItem(path=Path("source.docx"), name="source", size_kb=1.0)],
                settings,
            )

        logger_cls.assert_called_once_with(enabled=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
