from __future__ import annotations

import atexit
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QCheckBox, QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget, QVBoxLayout, QWidget

from core.mixed_language import MIXED_MARK_UNRESOLVED
from core.task_runner import DoneMsg, PdfPageRecoveryStatusMsg, ProgressMsg
from native_app.pages.excel_translate import ExcelTranslatePage
from native_app.pages.pdf_translate import PdfTranslatePage
from native_app.pages.word_translate import WordTranslatePage
from native_app.result_view import ResultIssueRow, render_translation_result
from native_app.widgets import MiddleElideLineEdit
from settings import AppSettings


try:
    import xlwings._xlmac as _xlwings_mac

    atexit.unregister(_xlwings_mac.cleanup)
except Exception:
    pass


class NativeTranslationPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        super().setUp()
        for target in (
            "native_app.pages.excel_translate.count_diagnostic_records",
            "native_app.pages.word_translate.count_diagnostic_records",
            "native_app.pages.pdf_translate.count_diagnostic_records",
        ):
            patcher = patch(target, return_value=0)
            patcher.start()
            self.addCleanup(patcher.stop)

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

    def test_running_action_cards_offer_only_current_task_controls(self) -> None:
        class FakePdfRunner:
            def __init__(self, stopped: bool = False) -> None:
                self.stopped = stopped

            def stop_requested(self) -> bool:
                return self.stopped

        with (
            patch("native_app.pages.excel_translate.count_diagnostic_records", return_value=0),
            patch("native_app.pages.word_translate.count_diagnostic_records", return_value=0),
            patch("native_app.pages.pdf_translate.count_diagnostic_records", return_value=0),
        ):
            excel_page = ExcelTranslatePage(AppSettings())
            word_page = WordTranslatePage(AppSettings())
            pdf_page = PdfTranslatePage(AppSettings())
            pdf_stopped_page = PdfTranslatePage(AppSettings())
        for page in (excel_page, word_page, pdf_page, pdf_stopped_page):
            self.addCleanup(page.close)
            self.addCleanup(page.deleteLater)
            page.phase = "running"

        excel_page.runner = object()
        excel_page._render_action_card()
        self.assertEqual(self._visible_button_texts(excel_page), ["终止翻译", "暂无历史诊断"])

        word_page.runner = object()
        word_page._render_action_card()
        self.assertEqual(self._visible_button_texts(word_page), ["终止翻译", "暂无历史诊断"])

        pdf_page.runner = FakePdfRunner()
        pdf_page._render_action_card()
        self.assertEqual(self._visible_button_texts(pdf_page), ["停止提交新页", "暂无历史诊断"])

        pdf_stopped_page.runner = FakePdfRunner(stopped=True)
        pdf_stopped_page._render_action_card()
        self.assertEqual(self._visible_button_texts(pdf_stopped_page), ["继续翻译", "暂无历史诊断"])

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

    def test_word_review_color_options_show_highlight_background(self) -> None:
        with patch("native_app.pages.word_translate.count_diagnostic_records", return_value=0):
            page = WordTranslatePage(AppSettings())
        self.addCleanup(page.close)
        self.addCleanup(page.deleteLater)

        combo = page.review_color_combos[MIXED_MARK_UNRESOLVED]
        default_index = combo.findData("FCE4D6")
        brush = combo.itemData(default_index, Qt.ItemDataRole.BackgroundRole)
        foreground = combo.itemData(default_index, Qt.ItemDataRole.ForegroundRole)

        self.assertEqual(combo.currentIndex(), default_index)
        self.assertEqual(combo.itemText(default_index), "浅橙（默认） #FCE4D6")
        self.assertFalse(any(combo.itemText(index).startswith("默认 ") for index in range(combo.count())))
        self.assertIn("保留原文复核", combo.toolTip())
        self.assertNotIn("保留原文需复核", combo.toolTip())
        self.assertEqual(brush.color().name().upper(), "#FCE4D6")
        self.assertEqual(foreground.color().name().upper(), "#111827")
        self.assertFalse(page.review_color_inputs[MIXED_MARK_UNRESOLVED].isVisible())
        self.assertTrue(page.review_color_buttons[MIXED_MARK_UNRESOLVED].isHidden())

        combo.addItem("深色 #000000", "000000")
        page._set_combo_item_highlight(combo, combo.count() - 1, "000000")
        foreground = combo.itemData(combo.count() - 1, Qt.ItemDataRole.ForegroundRole)

        self.assertEqual(foreground.color().name().upper(), "#FFFFFF")

    def test_excel_review_color_options_show_highlight_background(self) -> None:
        with (
            patch("native_app.pages.excel_translate.count_diagnostic_records", return_value=0),
            patch("native_app.pages.excel_translate.save_settings"),
        ):
            page = ExcelTranslatePage(AppSettings())

            self.addCleanup(page.close)
            self.addCleanup(page.deleteLater)

            combo = page.review_color_combos[MIXED_MARK_UNRESOLVED]
            default_index = combo.findData("FCE4D6")
            brush = combo.itemData(default_index, Qt.ItemDataRole.BackgroundRole)
            custom_index = combo.findData("__custom__")

            self.assertEqual(combo.currentIndex(), default_index)
            self.assertEqual(combo.itemText(default_index), "浅橙（默认） #FCE4D6")
            self.assertFalse(any(combo.itemText(index).startswith("默认 ") for index in range(combo.count())))
            self.assertIn("保留原文复核", combo.toolTip())
            self.assertNotIn("保留原文需复核", combo.toolTip())
            self.assertEqual(brush.color().name().upper(), "#FCE4D6")
            self.assertFalse(page.review_color_inputs[MIXED_MARK_UNRESOLVED].isVisible())
            self.assertTrue(page.review_color_buttons[MIXED_MARK_UNRESOLVED].isHidden())

            combo.setCurrentIndex(custom_index)
            page.review_color_inputs[MIXED_MARK_UNRESOLVED].setText("#000000")
            page._on_params_changed()

            self.assertEqual(
                page.settings.word_review.mark_colors[MIXED_MARK_UNRESOLVED],
                "000000",
            )
            self.assertFalse(page.review_color_buttons[MIXED_MARK_UNRESOLVED].isHidden())

            with patch(
                "native_app.pages.excel_translate.QColorDialog.getColor",
                return_value=QColor("#336699"),
            ):
                page._choose_review_custom_color(MIXED_MARK_UNRESOLVED)

            self.assertEqual(
                page.settings.word_review.mark_colors[MIXED_MARK_UNRESOLVED],
                "336699",
            )
            self.assertEqual(
                page.review_color_inputs[MIXED_MARK_UNRESOLVED].text(),
                "#336699",
            )

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
            if label.text() == "PDF 翻译"
        ]
        self.assertEqual(len(title_labels), 1)
        self.assertEqual(page.retry_spin.value(), 3)
        retry_labels = [
            label for label in page.findChildren(QLabel)
            if label.text() == "页级重试次数"
        ]
        self.assertEqual(len(retry_labels), 1)
        self.assertIn("设置单页失败后的重试次数", retry_labels[0].toolTip())
        pdf_concurrency_labels = [
            label for label in page.findChildren(QLabel)
            if label.text() == "PDF 页生成并发数"
        ]
        self.assertEqual(pdf_concurrency_labels, [])
        compression_checks = [
            checkbox
            for checkbox in page.findChildren(QCheckBox)
            if checkbox.text() == "同时生成压缩 PDF（推荐）"
        ]
        self.assertEqual(len(compression_checks), 1)
        self.assertTrue(compression_checks[0].isChecked())
        self.assertIn("关闭后仅输出高清版", compression_checks[0].toolTip())
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
        self.assertIn("已停止提交新页，可继续翻译。", action_labels)

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

        page = PdfTranslatePage(AppSettings())
        self.addCleanup(page.close)
        self.addCleanup(page.deleteLater)
        page.phase = "running"
        page.runner = FakeRunner()
        with patch("native_app.pages.pdf_translate.count_diagnostic_records", return_value=2):
            page._render_action_card()

        history_buttons = [
            button
            for button in page.action_card.findChildren(QPushButton)
            if button.objectName() == "PdfHistoryDiagnosticsButton"
        ]
        self.assertEqual(len(history_buttons), 1)
        self.assertEqual(history_buttons[0].text(), "导出历史诊断归档")
        self.assertFalse(history_buttons[0].isEnabled())

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
