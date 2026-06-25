from __future__ import annotations

import atexit
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QCheckBox, QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget, QVBoxLayout, QWidget

from core.file_scanner import FileItem
from core import tm_manager as tm_store
from core.mixed_language import MIXED_MARK_UNRESOLVED
from core.pdf_image_translation import PdfFileItem
from core.task_runner import DoneMsg, PdfPageRecoveryStatusMsg, ProgressMsg
from core.word_document import WordFileItem
from native_app.pages.excel_translate import ExcelTranslatePage
from native_app.pages.pdf_translate import PdfTranslatePage
from native_app.pages.tm_manager import TM_LANG_PAIR_TILE_MAX_WIDTH, TmManagerPage
from native_app.pages.word_translate import WordTranslatePage
from native_app.result_view import ResultIssueRow, render_translation_result
from native_app.style import APP_QSS
from native_app.widgets import AlignedComboBox, CurrentTextOverrideComboBox, MiddleElideLabel, MiddleElideLineEdit
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

    def _wait_for_qt_timers(self, delay_ms: int = 250) -> None:
        self.app.processEvents()
        QTimer.singleShot(delay_ms, self.app.quit)
        self.app.exec()
        self.app.processEvents()

    def _make_tm_page(self, settings: AppSettings | None = None) -> TmManagerPage:
        temp_dir = tempfile.TemporaryDirectory()
        old_db_path = tm_store.DB_PATH
        tm_store.DB_PATH = Path(temp_dir.name) / "tm.db"
        self.addCleanup(temp_dir.cleanup)
        self.addCleanup(lambda: setattr(tm_store, "DB_PATH", old_db_path))
        page = TmManagerPage(settings or AppSettings())
        self.addCleanup(page.close)
        self.addCleanup(page.deleteLater)
        return page

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

    def test_excel_untranslated_only_checkbox_updates_start_button_without_persistence(self) -> None:
        with patch("native_app.pages.excel_translate.save_settings"):
            page = ExcelTranslatePage(AppSettings())
            self.addCleanup(page.close)
            self.addCleanup(page.deleteLater)

            self.assertFalse(page.untranslated_only_check.isChecked())
            self.assertTrue(
                any(
                    text.startswith("开始翻译（")
                    for text in self._button_texts(page)
                )
            )

            page.untranslated_only_check.setChecked(True)

            self.assertTrue(
                any(
                    text.startswith("开始补译未译内容（")
                    for text in self._button_texts(page)
                )
            )

            fresh_page = ExcelTranslatePage(page.settings)
            self.addCleanup(fresh_page.close)
            self.addCleanup(fresh_page.deleteLater)
            self.assertFalse(fresh_page.untranslated_only_check.isChecked())

    def test_word_untranslated_only_checkbox_updates_start_button_without_persistence(self) -> None:
        with patch("native_app.pages.word_translate.save_settings"):
            page = WordTranslatePage(AppSettings())
            self.addCleanup(page.close)
            self.addCleanup(page.deleteLater)

            self.assertFalse(page.untranslated_only_check.isChecked())
            self.assertTrue(
                any(
                    text.startswith("开始翻译（")
                    for text in self._button_texts(page)
                )
            )

            page.untranslated_only_check.setChecked(True)

            self.assertTrue(
                any(
                    text.startswith("开始补译未译内容（")
                    for text in self._button_texts(page)
                )
            )

            fresh_page = WordTranslatePage(page.settings)
            self.addCleanup(fresh_page.close)
            self.addCleanup(fresh_page.deleteLater)
            self.assertFalse(fresh_page.untranslated_only_check.isChecked())

    def test_language_pages_keep_french_target_after_rebuild(self) -> None:
        settings = AppSettings(source_lang="zh", target_lang="fr", recent_target_langs=["fr"])

        with (
            patch("native_app.pages.excel_translate.save_settings"),
            patch("native_app.pages.word_translate.save_settings"),
            patch("native_app.pages.pdf_translate.save_settings"),
            patch("native_app.pages.tm_manager.save_settings"),
        ):
            excel_page = ExcelTranslatePage(settings)
            word_page = WordTranslatePage(settings)
            pdf_page = PdfTranslatePage(settings)
            tm_page = self._make_tm_page(settings)
        for page in (excel_page, word_page, pdf_page):
            self.addCleanup(page.close)
            self.addCleanup(page.deleteLater)

        self.assertEqual(excel_page.target_combo.currentData(), "fr")
        self.assertEqual(word_page.target_combo.currentData(), "fr")
        self.assertEqual(pdf_page.target_combo.currentData(), "fr")
        self.assertEqual(tm_page.target_combo.currentData(), "fr")
        self.assertEqual(settings.target_lang, "fr")

    def test_delayed_reset_keeps_start_action_and_allows_empty_scan(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("native_app.pages.excel_translate.save_settings"),
            patch("native_app.pages.word_translate.save_settings"),
            patch("native_app.pages.pdf_translate.save_settings"),
        ):
            root = Path(tmp)
            cases = (
                (
                    ExcelTranslatePage(AppSettings()),
                    FileItem(root / "source.xlsx", "source", 1.0, ["Sheet1"]),
                ),
                (
                    WordTranslatePage(AppSettings()),
                    WordFileItem(
                        root / "source.docx",
                        "source",
                        1.0,
                        paragraph_count=1,
                        table_count=0,
                        translatable_count=1,
                    ),
                ),
                (
                    PdfTranslatePage(AppSettings()),
                    PdfFileItem(root / "source.pdf", "source", 1.0, page_count=1),
                ),
            )
            for page, item in cases:
                with self.subTest(page=type(page).__name__):
                    self.addCleanup(page.close)
                    self.addCleanup(page.deleteLater)
                    page.files = [item]
                    page.source_root = str(root)
                    page.phase = "idle"
                    page.done = None
                    page._render_workspace()

                    page.done = self._done_msg()
                    page.phase = "done"
                    page._render_workspace()
                    page._schedule_action_card_resync()
                    reset_buttons = [
                        button
                        for button in page.action_card.findChildren(QPushButton)
                        if button.text() == "返回并开始新任务" and button.parent() is not None
                    ]
                    self.assertEqual(len(reset_buttons), 1)

                    reset_buttons[0].click()
                    self._wait_for_qt_timers()

                    button_texts = self._visible_button_texts(page)
                    self.assertEqual(page.phase, "idle")
                    self.assertTrue(any(text.startswith("开始翻译（") for text in button_texts))
                    self.assertNotIn("返回并开始新任务", button_texts)

                    page._on_scan_finished([], str(root), "")
                    self.assertTrue(
                        any(
                            text.startswith("开始翻译（")
                            for text in self._visible_button_texts(page)
                        )
                    )

    def test_reset_scan_and_rescan_never_use_stale_file_tables(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("native_app.pages.excel_translate.save_settings"),
            patch("native_app.pages.word_translate.save_settings"),
            patch("native_app.pages.pdf_translate.save_settings"),
        ):
            root = Path(tmp)
            cases = (
                (
                    ExcelTranslatePage(AppSettings()),
                    FileItem(root / "source.xlsx", "source", 1.0, ["Sheet1"]),
                    FileItem(root / "next.xlsx", "next", 2.0, ["Sheet1"]),
                ),
                (
                    WordTranslatePage(AppSettings()),
                    WordFileItem(
                        root / "source.docx",
                        "source",
                        1.0,
                        paragraph_count=1,
                        table_count=0,
                        translatable_count=1,
                    ),
                    WordFileItem(
                        root / "next.docx",
                        "next",
                        2.0,
                        paragraph_count=2,
                        table_count=0,
                        translatable_count=2,
                    ),
                ),
                (
                    PdfTranslatePage(AppSettings()),
                    PdfFileItem(root / "source.pdf", "source", 1.0, page_count=1),
                    PdfFileItem(root / "next.pdf", "next", 2.0, page_count=2),
                ),
            )
            for page, first_item, next_item in cases:
                with self.subTest(page=type(page).__name__):
                    self.addCleanup(page.close)
                    self.addCleanup(page.deleteLater)
                    page.files = [first_item]
                    page.source_root = str(root)
                    page.phase = "idle"
                    page._render_workspace()
                    stale_table = page.table
                    self.assertIsNotNone(stale_table)

                    page.done = self._done_msg()
                    page.phase = "done"
                    page._render_workspace()
                    page._reset_task()
                    self.assertIsNone(page.table)
                    self.assertEqual(page._selected_files(), [])
                    self.assertTrue(
                        any(text.startswith("开始翻译（") for text in self._visible_button_texts(page))
                    )
                    self.assertNotIn("返回并开始新任务", self._visible_button_texts(page))

                    page._on_scan_finished([], str(root), "")
                    self.assertIsNone(page.table)
                    self.assertEqual(page._selected_files(), [])
                    self.assertTrue(
                        any(text.startswith("开始翻译（") for text in self._visible_button_texts(page))
                    )
                    self.assertFalse(page._can_start())

                    page._on_scan_finished([next_item], str(root), "")
                    self.assertIsNotNone(page.table)
                    self.assertIsNot(page.table, stale_table)
                    self.assertEqual(page._selected_files(), [next_item])
                    self.assertTrue(page._can_start())
                    self.assertTrue(
                        any(text.startswith("开始翻译（") for text in self._visible_button_texts(page))
                    )

    def test_file_selection_state_survives_table_rerenders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cases = (
                (
                    ExcelTranslatePage(AppSettings()),
                    [
                        FileItem(root / "first.xlsx", "first", 1.0, ["Sheet1"]),
                        FileItem(root / "second.xlsx", "second", 2.0, ["Sheet1"]),
                    ],
                ),
                (
                    WordTranslatePage(AppSettings()),
                    [
                        WordFileItem(
                            root / "first.docx",
                            "first",
                            1.0,
                            paragraph_count=1,
                            table_count=0,
                            translatable_count=1,
                        ),
                        WordFileItem(
                            root / "second.docx",
                            "second",
                            2.0,
                            paragraph_count=2,
                            table_count=0,
                            translatable_count=2,
                        ),
                    ],
                ),
                (
                    PdfTranslatePage(AppSettings()),
                    [
                        PdfFileItem(root / "first.pdf", "first", 1.0, page_count=1),
                        PdfFileItem(root / "second.pdf", "second", 2.0, page_count=2),
                    ],
                ),
            )
            for page, items in cases:
                with self.subTest(page=type(page).__name__):
                    self.addCleanup(page.close)
                    self.addCleanup(page.deleteLater)
                    page.files = page.file_selection.set_files(items, select_all=True)
                    page.source_root = str(root)
                    page.phase = "idle"
                    page._render_workspace()
                    self.assertIsNotNone(page.table)

                    page.table.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
                    self.assertEqual(page._selected_files(), [items[1]])

                    page._render_workspace()
                    self.assertEqual(page._selected_files(), [items[1]])
                    self.assertEqual(
                        page.table.item(0, 0).checkState(),
                        Qt.CheckState.Unchecked,
                    )
                    self.assertEqual(
                        page.table.item(1, 0).checkState(),
                        Qt.CheckState.Checked,
                    )

    def test_delayed_action_card_resync_ignores_stale_workspace_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cases = (
                (
                    ExcelTranslatePage(AppSettings()),
                    FileItem(root / "source.xlsx", "source", 1.0, ["Sheet1"]),
                ),
                (
                    WordTranslatePage(AppSettings()),
                    WordFileItem(
                        root / "source.docx",
                        "source",
                        1.0,
                        paragraph_count=1,
                        table_count=0,
                        translatable_count=1,
                    ),
                ),
                (
                    PdfTranslatePage(AppSettings()),
                    PdfFileItem(root / "source.pdf", "source", 1.0, page_count=1),
                ),
            )
            for page, item in cases:
                with self.subTest(page=type(page).__name__):
                    self.addCleanup(page.close)
                    self.addCleanup(page.deleteLater)
                    page.files = page.file_selection.set_files([item], select_all=True)
                    page.source_root = str(root)
                    page.phase = "done"
                    page.done = self._done_msg()
                    page._render_workspace()

                    calls = 0
                    original_render_action_card = page._render_action_card

                    def counted_render_action_card() -> None:
                        nonlocal calls
                        calls += 1
                        original_render_action_card()

                    page._render_action_card = counted_render_action_card
                    page._schedule_action_card_resync()
                    page._reset_task()
                    calls = 0

                    self._wait_for_qt_timers()

                    self.assertEqual(calls, 0)
                    self.assertEqual(page.phase, "idle")
                    self.assertNotIn("返回并开始新任务", self._visible_button_texts(page))

    def test_excel_begin_runner_passes_untranslated_only_to_task_runner(self) -> None:
        captured_kwargs = {}

        class FakeRunner:
            task_id = "fake-excel"

            def __init__(self, *_args, **kwargs) -> None:
                captured_kwargs.update(kwargs)

            def start(self) -> None:
                pass

        with (
            patch("native_app.pages.excel_translate.TaskRunner", FakeRunner),
            patch("native_app.pages.excel_translate.save_settings"),
        ):
            page = ExcelTranslatePage(AppSettings())
            self.addCleanup(page.close)
            self.addCleanup(page.deleteLater)
            page._begin_runner(
                [FileItem(path=Path("source.xlsx"), name="source", size_kb=1.0)],
                page.settings,
                source_lang="zh",
                untranslated_only=True,
            )

        self.assertTrue(captured_kwargs["untranslated_only"])

    def test_word_begin_runner_passes_untranslated_only_to_task_runner(self) -> None:
        captured_kwargs = {}

        class FakeRunner:
            task_id = "fake-word"

            def __init__(self, *_args, **kwargs) -> None:
                captured_kwargs.update(kwargs)

            def start(self) -> None:
                pass

        with (
            patch("native_app.pages.word_translate.WordTaskRunner", FakeRunner),
            patch("native_app.pages.word_translate.save_settings"),
        ):
            page = WordTranslatePage(AppSettings())
            self.addCleanup(page.close)
            self.addCleanup(page.deleteLater)
            page._begin_runner(
                [WordFileItem(path=Path("source.docx"), name="source", size_kb=1.0)],
                page.settings,
                source_lang="zh",
                untranslated_only=True,
            )

        self.assertTrue(captured_kwargs["untranslated_only"])

    def test_word_review_color_options_show_highlight_background(self) -> None:
        with patch("native_app.pages.word_translate.count_diagnostic_records", return_value=0):
            page = WordTranslatePage(AppSettings())
        self.addCleanup(page.close)
        self.addCleanup(page.deleteLater)

        combo = page.review_color_combos[MIXED_MARK_UNRESOLVED]
        default_index = combo.findData("FCE4D6")
        custom_index = combo.findData("__custom__")
        brush = combo.itemData(default_index, Qt.ItemDataRole.BackgroundRole)
        foreground = combo.itemData(default_index, Qt.ItemDataRole.ForegroundRole)

        self.assertIsInstance(combo, CurrentTextOverrideComboBox)
        self.assertEqual(combo.currentIndex(), default_index)
        self.assertEqual(combo.itemText(default_index), "浅橙（默认） #FCE4D6")
        self.assertFalse(any(combo.itemText(index).startswith("默认 ") for index in range(combo.count())))
        self.assertEqual(combo.toolTip(), "")
        self.assertIn("保留原文复核", page.review_color_label.toolTip())
        self.assertNotIn("保留原文需复核", page.review_color_label.toolTip())
        self.assertEqual(brush.color().name().upper(), "#FCE4D6")
        self.assertEqual(foreground.color().name().upper(), "#111827")
        self.assertFalse(page.review_color_inputs[MIXED_MARK_UNRESOLVED].isVisible())
        self.assertEqual(page.review_color_inputs[MIXED_MARK_UNRESOLVED].toolTip(), "")
        self.assertTrue(page.review_color_buttons[MIXED_MARK_UNRESOLVED].isHidden())
        self.assertIn("border: 1px solid palette(base);", combo.styleSheet())
        self.assertIn("QComboBox QAbstractItemView", combo.styleSheet())
        self.assertGreaterEqual(int(combo.property("popupMinimumWidth")), 236)

        combo.blockSignals(True)
        combo.setCurrentIndex(custom_index)
        combo.blockSignals(False)
        page.review_color_inputs[MIXED_MARK_UNRESOLVED].setText("#112233")
        page._refresh_review_color_control(MIXED_MARK_UNRESOLVED)

        self.assertEqual(combo.currentDisplayTextOverride(), "自定义")
        self.assertEqual(combo.itemText(custom_index), "自定义 #112233")
        self.assertEqual(combo.minimumWidth(), combo.maximumWidth())
        self.assertLess(combo.maximumWidth(), 116)
        self.assertGreaterEqual(page.review_color_inputs[MIXED_MARK_UNRESOLVED].minimumWidth(), 92)

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

            self.assertIsInstance(combo, CurrentTextOverrideComboBox)
            self.assertEqual(combo.currentIndex(), default_index)
            self.assertEqual(combo.itemText(default_index), "浅橙（默认） #FCE4D6")
            self.assertFalse(any(combo.itemText(index).startswith("默认 ") for index in range(combo.count())))
            self.assertEqual(combo.toolTip(), "")
            self.assertIn("保留原文复核", page.review_color_label.toolTip())
            self.assertNotIn("保留原文需复核", page.review_color_label.toolTip())
            self.assertEqual(brush.color().name().upper(), "#FCE4D6")
            self.assertIn("border: 1px solid palette(base);", combo.styleSheet())
            self.assertIn("QComboBox QAbstractItemView", combo.styleSheet())
            self.assertGreaterEqual(int(combo.property("popupMinimumWidth")), 236)
            self.assertTrue(page.review_mark_check.isChecked())
            self.assertFalse(page.review_color_inputs[MIXED_MARK_UNRESOLVED].isVisible())
            self.assertEqual(page.review_color_inputs[MIXED_MARK_UNRESOLVED].toolTip(), "")
            self.assertTrue(page.review_color_buttons[MIXED_MARK_UNRESOLVED].isHidden())

            page.review_mark_check.setChecked(False)
            page._on_params_changed()
            self.assertFalse(page.settings.excel_review.mark_review_items)

            combo.setCurrentIndex(custom_index)
            page.review_color_inputs[MIXED_MARK_UNRESOLVED].setText("#000000")
            page._on_params_changed()

            self.assertEqual(
                page.settings.word_review.mark_colors[MIXED_MARK_UNRESOLVED],
                "000000",
            )
            self.assertEqual(combo.currentDisplayTextOverride(), "自定义")
            self.assertEqual(combo.itemText(custom_index), "自定义 #000000")
            self.assertEqual(combo.minimumWidth(), combo.maximumWidth())
            self.assertLess(combo.maximumWidth(), 116)
            self.assertGreaterEqual(
                page.review_color_inputs[MIXED_MARK_UNRESOLVED].minimumWidth(),
                92,
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

    def test_review_policy_combos_use_shared_popup_contract(self) -> None:
        with patch("native_app.pages.excel_translate.save_settings"):
            excel_page = ExcelTranslatePage(AppSettings())
        word_page = WordTranslatePage(AppSettings())
        for page in (excel_page, word_page):
            self.addCleanup(page.close)
            self.addCleanup(page.deleteLater)

        policy_combos = (
            excel_page.existing_fill_policy_combo,
            word_page.existing_highlight_policy_combo,
        )
        for combo in policy_combos:
            longest = max(
                combo.fontMetrics().horizontalAdvance(combo.itemText(index))
                for index in range(combo.count())
            )
            self.assertIsInstance(combo, AlignedComboBox)
            self.assertFalse(combo.isEditable())
            self.assertTrue(combo.property("appOptionCombo"))
            self.assertEqual(combo.maxVisibleItems(), combo.count())
            self.assertGreaterEqual(int(combo.property("popupMinimumWidth")), longest + 48)

    def test_tm_language_pair_pill_clips_from_left_without_middle_elide(self) -> None:
        settings = AppSettings()
        settings.source_lang = "x-very-long-source-language-code"
        settings.target_lang = "x-very-long-target-language-code"
        page = self._make_tm_page(settings)

        page.resize(1100, 720)
        page.show()
        self.app.processEvents()

        value_labels = [
            label
            for label in page.findChildren(QLabel)
            if label.objectName() == "PillValue" and label.text() == page.lang_pair
        ]
        self.assertTrue(value_labels)
        value_label = value_labels[0]
        self.assertNotIsInstance(value_label, MiddleElideLabel)
        self.assertTrue(value_label.alignment() & Qt.AlignmentFlag.AlignLeft)
        self.assertEqual(value_label.toolTip(), page.lang_pair)
        self.assertLessEqual(
            value_label.maximumWidth(),
            TM_LANG_PAIR_TILE_MAX_WIDTH - 16,
        )

    def test_tm_scope_controls_keep_three_columns_aligned(self) -> None:
        page = self._make_tm_page(AppSettings())

        page.resize(1100, 720)
        page.show()
        self.app.processEvents()

        controls = (page.source_combo, page.target_combo, page.max_len_spin)
        tops = [
            control.mapTo(page, control.rect().topLeft()).y()
            for control in controls
        ]
        heights = [control.height() for control in controls]
        widths = [control.width() for control in controls]

        self.assertLessEqual(max(tops) - min(tops), 1)
        self.assertEqual(heights, [heights[0]] * len(heights))
        self.assertEqual(heights[0], 32)
        self.assertLessEqual(max(widths) - min(widths), 1)

    def test_tm_top_cards_keep_rows_aligned(self) -> None:
        page = self._make_tm_page(AppSettings())

        page.resize(1100, 720)
        page.show()
        self.app.processEvents()

        row_groups = (
            (page.overview_title_row, page.scope_title_row, page.cleaner_title_row),
            (page.overview_info_row, page.scope_label_row, page.cleaner_info_row),
            (page.overview_action_row, page.scope_control_row, page.cleaner_action_row),
        )
        for rows in row_groups:
            tops = [row.mapTo(page, row.rect().topLeft()).y() for row in rows]
            heights = [row.height() for row in rows]

            self.assertLessEqual(max(tops) - min(tops), 1)
            self.assertEqual(heights, [heights[0]] * len(heights))
            self.assertTrue(all(row.property("tmTopCardRow") for row in rows))
        self.assertIn(
            'QFrame[tmTopCard="true"] QWidget[tmTopCardRow="true"] {\n    background: transparent;',
            APP_QSS,
        )

    def test_tm_metric_rows_use_inline_separators_and_balanced_weight(self) -> None:
        page = self._make_tm_page(AppSettings())

        page.resize(1100, 720)
        page.show()
        self.app.processEvents()

        separators = [
            widget
            for widget in page.findChildren(QWidget)
            if widget.property("tmMetricSeparator")
        ]
        self.assertEqual(len(separators), 5)
        self.assertTrue(all(separator.width() == 1 for separator in separators))
        self.assertTrue(all(separator.height() == 16 for separator in separators))
        self.assertIn("QFrame[tmMetricSeparator=\"true\"]", APP_QSS)
        self.assertIn("QLabel#TmMetricValue {\n    color: #1A2035;\n    font-size: 12px;\n    font-weight: 500;", APP_QSS)
        self.assertNotIn("QLabel#TmMetricValue {\n    color: #111827;\n    font-size: 13px;\n    font-weight: 700;", APP_QSS)

    def test_tm_language_combo_popups_read_from_left_to_right(self) -> None:
        page = self._make_tm_page(AppSettings())

        for combo in (page.source_combo, page.target_combo):
            longest = max(
                combo.fontMetrics().horizontalAdvance(combo.itemText(index))
                for index in range(combo.count())
            )
            self.assertEqual(combo.view().textElideMode(), Qt.TextElideMode.ElideRight)
            self.assertEqual(
                combo.completer().popup().textElideMode(),
                Qt.TextElideMode.ElideRight,
            )
            self.assertGreaterEqual(int(combo.property("popupMinimumWidth")), longest + 56)
            self.assertIsNotNone(combo.lineEdit())
            self.assertEqual(combo.lineEdit().cursorPosition(), 0)

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
        params_tooltip = page.params_title_label.toolTip()
        self.assertIn("设置 PDF 页图翻译", params_tooltip)
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
        self.assertEqual(compression_checks[0].toolTip(), "")
        self.assertIn("关闭后仅输出高清版", params_tooltip)
        image_checks = [
            checkbox
            for checkbox in page.findChildren(QCheckBox)
            if checkbox.text() == "启用图片翻译"
        ]
        self.assertEqual(len(image_checks), 1)
        self.assertFalse(image_checks[0].isChecked())
        self.assertEqual(image_checks[0].toolTip(), "")
        self.assertIn("PNG、JPG、JPEG、WebP、BMP、TIFF", params_tooltip)
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
        self.assertEqual(review_checks[0].toolTip(), "")
        self.assertIn("未启用时无需配置审核模型", params_tooltip)
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
