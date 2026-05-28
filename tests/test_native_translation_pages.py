from __future__ import annotations

import atexit
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QCheckBox, QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget, QVBoxLayout, QWidget

from core.file_scanner import FileItem
from core.pdf_image_translation import PdfFileItem
from core.task_runner import DoneMsg, PdfPageRecoveryStatusMsg, ProgressMsg
from core.task_queue import (
    ApiConcurrencyGroupKey,
    ApiConcurrencyRequirement,
    TASK_STATUS_COMPLETED,
    TRANSLATION_TYPE_EXCEL,
    TRANSLATION_TYPE_PDF,
    TRANSLATION_TYPE_WORD,
    TranslationTask,
    TranslationTaskSnapshot,
)
from core.word_document import WordFileItem
from native_app.pages.excel_translate import ExcelTranslatePage
from native_app.pages.pdf_translate import PdfTranslatePage
from native_app.pages.word_translate import WordTranslatePage
from native_app.result_view import ResultIssueRow, render_translation_result
from native_app.task_queue_controller import NativeTranslationQueueController
from native_app.task_queue_view import clear_layout
from native_app.widgets import MiddleElideLineEdit
from settings import AppSettings, set_cloud_provider_config


try:
    import xlwings._xlmac as _xlwings_mac

    atexit.unregister(_xlwings_mac.cleanup)
except Exception:
    pass


class _OkApiCheck:
    ok = True
    message = "测试配置可用"
    detail = ""


