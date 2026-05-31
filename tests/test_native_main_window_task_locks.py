from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from native_app.main_window import NativeMainWindow
from settings import AppSettings


API_A = ("cloud", "custom_openai", "https://api-a.test/v1", "key-a")
API_B = ("cloud", "custom_openai", "https://api-b.test/v1", "key-b")
API_C = ("cloud", "custom_openai", "https://api-c.test/v1", "key-c")


class NativeMainWindowTaskLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        super().setUp()
        patchers = [
            patch("native_app.main_window.NativeMainWindow._start_update_check"),
            patch("native_app.main_window.save_settings"),
            patch("native_app.pages.excel_translate.count_diagnostic_records", return_value=0),
            patch("native_app.pages.word_translate.count_diagnostic_records", return_value=0),
            patch("native_app.pages.pdf_translate.count_diagnostic_records", return_value=0),
            patch("native_app.pages.tm_manager.tm_manager.init_db"),
            patch(
                "native_app.pages.tm_manager.tm_manager.get_stats",
                return_value={
                    "total": 0,
                    "pinned": 0,
                    "manual": 0,
                    "auto": 0,
                },
            ),
            patch("native_app.pages.tm_manager.tm_manager.search_entries", return_value=([], 0)),
            patch(
                "native_app.pages.tm_manager.tm_manager.get_pin_count",
                return_value={"pinned": 0, "unpinned": 0},
            ),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _make_window(self) -> NativeMainWindow:
        window = NativeMainWindow(AppSettings())
        self.addCleanup(window.close)
        self.addCleanup(window.deleteLater)
        return window

    def test_navigation_between_pages_keeps_translation_lock_sync(self) -> None:
        window = self._make_window()

        for page in ("word_translate", "pdf_translate", "tm", "excel_translate"):
            with self.subTest(page=page):
                window._navigate(page)

        self.assertEqual(window.stack.currentWidget(), window.pages["excel_translate"])

    def test_running_translation_locks_only_pages_sharing_api_until_terminal(self) -> None:
        window = self._make_window()
        excel_page = window.pages["excel_translate"]
        word_page = window.pages["word_translate"]
        pdf_page = window.pages["pdf_translate"]

        excel_page.phase = "running"
        excel_page.runner = object()
        excel_page.done = None
        excel_page.current_task_api_groups = {API_A}

        def groups_for_page(_settings, page_key: str):
            return {
                "excel_translate": frozenset({API_A}),
                "word_translate": frozenset({API_A}),
                "pdf_translate": frozenset({API_B}),
            }[page_key]

        with patch("native_app.main_window.task_api_groups_for_page", side_effect=groups_for_page):
            window._sync_translation_task_locks()

        self.assertFalse(excel_page.external_task_lock)
        self.assertTrue(word_page.external_task_lock)
        self.assertFalse(pdf_page.external_task_lock)
        self.assertFalse(word_page.scan_button.isEnabled())
        self.assertTrue(pdf_page.scan_button.isEnabled())
        self.assertFalse(word_page._can_start())
        self.assertIn("当前 API", word_page.external_task_lock_reason)

        excel_page.phase = "done"
        excel_page.runner = None
        with patch("native_app.main_window.task_api_groups_for_page", side_effect=groups_for_page):
            window._sync_translation_task_locks()

        self.assertFalse(word_page.external_task_lock)
        self.assertFalse(pdf_page.external_task_lock)
        self.assertTrue(word_page.scan_button.isEnabled())
        self.assertTrue(pdf_page.scan_button.isEnabled())

    def test_pdf_review_api_footprint_blocks_conflicting_text_task(self) -> None:
        window = self._make_window()
        excel_page = window.pages["excel_translate"]
        word_page = window.pages["word_translate"]
        pdf_page = window.pages["pdf_translate"]

        pdf_page.phase = "running"
        pdf_page.runner = object()
        pdf_page.done = None
        pdf_page.current_task_api_groups = {API_A, API_B}

        def groups_for_page(_settings, page_key: str):
            return {
                "excel_translate": frozenset({API_C}),
                "word_translate": frozenset({API_B}),
                "pdf_translate": frozenset({API_A, API_B}),
            }[page_key]

        with patch("native_app.main_window.task_api_groups_for_page", side_effect=groups_for_page):
            window._sync_translation_task_locks()

        self.assertFalse(pdf_page.external_task_lock)
        self.assertFalse(excel_page.external_task_lock)
        self.assertTrue(word_page.external_task_lock)
        self.assertIn("PDF 翻译", word_page.external_task_owner_label)
        self.assertIn("当前 API", word_page.external_task_lock_reason)

    def test_multiple_running_tasks_allow_candidate_on_third_api(self) -> None:
        window = self._make_window()
        excel_page = window.pages["excel_translate"]
        word_page = window.pages["word_translate"]
        pdf_page = window.pages["pdf_translate"]

        excel_page.phase = "running"
        excel_page.runner = object()
        excel_page.done = None
        excel_page.current_task_api_groups = {API_A}
        pdf_page.phase = "running"
        pdf_page.runner = object()
        pdf_page.done = None
        pdf_page.current_task_api_groups = {API_B}

        def groups_for_page(_settings, page_key: str):
            return {
                "excel_translate": frozenset({API_A}),
                "word_translate": frozenset({API_C}),
                "pdf_translate": frozenset({API_B}),
            }[page_key]

        with patch("native_app.main_window.task_api_groups_for_page", side_effect=groups_for_page):
            window._sync_translation_task_locks()

        self.assertFalse(excel_page.external_task_lock)
        self.assertFalse(pdf_page.external_task_lock)
        self.assertFalse(word_page.external_task_lock)


if __name__ == "__main__":
    unittest.main(verbosity=2)
