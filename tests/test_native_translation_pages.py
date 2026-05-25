from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QTableWidget, QVBoxLayout, QWidget

from core.task_runner import DoneMsg
from native_app.pages.excel_translate import ExcelTranslatePage
from native_app.pages.word_translate import WordTranslatePage
from native_app.result_view import ResultIssueRow, render_translation_result
from settings import AppSettings


class NativeTranslationPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _done_msg(self) -> DoneMsg:
        return DoneMsg(
            output_dir="",
            file_results=[],
            elapsed_sec=0,
            tm_hit_count=0,
            api_call_count=0,
        )

    def _button_texts(self, page) -> list[str]:
        return [button.text() for button in page.action_card.findChildren(QPushButton)]

    def _visible_button_texts(self, page) -> list[str]:
        return [
            button.text()
            for button in page.action_card.findChildren(QPushButton)
            if button.parent() is not None
        ]

    def test_word_action_card_does_not_offer_stop_after_done_payload(self) -> None:
        with patch("native_app.pages.word_translate.count_diagnostic_records", return_value=0):
            page = WordTranslatePage(AppSettings())
        self.addCleanup(page.close)
        self.addCleanup(page.deleteLater)

        page.phase = "running"
        page.runner = object()
        page.done = self._done_msg()
        page._render_action_card()

        button_texts = self._button_texts(page)
        self.assertIn("返回并开始新任务", button_texts)
        self.assertNotIn("终止翻译", button_texts)

    def test_word_done_poll_replaces_stop_action(self) -> None:
        class FakeRunner:
            task_id = "fake-word"

            def __init__(self, done: DoneMsg) -> None:
                self._messages = [done]

            def get_message(self, timeout: float = 0.0):
                return self._messages.pop(0) if self._messages else None

            def needs_poll(self) -> bool:
                return bool(self._messages)

        archive_seen = []
        page_holder = {}

        def archive_side_effect(*_args, **_kwargs) -> None:
            page_holder["page"]._render_action_card()
            archive_seen.append(
                (
                    page_holder["page"].phase,
                    self._visible_button_texts(page_holder["page"]),
                )
            )

        count_patch = patch(
            "native_app.pages.word_translate.count_diagnostic_records",
            return_value=0,
        )
        archive_patch = patch(
            "native_app.pages.word_translate.archive_task_diagnostics",
            side_effect=archive_side_effect,
        )
        with count_patch, archive_patch:
            page = WordTranslatePage(AppSettings())
            page_holder["page"] = page
            self.addCleanup(page.close)
            self.addCleanup(page.deleteLater)

            page.phase = "running"
            page.runner = FakeRunner(self._done_msg())
            page._render_action_card()
            self.assertIn("终止翻译", self._visible_button_texts(page))

            page._poll_runner()

            button_texts = self._visible_button_texts(page)
            self.assertEqual(page.phase, "done")
            self.assertIn("返回并开始新任务", button_texts)
            self.assertNotIn("终止翻译", button_texts)
            self.assertEqual(archive_seen[0][0], "done")
            self.assertNotIn("终止翻译", archive_seen[0][1])

    def test_word_workspace_guard_repairs_stale_stop_action(self) -> None:
        with patch("native_app.pages.word_translate.count_diagnostic_records", return_value=0):
            page = WordTranslatePage(AppSettings())
        self.addCleanup(page.close)
        self.addCleanup(page.deleteLater)

        page.phase = "running"
        page.runner = object()
        page.done = None
        page._workspace_render_phase = "done"
        page.ui_sync_timer.start()
        page._render_action_card()
        self.assertIn("终止翻译", self._visible_button_texts(page))

        page._sync_action_card_with_workspace()

        button_texts = self._visible_button_texts(page)
        self.assertEqual(page.phase, "done")
        self.assertIn("返回并开始新任务", button_texts)
        self.assertNotIn("终止翻译", button_texts)
        self.assertFalse(page.ui_sync_timer.isActive())

    def test_excel_action_card_does_not_offer_stop_after_done_payload(self) -> None:
        with patch("native_app.pages.excel_translate.count_diagnostic_records", return_value=0):
            page = ExcelTranslatePage(AppSettings())
        self.addCleanup(page.close)
        self.addCleanup(page.deleteLater)

        page.phase = "running"
        page.runner = object()
        page.done = self._done_msg()
        page._render_action_card()

        button_texts = self._button_texts(page)
        self.assertIn("返回并开始新任务", button_texts)
        self.assertNotIn("终止翻译", button_texts)

    def test_excel_done_poll_replaces_stop_action(self) -> None:
        class FakeRunner:
            task_id = "fake-excel"

            def __init__(self, done: DoneMsg) -> None:
                self._messages = [done]

            def get_message(self, timeout: float = 0.0):
                return self._messages.pop(0) if self._messages else None

            def needs_poll(self) -> bool:
                return bool(self._messages)

        archive_seen = []
        page_holder = {}

        def archive_side_effect(*_args, **_kwargs) -> None:
            page_holder["page"]._render_action_card()
            archive_seen.append(
                (
                    page_holder["page"].phase,
                    self._visible_button_texts(page_holder["page"]),
                )
            )

        count_patch = patch(
            "native_app.pages.excel_translate.count_diagnostic_records",
            return_value=0,
        )
        archive_patch = patch(
            "native_app.pages.excel_translate.archive_task_diagnostics",
            side_effect=archive_side_effect,
        )
        with count_patch, archive_patch:
            page = ExcelTranslatePage(AppSettings())
            page_holder["page"] = page
            self.addCleanup(page.close)
            self.addCleanup(page.deleteLater)

            page.phase = "running"
            page.runner = FakeRunner(self._done_msg())
            page._render_action_card()
            self.assertIn("终止翻译", self._visible_button_texts(page))

            page._poll_runner()

            button_texts = self._visible_button_texts(page)
            self.assertEqual(page.phase, "done")
            self.assertIn("返回并开始新任务", button_texts)
            self.assertNotIn("终止翻译", button_texts)
            self.assertEqual(archive_seen[0][0], "done")
            self.assertNotIn("终止翻译", archive_seen[0][1])

    def test_excel_workspace_guard_repairs_stale_stop_action(self) -> None:
        with patch("native_app.pages.excel_translate.count_diagnostic_records", return_value=0):
            page = ExcelTranslatePage(AppSettings())
        self.addCleanup(page.close)
        self.addCleanup(page.deleteLater)

        page.phase = "running"
        page.runner = object()
        page.done = None
        page._workspace_render_phase = "done"
        page.ui_sync_timer.start()
        page._render_action_card()
        self.assertIn("终止翻译", self._visible_button_texts(page))

        page._sync_action_card_with_workspace()

        button_texts = self._visible_button_texts(page)
        self.assertEqual(page.phase, "done")
        self.assertIn("返回并开始新任务", button_texts)
        self.assertNotIn("终止翻译", button_texts)
        self.assertFalse(page.ui_sync_timer.isActive())

    def test_excel_done_workspace_uses_shared_result_contract(self) -> None:
        with patch("native_app.pages.excel_translate.count_diagnostic_records", return_value=0):
            page = ExcelTranslatePage(AppSettings())
        self.addCleanup(page.close)
        self.addCleanup(page.deleteLater)
        page.done = DoneMsg(
            output_dir="/tmp/out",
            file_results=[{"name": "source.xlsx", "success": True, "error": ""}],
            elapsed_sec=61,
            tm_hit_count=2,
            api_call_count=3,
        )
        container = QWidget()
        self.addCleanup(container.deleteLater)
        layout = QVBoxLayout(container)

        page._render_done_workspace(layout)

        labels = [label.text() for label in container.findChildren(QLabel)]
        self.assertIn("翻译成功。", labels)
        self.assertIn("已生成文件", labels)
        self.assertIn("输出目录", labels)
        self.assertNotIn("成功文件", labels)

    def test_shared_result_view_renders_optional_issue_table(self) -> None:
        container = QWidget()
        self.addCleanup(container.deleteLater)
        layout = QVBoxLayout(container)

        render_translation_result(
            layout,
            empty_message="完成。",
            done=DoneMsg(
                output_dir="/tmp/out",
                file_results=[{"name": "source.docx", "success": True, "error": ""}],
                elapsed_sec=0,
                tm_hit_count=0,
                api_call_count=1,
            ),
            summary_text="翻译成功。",
            summary_success=True,
            kpi_items=[("已生成文件", "1")],
            file_status_formatter=lambda result: "已生成 / 成功",
            issue_rows=[
                ResultIssueRow(
                    issue_type="已自动处理",
                    file_name="source.docx",
                    position="正文段落 3",
                    problem="规则校验未通过",
                    status="候选译文已自动接受。",
                )
            ],
        )

        tables = container.findChildren(QTableWidget)
        self.assertEqual(len(tables), 2)
        self.assertEqual(tables[0].horizontalHeaderItem(3).text(), "问题")
        self.assertEqual(tables[0].item(0, 3).text(), "规则校验未通过")

    def test_shared_result_view_hides_empty_detail_column(self) -> None:
        container = QWidget()
        self.addCleanup(container.deleteLater)
        layout = QVBoxLayout(container)

        render_translation_result(
            layout,
            empty_message="完成。",
            done=DoneMsg(
                output_dir="/tmp/out",
                file_results=[{"name": "source.xlsx", "success": True, "error": ""}],
                elapsed_sec=0,
                tm_hit_count=0,
                api_call_count=1,
            ),
            summary_text="翻译成功。",
            summary_success=True,
            kpi_items=[("已生成文件", "1")],
            file_status_formatter=lambda result: "已生成 / 成功",
        )

        table = container.findChildren(QTableWidget)[0]
        self.assertEqual(table.columnCount(), 2)
        self.assertEqual(table.horizontalHeaderItem(1).text(), "状态")

    def test_shared_result_view_shows_error_reason_when_needed(self) -> None:
        container = QWidget()
        self.addCleanup(container.deleteLater)
        layout = QVBoxLayout(container)

        render_translation_result(
            layout,
            empty_message="完成。",
            done=DoneMsg(
                output_dir="/tmp/out",
                file_results=[
                    {
                        "name": "source.xlsx",
                        "success": False,
                        "error": "源文件读取失败",
                    }
                ],
                elapsed_sec=0,
                tm_hit_count=0,
                api_call_count=1,
            ),
            summary_text="任务完成：已生成 0 个文件，生成失败 1 个文件。",
            summary_success=False,
            kpi_items=[("已生成文件", "0"), ("生成失败", "1")],
            file_status_formatter=lambda result: "生成失败",
        )

        table = container.findChildren(QTableWidget)[0]
        self.assertEqual(table.columnCount(), 3)
        self.assertEqual(table.horizontalHeaderItem(2).text(), "错误原因")
        self.assertEqual(table.item(0, 2).text(), "源文件读取失败")


if __name__ == "__main__":
    unittest.main(verbosity=2)