class _LiveFakeRunner:
    """Runner test double that stays pollable until explicitly finished."""

    counter = 0

    def __init__(self, *_args, **_kwargs) -> None:
        type(self).counter += 1
        self.task_id = f"live-fake-runner-{type(self).counter:03d}"
        self._messages = []
        self._alive = False
        self.stopped = False

    def start(self) -> None:
        self._alive = True

    def stop(self) -> None:
        self.stopped = True

    def get_message(self, timeout: float = 0.0):
        return self._messages.pop(0) if self._messages else None

    def needs_poll(self) -> bool:
        return self._alive or bool(self._messages)


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

    def _arrange_completed_queue_task(self, page, translation_type: str) -> None:
        controller = getattr(page, "queue_controller", None)
        if controller is None:
            controller = NativeTranslationQueueController()
            page.set_queue_controller(controller)
        group = ApiConcurrencyGroupKey(
            mode="cloud",
            base_url="https://api.example.com/v1",
            api_key_hash="hash",
        )
        task = TranslationTask(
            snapshot=TranslationTaskSnapshot(
                title=f"{translation_type} 翻译 · 1 个文件",
                translation_type=translation_type,
                file_count=1,
                target_language="英文",
            ),
            group_requirements=(
                ApiConcurrencyRequirement(
                    key=group,
                    declared_concurrency=5,
                    provider="custom_openai",
                    role="translation",
                    role_label="翻译模型",
                    key_fingerprint="sk-a...wxyz",
                ),
            ),
        )
        arranged = controller.arrange(task, starter=lambda _task: None)
        controller.finish_task(arranged.task_id, TASK_STATUS_COMPLETED, message="已完成")

    def _arrange_running_queue_task(self, page, translation_type: str) -> TranslationTask:
        controller = getattr(page, "queue_controller", None)
        if controller is None:
            controller = NativeTranslationQueueController()
            page.set_queue_controller(controller)
        group = ApiConcurrencyGroupKey(
            mode="cloud",
            base_url="https://api.example.com/v1",
            api_key_hash="hash",
        )
        task = TranslationTask(
            snapshot=TranslationTaskSnapshot(
                title=f"{translation_type} 翻译 · 1 个文件",
                translation_type=translation_type,
                file_count=1,
                target_language="英文",
            ),
            group_requirements=(
                ApiConcurrencyRequirement(
                    key=group,
                    declared_concurrency=5,
                    provider="custom_openai",
                    role="translation",
                    role_label="翻译模型",
                    key_fingerprint="sk-a...wxyz",
                ),
            ),
        )
        return controller.arrange(task, starter=lambda _task: None)

    def _queue_button(self, page) -> QPushButton:
        return next(
            button
            for button in page.action_card.findChildren(QPushButton)
            if button.text().startswith("查看翻译列表")
        )

    def _has_queue_entry(self, page) -> bool:
        return any(
            text.startswith("查看翻译列表")
            for text in self._visible_button_texts(page)
        )

    def _sample_scan_item(self, translation_type: str):
        if translation_type == TRANSLATION_TYPE_EXCEL:
            return FileItem(Path("/tmp/sample.xlsx"), "sample", 1.0, ["Sheet1"])
        if translation_type == TRANSLATION_TYPE_WORD:
            return WordFileItem(Path("/tmp/sample.docx"), "sample.docx", 1.0, 2, 1)
        if translation_type == TRANSLATION_TYPE_PDF:
            return PdfFileItem(Path("/tmp/sample.pdf"), "sample.pdf", 1.0, 3)
        raise AssertionError(f"Unsupported translation type: {translation_type}")

    def _queue_ready_settings(self) -> AppSettings:
        settings = AppSettings()
        settings.engine.mode = "cloud"
        settings.engine.cloud_provider = "custom_openai"
        settings.engine.concurrency = 1
        settings.target_lang = "en"
        settings.source_lang = "zh"
        set_cloud_provider_config(
            settings.engine,
            "custom_openai",
            cloud_model="queue-ui-test-model",
            cloud_base_url="http://127.0.0.1:65535/v1",
        )
        return settings

    def _stop_page_runtime(self, page) -> None:
        runner = getattr(page, "runner", None)
        if hasattr(runner, "_alive"):
            runner._alive = False
        page.runner = None
        for attr in ("poll_timer", "ui_sync_timer"):
            timer = getattr(page, attr, None)
            if timer is not None:
                timer.stop()

    def test_translation_pages_use_page_specific_source_paths(self) -> None:
        settings = AppSettings(
            last_source_folder="/tmp/legacy.pdf",
            last_excel_source_folder="/tmp/excel-source.xlsx",
            last_word_source_folder="/tmp/word-source.docx",
            last_pdf_source_folder="/tmp/pdf-source.pdf",
        )

        with (
            patch("native_app.pages.excel_translate.count_diagnostic_records", return_value=0),
            patch("native_app.pages.word_translate.count_diagnostic_records", return_value=0),
            patch("native_app.pages.pdf_translate.count_diagnostic_records", return_value=0),
        ):
            excel_page = ExcelTranslatePage(settings)
            word_page = WordTranslatePage(settings)
            pdf_page = PdfTranslatePage(settings)
        for page in (excel_page, word_page, pdf_page):
            self.addCleanup(page.close)
            self.addCleanup(page.deleteLater)

        self.assertEqual(excel_page.source_root, "/tmp/excel-source.xlsx")
        self.assertEqual(excel_page.source_input.text(), "/tmp/excel-source.xlsx")
        self.assertIsInstance(excel_page.source_input, MiddleElideLineEdit)
        self.assertIsInstance(excel_page.custom_output_input, MiddleElideLineEdit)
        self.assertEqual(word_page.source_root, "/tmp/word-source.docx")
        self.assertEqual(word_page.source_input.text(), "/tmp/word-source.docx")
        self.assertIsInstance(word_page.source_input, MiddleElideLineEdit)
        self.assertIsInstance(word_page.custom_output_input, MiddleElideLineEdit)
        self.assertEqual(pdf_page.source_root, "/tmp/pdf-source.pdf")
        self.assertEqual(pdf_page.source_input.text(), "/tmp/pdf-source.pdf")
        self.assertIsInstance(pdf_page.source_input, MiddleElideLineEdit)
        self.assertIsInstance(pdf_page.custom_output_input, MiddleElideLineEdit)

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

    def test_word_reset_ignores_stale_terminal_guard(self) -> None:
        with patch("native_app.pages.word_translate.count_diagnostic_records", return_value=0):
            page = WordTranslatePage(AppSettings())
        self.addCleanup(page.close)
        self.addCleanup(page.deleteLater)

        page.phase = "done"
        page.done = self._done_msg()
        page._render_action_card()
        self.assertIn("返回并开始新任务", self._visible_button_texts(page))

        page._reset_task()
        page._workspace_render_phase = "done"
        page._sync_action_card_with_workspace()

        button_texts = self._visible_button_texts(page)
        self.assertEqual(page.phase, "idle")
        self.assertTrue(any(text.startswith("开始翻译（") for text in button_texts))
        self.assertNotIn("返回并开始新任务", button_texts)

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

    def test_excel_reset_ignores_stale_terminal_guard(self) -> None:
        with patch("native_app.pages.excel_translate.count_diagnostic_records", return_value=0):
            page = ExcelTranslatePage(AppSettings())
        self.addCleanup(page.close)
        self.addCleanup(page.deleteLater)

        page.phase = "done"
        page.done = self._done_msg()
        page._render_action_card()
        self.assertIn("返回并开始新任务", self._visible_button_texts(page))

        page._reset_task()
        page._workspace_render_phase = "done"
        page._sync_action_card_with_workspace()

        button_texts = self._visible_button_texts(page)
        self.assertEqual(page.phase, "idle")
        self.assertTrue(any(text.startswith("开始翻译（") for text in button_texts))
        self.assertNotIn("返回并开始新任务", button_texts)

    def test_pdf_page_renders_image_layout_route_defaults(self) -> None:
        with patch("native_app.pages.pdf_translate.count_diagnostic_records", return_value=0):
            page = PdfTranslatePage(AppSettings())
        self.addCleanup(page.close)
        self.addCleanup(page.deleteLater)

        self.assertEqual(page.windowTitle(), "")
        title_labels = [
            label for label in page.findChildren(QLabel)
            if label.text() == "PDF/图片翻译"
        ]
        self.assertEqual(len(title_labels), 1)
        self.assertEqual(page.retry_spin.value(), 3)
        retry_labels = [
            label for label in page.findChildren(QLabel)
            if label.text() == "页级重试次数"
        ]
        self.assertEqual(len(retry_labels), 1)
        self.assertIn("总尝试次数 = 首次生成 + 重试次数", retry_labels[0].toolTip())
        self.assertEqual(page.pdf_concurrency_input.text(), "")
        compression_checks = [
            checkbox
            for checkbox in page.findChildren(QCheckBox)
            if checkbox.text() == "同时生成压缩 PDF（推荐）"
        ]
        self.assertEqual(len(compression_checks), 1)
        self.assertTrue(compression_checks[0].isChecked())
        self.assertIn("关闭时只输出高清版", compression_checks[0].toolTip())
        image_checks = [
            checkbox
            for checkbox in page.findChildren(QCheckBox)
            if checkbox.text() == "启用图片翻译"
        ]
        self.assertEqual(len(image_checks), 1)
        self.assertFalse(image_checks[0].isChecked())
        self.assertIn("支持 PNG、JPG、JPEG、WebP、BMP、TIFF", image_checks[0].toolTip())
        self.assertFalse(
            any(
                label.text() == "开启后会同时输出高清版和压缩版；关闭时只输出高清版。"
                for label in page.findChildren(QLabel)
            )
        )
        review_checks = [
            checkbox
            for checkbox in page.findChildren(QCheckBox)
            if checkbox.text() == "启用翻译审核"
        ]
        self.assertEqual(len(review_checks), 1)
        self.assertIn("未启用时无需配置审核模型", review_checks[0].toolTip())
        self.assertFalse(
            any(
                label.text() == "可选项：未启用时无需配置审核模型；启用后会保留候选图和审核记录。"
                for label in page.findChildren(QLabel)
            )
        )
        self.assertFalse(any(label.text() == "源语言" for label in page.findChildren(QLabel)))
        self.assertIn("开始翻译", " ".join(self._button_texts(page)))

    def test_pdf_stop_request_switches_to_continue_translation_action(self) -> None:
        class FakeRunner:
            def __init__(self) -> None:
                self.stopped = False
                self.resumed = False

            def stop_requested(self) -> bool:
                return self.stopped

            def stop(self) -> None:
                self.stopped = True

            def resume(self) -> None:
                self.resumed = True
                self.stopped = False

        with patch("native_app.pages.pdf_translate.count_diagnostic_records", return_value=0):
            page = PdfTranslatePage(AppSettings())
        self.addCleanup(page.close)
        self.addCleanup(page.deleteLater)
        runner = FakeRunner()
        page.phase = "running"
        page.runner = runner
        page.progress = ProgressMsg(2, 4, "翻译页面", 4, 11)
        page.page_recovery_status = PdfPageRecoveryStatusMsg(
            total_pages=11,
            completed_pages=3,
            submitted_page_count=3,
            pending_submitted_page_count=0,
        )
        page._render_workspace()
        self.assertIn("停止提交新页", self._visible_button_texts(page))

        with patch(
            "native_app.pages.pdf_translate.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            page._confirm_stop()

        button_texts = self._visible_button_texts(page)
        self.assertIn("继续翻译", button_texts)
        self.assertNotIn("停止提交新页", button_texts)
        self.assertIn("已提交 3 页，等待 0 页完成", page.running_status.text())
        self.assertIn("继续翻译", page.running_status.text())
        action_labels = [label.text() for label in page.action_card.findChildren(QLabel)]
        self.assertIn("已停止提交新页，当前可继续翻译。", action_labels)

        page._resume_translation()

        self.assertTrue(runner.resumed)
        button_texts = self._visible_button_texts(page)
        self.assertIn("停止提交新页", button_texts)
        self.assertNotIn("继续翻译", button_texts)

    def test_pdf_resume_translation_does_not_prompt_for_confirmation(self) -> None:
        class FakeRunner:
            def __init__(self) -> None:
                self.resumed = False

            def stop_requested(self) -> bool:
                return True

            def resume(self) -> None:
                self.resumed = True

        with patch("native_app.pages.pdf_translate.count_diagnostic_records", return_value=0):
            page = PdfTranslatePage(AppSettings())
        self.addCleanup(page.close)
        self.addCleanup(page.deleteLater)
        runner = FakeRunner()
        page.phase = "running"
        page.runner = runner
        page._render_action_card()

        with patch("native_app.pages.pdf_translate.QMessageBox.question") as question:
            page._resume_translation()

        self.assertTrue(runner.resumed)
        self.assertFalse(question.called)

    def test_pdf_stopped_result_does_not_offer_continue_translation(self) -> None:
        with patch("native_app.pages.pdf_translate.count_diagnostic_records", return_value=0):
            page = PdfTranslatePage(AppSettings())
        self.addCleanup(page.close)
        self.addCleanup(page.deleteLater)

        page.phase = "stopped"
        page.runner = object()
        page.stop_message = "PDF 翻译已中止"
        page._render_workspace()

        button_texts = self._visible_button_texts(page)
        self.assertIn("返回并开始新任务", button_texts)
        self.assertNotIn("继续翻译", button_texts)
        self.assertNotIn("停止提交新页", button_texts)
        self.assertNotIn("终止翻译", button_texts)

    def test_pdf_reset_ignores_stale_terminal_guard(self) -> None:
        with patch("native_app.pages.pdf_translate.count_diagnostic_records", return_value=0):
            page = PdfTranslatePage(AppSettings())
        self.addCleanup(page.close)
        self.addCleanup(page.deleteLater)

        page.phase = "done"
        page.done = self._done_msg()
        page._render_action_card()
        self.assertIn("返回并开始新任务", self._visible_button_texts(page))

        page._reset_task()
        page._workspace_render_phase = "done"
        page._sync_action_card_with_workspace()

        button_texts = self._visible_button_texts(page)
        self.assertEqual(page.phase, "idle")
        self.assertTrue(any(text.startswith("开始翻译（") for text in button_texts))
        self.assertNotIn("返回并开始新任务", button_texts)

    def test_translation_list_button_toggles_workspace_and_action_state(self) -> None:
        cases = [
            (
                WordTranslatePage,
                "native_app.pages.word_translate.count_diagnostic_records",
                TRANSLATION_TYPE_WORD,
            ),
            (
                ExcelTranslatePage,
                "native_app.pages.excel_translate.count_diagnostic_records",
                TRANSLATION_TYPE_EXCEL,
            ),
            (
                PdfTranslatePage,
                "native_app.pages.pdf_translate.count_diagnostic_records",
                TRANSLATION_TYPE_PDF,
            ),
        ]
        for page_cls, count_patch, translation_type in cases:
            with self.subTest(translation_type=translation_type):
                with patch(count_patch, return_value=0):
                    page = page_cls(AppSettings())
                self.addCleanup(page.close)
                self.addCleanup(page.deleteLater)
                self._arrange_completed_queue_task(page, translation_type)
                self._arrange_completed_queue_task(page, translation_type)
                page.phase = "done"
                page.done = self._done_msg()
                page._render_workspace()

                self._queue_button(page).click()

                self.assertTrue(page.translation_list_open)
                self.assertIn("关闭翻译列表", self._visible_button_texts(page))
                labels = [label.text() for label in page.workspace_frame.findChildren(QLabel)]
                self.assertIn("翻译列表", labels)
                self.assertIn("历史", labels)

                close = next(
                    button
                    for button in page.action_card.findChildren(QPushButton)
                    if button.text() == "关闭翻译列表"
                )
                close.click()

                self.assertFalse(page.translation_list_open)
                self.assertIn("查看翻译列表", self._visible_button_texts(page))
                labels = [label.text() for label in page.workspace_frame.findChildren(QLabel)]
                self.assertNotIn("翻译列表", labels)

    def test_reset_starts_new_lifecycle_and_hides_completed_queue_history(self) -> None:
        cases = [
            (
                WordTranslatePage,
                "native_app.pages.word_translate.count_diagnostic_records",
                TRANSLATION_TYPE_WORD,
            ),
            (
                ExcelTranslatePage,
                "native_app.pages.excel_translate.count_diagnostic_records",
                TRANSLATION_TYPE_EXCEL,
            ),
            (
                PdfTranslatePage,
                "native_app.pages.pdf_translate.count_diagnostic_records",
                TRANSLATION_TYPE_PDF,
            ),
        ]
        for page_cls, count_patch, translation_type in cases:
            with self.subTest(translation_type=translation_type):
                with patch(count_patch, return_value=0):
                    page = page_cls(AppSettings())
                self.addCleanup(page.close)
                self.addCleanup(page.deleteLater)
                self._arrange_completed_queue_task(page, translation_type)
                self._arrange_completed_queue_task(page, translation_type)
                page.phase = "done"
                page.done = self._done_msg()
                page._render_workspace()
                self.assertTrue(self._has_queue_entry(page))

                page._reset_task()

                self.assertEqual(page.phase, "idle")
                self.assertFalse(page.translation_list_open)
                self.assertFalse(self._has_queue_entry(page))
                self.assertFalse(page.queue_controller.queue.tasks())

    def test_single_running_task_hides_translation_list_until_next_task_is_arranged(self) -> None:
        cases = [
            (
                WordTranslatePage,
                "native_app.pages.word_translate.count_diagnostic_records",
                TRANSLATION_TYPE_WORD,
                "终止翻译",
            ),
            (
                ExcelTranslatePage,
                "native_app.pages.excel_translate.count_diagnostic_records",
                TRANSLATION_TYPE_EXCEL,
                "终止翻译",
            ),
            (
                PdfTranslatePage,
                "native_app.pages.pdf_translate.count_diagnostic_records",
                TRANSLATION_TYPE_PDF,
                "停止提交新页",
            ),
        ]
        for page_cls, count_patch, translation_type, stop_label in cases:
            with self.subTest(translation_type=translation_type):
                with patch(count_patch, return_value=0):
                    page = page_cls(AppSettings())
                self.addCleanup(page.close)
                self.addCleanup(page.deleteLater)
                self._arrange_running_queue_task(page, translation_type)
                page.phase = "running"
                page.runner = object()
                page.done = None
                page._render_action_card()

                self.assertFalse(page.translation_list_open)
                self.assertFalse(self._has_queue_entry(page))
                button_texts = self._visible_button_texts(page)
                self.assertIn("安排新任务", button_texts)
                self.assertIn(stop_label, button_texts)

                self._arrange_running_queue_task(page, translation_type)
                page._render_action_card()

                self.assertFalse(page.translation_list_open)
                button_texts = self._visible_button_texts(page)
                self.assertIn("查看翻译列表，当前 1/2", button_texts)
                self.assertIn("安排新任务", button_texts)
                self.assertIn(stop_label, button_texts)

    def test_running_arrange_next_button_uses_primary_style(self) -> None:
        cases = [
            (
                WordTranslatePage,
                "native_app.pages.word_translate.count_diagnostic_records",
                TRANSLATION_TYPE_WORD,
            ),
            (
                ExcelTranslatePage,
                "native_app.pages.excel_translate.count_diagnostic_records",
                TRANSLATION_TYPE_EXCEL,
            ),
            (
                PdfTranslatePage,
                "native_app.pages.pdf_translate.count_diagnostic_records",
                TRANSLATION_TYPE_PDF,
            ),
        ]
        for page_cls, count_patch, translation_type in cases:
            with self.subTest(translation_type=translation_type):
                with patch(count_patch, return_value=0):
                    page = page_cls(AppSettings())
                self.addCleanup(page.close)
                self.addCleanup(page.deleteLater)
                self._arrange_running_queue_task(page, translation_type)
                page.phase = "running"
                page.runner = object()
                page._render_action_card()

                arrange_button = next(
                    button
                    for button in page.action_card.findChildren(QPushButton)
                    if button.text() == "安排新任务"
                )
                self.assertEqual(arrange_button.objectName(), "PrimaryButton")

    def test_prepare_next_task_clears_previous_file_list_workspace(self) -> None:
        cases = [
            (
                WordTranslatePage,
                "native_app.pages.word_translate.count_diagnostic_records",
                TRANSLATION_TYPE_WORD,
            ),
            (
                ExcelTranslatePage,
                "native_app.pages.excel_translate.count_diagnostic_records",
                TRANSLATION_TYPE_EXCEL,
            ),
            (
                PdfTranslatePage,
                "native_app.pages.pdf_translate.count_diagnostic_records",
                TRANSLATION_TYPE_PDF,
            ),
        ]
        for page_cls, count_patch, translation_type in cases:
            with self.subTest(translation_type=translation_type):
                with patch(count_patch, return_value=0):
                    page = page_cls(AppSettings())
                self.addCleanup(page.close)
                self.addCleanup(page.deleteLater)
                page.files = [self._sample_scan_item(translation_type)]
                page.phase = "running"
                page.runner = object()

                page._prepare_next_task()

                self.assertEqual(page.files, [])
                labels = [label.text() for label in page.workspace_frame.findChildren(QLabel)]
                self.assertIn("任务清单", labels)
                self.assertIsNone(page.workspace_frame.findChild(QTableWidget))

    def test_prepare_next_task_shows_start_and_cancel_only(self) -> None:
        cases = [
            (
                WordTranslatePage,
                "native_app.pages.word_translate.count_diagnostic_records",
                TRANSLATION_TYPE_WORD,
            ),
            (
                ExcelTranslatePage,
                "native_app.pages.excel_translate.count_diagnostic_records",
                TRANSLATION_TYPE_EXCEL,
            ),
            (
                PdfTranslatePage,
                "native_app.pages.pdf_translate.count_diagnostic_records",
                TRANSLATION_TYPE_PDF,
            ),
        ]
        for page_cls, count_patch, translation_type in cases:
            with self.subTest(translation_type=translation_type):
                with patch(count_patch, return_value=0):
                    page = page_cls(AppSettings())
                self.addCleanup(page.close)
                self.addCleanup(page.deleteLater)
                self._arrange_running_queue_task(page, translation_type)
                page.phase = "running"
                page.runner = object()

                page._prepare_next_task()

                button_texts = self._visible_button_texts(page)
                self.assertIn(f"开始翻译（{page._selected_target_label()}）", button_texts)
                self.assertIn("取消安排", button_texts)
                self.assertNotIn("终止翻译", button_texts)
                self.assertNotIn("停止提交新页", button_texts)
                self.assertNotIn("继续翻译", button_texts)
                self.assertNotIn("安排新任务", button_texts)

    def test_page_activation_restores_prepare_next_action_card(self) -> None:
        cases = [
            (
                WordTranslatePage,
                "native_app.pages.word_translate.count_diagnostic_records",
                TRANSLATION_TYPE_WORD,
            ),
            (
                ExcelTranslatePage,
                "native_app.pages.excel_translate.count_diagnostic_records",
                TRANSLATION_TYPE_EXCEL,
            ),
            (
                PdfTranslatePage,
                "native_app.pages.pdf_translate.count_diagnostic_records",
                TRANSLATION_TYPE_PDF,
            ),
        ]
        for page_cls, count_patch, translation_type in cases:
            with self.subTest(translation_type=translation_type):
                with patch(count_patch, return_value=0):
                    page = page_cls(AppSettings())
                self.addCleanup(page.close)
                self.addCleanup(page.deleteLater)
                self._arrange_running_queue_task(page, translation_type)
                page.phase = "running"
                page.runner = object()

                page._prepare_next_task()
                clear_layout(page.action_layout)
                page.action_layout.addWidget(QLabel("执行操作"))
                page.set_page_active(True)

                button_texts = self._visible_button_texts(page)
                self.assertIn(f"开始翻译（{page._selected_target_label()}）", button_texts)
                self.assertIn("取消安排", button_texts)
                self.assertNotIn("终止翻译", button_texts)
                self.assertNotIn("停止提交新页", button_texts)
                self.assertNotIn("继续翻译", button_texts)
                self.assertNotIn("安排新任务", button_texts)

    def test_cancel_prepare_next_task_restores_running_view(self) -> None:
        cases = [
            (
                WordTranslatePage,
                "native_app.pages.word_translate.count_diagnostic_records",
                TRANSLATION_TYPE_WORD,
                "终止翻译",
            ),
            (
                ExcelTranslatePage,
                "native_app.pages.excel_translate.count_diagnostic_records",
                TRANSLATION_TYPE_EXCEL,
                "终止翻译",
            ),
            (
                PdfTranslatePage,
                "native_app.pages.pdf_translate.count_diagnostic_records",
                TRANSLATION_TYPE_PDF,
                "停止提交新页",
            ),
        ]
        for page_cls, count_patch, translation_type, stop_label in cases:
            with self.subTest(translation_type=translation_type):
                with patch(count_patch, return_value=0):
                    page = page_cls(AppSettings())
                self.addCleanup(page.close)
                self.addCleanup(page.deleteLater)
                self._arrange_running_queue_task(page, translation_type)
                page.phase = "running"
                page.runner = object()

                page._prepare_next_task()
                page._cancel_prepare_next_task()

                self.assertFalse(page.preparing_next_task)
                self.assertEqual(page.phase, "running")
                button_texts = self._visible_button_texts(page)
                self.assertIn("安排新任务", button_texts)
                self.assertIn(stop_label, button_texts)
                self.assertNotIn("取消安排", button_texts)

    def test_arranging_queued_task_restores_running_view_and_close_list_keeps_it(self) -> None:
        cases = [
            (
                WordTranslatePage,
                "native_app.pages.word_translate.count_diagnostic_records",
                "native_app.pages.word_translate.check_translation_api_config",
                "native_app.pages.word_translate.save_settings",
                "native_app.pages.word_translate.QMessageBox.warning",
                "native_app.pages.word_translate.WordTaskRunner",
                TRANSLATION_TYPE_WORD,
            ),
            (
                ExcelTranslatePage,
                "native_app.pages.excel_translate.count_diagnostic_records",
                "native_app.pages.excel_translate.check_translation_api_config",
                "native_app.pages.excel_translate.save_settings",
                "native_app.pages.excel_translate.QMessageBox.warning",
                "native_app.pages.excel_translate.TaskRunner",
                TRANSLATION_TYPE_EXCEL,
            ),
        ]
        for (
            page_cls,
            count_patch,
            config_patch,
            save_patch,
            warning_patch,
            runner_patch,
            translation_type,
        ) in cases:
            with self.subTest(translation_type=translation_type):
                with (
                    patch(count_patch, return_value=0),
                    patch(config_patch, return_value=_OkApiCheck()),
                    patch(save_patch),
                    patch(
                        warning_patch,
                        side_effect=AssertionError(
                            "queue-ready test should not show a warning dialog"
                        ),
                    ),
                    patch("core.model_roles.get_key", return_value="sk-test-key"),
                    patch(runner_patch, _LiveFakeRunner),
                ):
                    page = page_cls(self._queue_ready_settings())
                    self.addCleanup(page.close)
                    self.addCleanup(page.deleteLater)
                    self.addCleanup(self._stop_page_runtime, page)
                    page.set_queue_controller(NativeTranslationQueueController())

                    page.files = [self._sample_scan_item(translation_type)]
                    page.source_root = "/tmp/source-a"
                    page._start_translation()

                    self.assertEqual(page.phase, "running")
                    self.assertIn("终止翻译", self._visible_button_texts(page))

                    page._prepare_next_task()
                    page.files = [self._sample_scan_item(translation_type)]
                    page.source_root = "/tmp/source-b"
                    page._start_translation()

                    button_texts = self._visible_button_texts(page)
                    self.assertEqual(page.phase, "running")
                    self.assertFalse(page.preparing_next_task)
                    self.assertIn("查看翻译列表，当前 1/2", button_texts)
                    self.assertIn("安排新任务", button_texts)
                    self.assertIn("终止翻译", button_texts)
                    self.assertNotIn("取消安排", button_texts)

                    page._toggle_translation_list()
                    self.assertTrue(page.translation_list_open)
                    page._poll_runner()
                    queued = [
                        task
                        for task in page.queue_controller.queue.tasks()
                        if task.status == "queued"
                    ]
                    self.assertEqual(len(queued), 1)

                    page._cancel_queue_task(queued[0].task_id)
                    page._clear_queue_history()
                    page._toggle_translation_list()

                    button_texts = self._visible_button_texts(page)
                    self.assertFalse(page.translation_list_open)
                    self.assertEqual(page.phase, "running")
                    self.assertIn("安排新任务", button_texts)
                    self.assertIn("终止翻译", button_texts)
                    self.assertFalse(
                        any(text.startswith("查看翻译列表") for text in button_texts)
                    )

    def test_prepare_next_scan_keeps_current_runtime_and_shows_new_task_table(self) -> None:
        cases = [
            (
                WordTranslatePage,
                "native_app.pages.word_translate.count_diagnostic_records",
                "native_app.pages.word_translate.save_settings",
                TRANSLATION_TYPE_WORD,
            ),
            (
                ExcelTranslatePage,
                "native_app.pages.excel_translate.count_diagnostic_records",
                "native_app.pages.excel_translate.save_settings",
                TRANSLATION_TYPE_EXCEL,
            ),
            (
                PdfTranslatePage,
                "native_app.pages.pdf_translate.count_diagnostic_records",
                "native_app.pages.pdf_translate.save_settings",
                TRANSLATION_TYPE_PDF,
            ),
        ]
        for page_cls, count_patch, save_patch, translation_type in cases:
            with self.subTest(translation_type=translation_type):
                with patch(count_patch, return_value=0):
                    page = page_cls(AppSettings())
                self.addCleanup(page.close)
                self.addCleanup(page.deleteLater)
                running_task = self._arrange_running_queue_task(page, translation_type)
                running_files = [self._sample_scan_item(translation_type)]
                page.current_task_id = "current-runtime"
                page.current_queue_task_id = running_task.task_id
                page.task_files = list(running_files)
                page.phase = "running"
                page.runner = object()

                page._prepare_next_task()
                with patch(save_patch):
                    page._on_scan_finished(
                        [self._sample_scan_item(translation_type)],
                        "/tmp/new-source",
                        "",
                    )

                table = page.workspace_frame.findChild(QTableWidget)
                self.assertIsNotNone(table)
                self.assertEqual(table.rowCount(), 1)
                self.assertEqual(page.current_task_id, "current-runtime")
                self.assertEqual(page.current_queue_task_id, running_task.task_id)
                self.assertEqual(page.task_files, running_files)
                self.assertIn("取消安排", self._visible_button_texts(page))

    def test_completed_original_task_during_prepare_next_shows_single_result_entry(self) -> None:
        class FakeRunner:
            task_id = "fake-running"

            def __init__(self, done: DoneMsg) -> None:
                self._messages = [done]

            def get_message(self, timeout: float = 0.0):
                return self._messages.pop(0) if self._messages else None

            def needs_poll(self) -> bool:
                return bool(self._messages)

        cases = [
            (
                WordTranslatePage,
                "native_app.pages.word_translate.count_diagnostic_records",
                "native_app.pages.word_translate.archive_task_diagnostics",
                TRANSLATION_TYPE_WORD,
            ),
            (
                ExcelTranslatePage,
                "native_app.pages.excel_translate.count_diagnostic_records",
                "native_app.pages.excel_translate.archive_task_diagnostics",
                TRANSLATION_TYPE_EXCEL,
            ),
            (
                PdfTranslatePage,
                "native_app.pages.pdf_translate.count_diagnostic_records",
                "native_app.pages.pdf_translate.archive_task_diagnostics",
                TRANSLATION_TYPE_PDF,
            ),
        ]
        for page_cls, count_patch, archive_patch, translation_type in cases:
            with self.subTest(translation_type=translation_type):
                with (
                    patch(count_patch, return_value=0),
                    patch(archive_patch),
                    patch("native_app.pages.pdf_translate.save_settings"),
                ):
                    page = page_cls(AppSettings())
                    self.addCleanup(page.close)
                    self.addCleanup(page.deleteLater)
                    task = self._arrange_running_queue_task(page, translation_type)
                    page.current_queue_task_id = task.task_id
                    page.phase = "running"
                    page.runner = FakeRunner(self._done_msg())
                    page.files = [self._sample_scan_item(translation_type)]
                    page._prepare_next_task()

                    page._poll_runner()

                self.assertEqual(page.phase, "idle")
                self.assertIsNone(page.done)
                self.assertEqual(
                    [text for text in self._visible_button_texts(page) if "上一轮任务" in text],
                    ["上一轮任务已完成，点击查看"],
                )

                result_button = next(
                    button
                    for button in page.action_card.findChildren(QPushButton)
                    if button.text() == "上一轮任务已完成，点击查看"
                )
                result_button.click()

                self.assertEqual(page.phase, "done")
                self.assertIsNotNone(page.done)
                self.assertIn("返回并开始新任务", self._visible_button_texts(page))

    def test_running_queue_task_completion_shows_result_immediately(self) -> None:
        class FakeRunner:
            task_id = "fake-running"

            def __init__(self, done: DoneMsg) -> None:
                self._messages = [done]

            def get_message(self, timeout: float = 0.0):
                return self._messages.pop(0) if self._messages else None

            def needs_poll(self) -> bool:
                return bool(self._messages)

        cases = [
            (
                WordTranslatePage,
                "native_app.pages.word_translate.count_diagnostic_records",
                "native_app.pages.word_translate.archive_task_diagnostics",
                TRANSLATION_TYPE_WORD,
            ),
            (
                ExcelTranslatePage,
                "native_app.pages.excel_translate.count_diagnostic_records",
                "native_app.pages.excel_translate.archive_task_diagnostics",
                TRANSLATION_TYPE_EXCEL,
            ),
            (
                PdfTranslatePage,
                "native_app.pages.pdf_translate.count_diagnostic_records",
                "native_app.pages.pdf_translate.archive_task_diagnostics",
                TRANSLATION_TYPE_PDF,
            ),
        ]
        for page_cls, count_patch, archive_patch, translation_type in cases:
            with self.subTest(translation_type=translation_type):
                with (
                    patch(count_patch, return_value=0),
                    patch(archive_patch),
                    patch("native_app.pages.pdf_translate.save_settings"),
                ):
                    page = page_cls(AppSettings())
                    self.addCleanup(page.close)
                    self.addCleanup(page.deleteLater)
                    task = self._arrange_running_queue_task(page, translation_type)
                    page.current_queue_task_id = task.task_id
                    page.phase = "running"
                    page.runner = FakeRunner(self._done_msg())

                    page._poll_runner()

                self.assertEqual(page.phase, "done")
                self.assertIsNotNone(page.done)
                self.assertEqual(page.deferred_terminal_phase, "")
                self.assertIn("返回并开始新任务", self._visible_button_texts(page))

    def test_scan_result_closes_translation_list_and_restores_task_table(self) -> None:
        cases = [
            (
                WordTranslatePage,
                "native_app.pages.word_translate.count_diagnostic_records",
                "native_app.pages.word_translate.save_settings",
                TRANSLATION_TYPE_WORD,
            ),
            (
                ExcelTranslatePage,
                "native_app.pages.excel_translate.count_diagnostic_records",
                "native_app.pages.excel_translate.save_settings",
                TRANSLATION_TYPE_EXCEL,
            ),
            (
                PdfTranslatePage,
                "native_app.pages.pdf_translate.count_diagnostic_records",
                "native_app.pages.pdf_translate.save_settings",
                TRANSLATION_TYPE_PDF,
            ),
        ]
        for page_cls, count_patch, save_patch, translation_type in cases:
            with self.subTest(translation_type=translation_type):
                with patch(count_patch, return_value=0):
                    page = page_cls(AppSettings())
                self.addCleanup(page.close)
                self.addCleanup(page.deleteLater)
                self._arrange_completed_queue_task(page, translation_type)
                self._arrange_completed_queue_task(page, translation_type)
                page.phase = "done"
                page.done = self._done_msg()
                page._render_workspace()
                self._queue_button(page).click()
                self.assertTrue(page.translation_list_open)

                with patch(save_patch):
                    page._on_scan_finished(
                        [self._sample_scan_item(translation_type)],
                        "/tmp",
                        "",
                    )

                self.assertEqual(page.phase, "idle")
                self.assertFalse(page.translation_list_open)
                self.assertIsNone(page.done)
                self.assertFalse(self._has_queue_entry(page))
                labels = [label.text() for label in page.workspace_frame.findChildren(QLabel)]
                self.assertIn("任务清单", labels)
                self.assertNotIn("翻译列表", labels)
                table = page.workspace_frame.findChild(QTableWidget)
                self.assertIsNotNone(table)
                self.assertEqual(table.rowCount(), 1)

    def test_pdf_stopped_workspace_shows_paths_and_file_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "pdf_out"
            output_dir.mkdir()
            report_path = output_dir / "pdf_translation_report.md"
            manifest_path = output_dir / "pdf_translation_manifest.json"
            report_path.write_text("# report", encoding="utf-8")
            manifest_path.write_text(
                json.dumps(
                    {
                        "elapsed_sec": 65,
                        "total_page_count": 4,
                        "files": [
                            {
                                "name": "first.pdf",
                                "relative_path": "first.pdf",
                                "status": "completed",
                                "translated_pdf_path": str(output_dir / "译文(英文)_first_高清.pdf"),
                                "compressed_pdf_path": "",
                            },
                            {
                                "name": "second.pdf",
                                "relative_path": "folder/second.pdf",
                                "status": "stopped",
                                "translated_pdf_path": "",
                                "compressed_pdf_path": "",
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with patch("native_app.pages.pdf_translate.count_diagnostic_records", return_value=0):
                page = PdfTranslatePage(AppSettings())
            self.addCleanup(page.close)
            self.addCleanup(page.deleteLater)

            page.phase = "stopped"
            page.stop_message = "PDF 翻译已中止"
            page._terminal_output_dir = str(output_dir)
            page._terminal_report_path = str(report_path)
            page._terminal_manifest_path = str(manifest_path)
            page._render_workspace()

            labels = [label.text() for label in page.workspace_frame.findChildren(QLabel)]
            self.assertIn("报告位置", labels)
            self.assertIn("页面素材", labels)
            self.assertNotIn("源页素材", labels)
            self.assertNotIn("译后页素材", labels)

            path_fields = [
                field.text()
                for field in page.workspace_frame.findChildren(QLineEdit)
            ]
            self.assertIn(str(report_path), path_fields)
            self.assertIn(str(output_dir / "_pdf_pages"), path_fields)
            for field in page.workspace_frame.findChildren(QLineEdit):
                self.assertIsInstance(field, MiddleElideLineEdit)

            table = page.workspace_frame.findChild(QTableWidget)
            self.assertIsNotNone(table)
            table_text = " ".join(
                table.item(row, column).text()
                for row in range(table.rowCount())
                for column in range(table.columnCount())
                if table.item(row, column) is not None
            )
            self.assertIn("已完成 PDF", table_text)
            self.assertIn("未完成 PDF", table_text)

            with patch("native_app.pages.pdf_translate.QMessageBox.information") as information:
                page._copy_text(str(report_path))
            self.assertFalse(information.called)
            self.assertEqual(QApplication.clipboard().text(), str(report_path))

    def test_pdf_history_export_is_disabled_while_running(self) -> None:
        class FakeRunner:
            def stop_requested(self) -> bool:
                return False

        with patch("native_app.pages.pdf_translate.count_diagnostic_records", return_value=2):
            page = PdfTranslatePage(AppSettings())
        self.addCleanup(page.close)
        self.addCleanup(page.deleteLater)
        page.phase = "running"
        page.runner = FakeRunner()
        page._render_action_card()

        history_buttons = [
            button
            for button in page.action_card.findChildren(QPushButton)
            if button.objectName() == "PdfHistoryDiagnosticsButton"
        ]
        self.assertEqual(len(history_buttons), 1)
        self.assertEqual(history_buttons[0].text(), "导出历史诊断归档")
        self.assertFalse(history_buttons[0].isEnabled())

    def test_pdf_concurrency_input_clamps_to_safety_cap(self) -> None:
        with (
            patch("native_app.pages.pdf_translate.count_diagnostic_records", return_value=0),
            patch("native_app.pages.pdf_translate.save_settings"),
        ):
            page = PdfTranslatePage(AppSettings())
            self.addCleanup(page.close)
            self.addCleanup(page.deleteLater)

            page.pdf_concurrency_input.setText("99")
            page._on_params_changed()

        self.assertEqual(page.settings.pdf.page_generation_concurrency, 20)
        self.assertEqual(page.pdf_concurrency_input.text(), "20")

    def test_pdf_review_checkbox_requires_configured_review_model(self) -> None:
        with (
            patch("native_app.pages.pdf_translate.count_diagnostic_records", return_value=0),
            patch("native_app.pages.pdf_translate.save_settings"),
            patch.object(PdfTranslatePage, "_is_pdf_review_model_configured", return_value=False),
            patch.object(PdfTranslatePage, "_prompt_configure_pdf_review_model") as prompt,
        ):
            page = PdfTranslatePage(AppSettings())
            self.addCleanup(page.close)
            self.addCleanup(page.deleteLater)

            page.pdf_review_checkbox.setChecked(True)

        self.assertFalse(page.settings.pdf.review_enabled)
        self.assertFalse(page.pdf_review_checkbox.isChecked())
        self.assertTrue(prompt.called)

    def test_pdf_done_poll_persists_runtime_model_state(self) -> None:
        class FakeRunner:
            task_id = "fake-pdf"

            def __init__(self, done: DoneMsg) -> None:
                self._messages = [done]

            def get_message(self, timeout: float = 0.0):
                return self._messages.pop(0) if self._messages else None

            def needs_poll(self) -> bool:
                return bool(self._messages)

        with (
            patch("native_app.pages.pdf_translate.count_diagnostic_records", return_value=0),
            patch("native_app.pages.pdf_translate.archive_task_diagnostics"),
            patch("native_app.pages.pdf_translate.save_settings") as save_settings,
        ):
            page = PdfTranslatePage(AppSettings())
            self.addCleanup(page.close)
            self.addCleanup(page.deleteLater)

            page.phase = "running"
            page.runner = FakeRunner(self._done_msg())
            page._poll_runner()

        self.assertTrue(save_settings.called)

    def test_pdf_action_card_does_not_offer_stop_after_done_payload(self) -> None:
        with patch("native_app.pages.pdf_translate.count_diagnostic_records", return_value=0):
            page = PdfTranslatePage(AppSettings())
        self.addCleanup(page.close)
        self.addCleanup(page.deleteLater)

        page.phase = "running"
        page.runner = object()
        page.done = self._done_msg()
        page._render_action_card()

        button_texts = self._button_texts(page)
        self.assertIn("返回并开始新任务", button_texts)
        self.assertNotIn("停止提交新页", button_texts)
        self.assertNotIn("终止翻译", button_texts)

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
        self.assertIsNone(tables[0].item(0, 1))
        self.assertIsNone(tables[0].item(0, 2))
        self.assertEqual(tables[0].cellWidget(0, 1).fullText(), "source.docx")
        self.assertEqual(tables[0].cellWidget(0, 2).fullText(), "正文段落 3")
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

    def test_shared_result_view_hides_error_column_when_only_detail_exists(self) -> None:
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
                        "name": "source.pdf",
                        "success": True,
                        "error": "",
                        "detail": "高清版 / 压缩版 / 成功",
                    }
                ],
                elapsed_sec=0,
                tm_hit_count=0,
                api_call_count=1,
            ),
            summary_text="翻译成功。",
            summary_success=True,
            kpi_items=[("高清 PDF", "1")],
            file_status_formatter=lambda result: "高清版 / 压缩版 / 成功",
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
