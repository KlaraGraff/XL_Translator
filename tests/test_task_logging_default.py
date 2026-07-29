from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.file_scanner import FileItem
from core.task_logger import CONTENT_WITHHELD_MESSAGE, sanitize_task_log_message
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


class TaskLogSanitizationTests(unittest.TestCase):
    """脱敏必须保留进度：面板只显示“已脱敏”等于没有面板。"""

    def test_operational_lines_survive_sanitization(self) -> None:
        for message in (
            "[阶段 1] 提取词汇：进度计划样例.xlsx（1/3）",
            "→ 进度计划样例.xlsx：12 个词条（0.031s）",
            "[全局] TM命中=0 | 待API翻译=9",
            "已完成 3/9 批，耗时 4.20s",
            "已切换连接：主账号 → 备用厂商（服务端不可用）",
        ):
            with self.subTest(message=message):
                self.assertEqual(sanitize_task_log_message(message), message.strip())

    def test_provider_url_stays_readable_but_paths_are_hidden(self) -> None:
        sanitized = sanitize_task_log_message(
            "POST https://api.example.test/v1/chat/completions 超时重试 2/3"
        )
        self.assertIn("https://api.example.test/v1/chat/completions", sanitized)
        self.assertIn("2/3", sanitized)

        sanitized = sanitize_task_log_message(
            "[诊断] source_root=/Users/me/docs | output_dir=C:\\Users\\me\\out"
        )
        self.assertEqual(sanitized, "[诊断] source_root=[path] | output_dir=[path]")

    def test_credentials_and_quoted_content_never_survive(self) -> None:
        self.assertEqual(
            sanitize_task_log_message("source_text: 混凝土工程量"),
            CONTENT_WITHHELD_MESSAGE,
        )
        self.assertEqual(
            sanitize_task_log_message("raw_response=complete provider answer"),
            CONTENT_WITHHELD_MESSAGE,
        )
        sanitized = sanitize_task_log_message(
            "Authorization: Bearer real-secret-token /private/input.docx"
        )
        self.assertNotIn("real-secret-token", sanitized)
        self.assertNotIn("/private/input.docx", sanitized)
        self.assertIn("[redacted]", sanitized)
        self.assertIn("[path]", sanitized)


if __name__ == "__main__":
    unittest.main(verbosity=2)
