from __future__ import annotations

import unittest

from settings import AppSettings


class SourcePathSettingsTests(unittest.TestCase):
    def test_page_source_paths_are_independent_settings(self) -> None:
        settings = AppSettings(
            last_excel_source_folder="/tmp/source.xlsx",
            last_word_source_folder="/tmp/source.docx",
            last_pdf_source_folder="/tmp/source.pdf",
        )

        self.assertEqual(settings.last_excel_source_folder, "/tmp/source.xlsx")
        self.assertEqual(settings.last_word_source_folder, "/tmp/source.docx")
        self.assertEqual(settings.last_pdf_source_folder, "/tmp/source.pdf")

if __name__ == "__main__":
    unittest.main(verbosity=2)
