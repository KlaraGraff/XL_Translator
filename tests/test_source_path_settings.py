from __future__ import annotations

import unittest

from config import SETTINGS_SCHEMA_VERSION
from settings import AppSettings, _migrate_settings_payload


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

    def test_v17_migration_routes_legacy_pdf_file_to_pdf_history(self) -> None:
        migrated = _migrate_settings_payload(
            {
                "settings_version": 16,
                "last_source_folder": "/Users/example/Desktop/source.pdf",
            },
            source_version=16,
        )

        self.assertEqual(migrated["settings_version"], SETTINGS_SCHEMA_VERSION)
        self.assertEqual(
            migrated["last_pdf_source_folder"],
            "/Users/example/Desktop/source.pdf",
        )
        self.assertEqual(migrated["last_excel_source_folder"], "")
        self.assertEqual(migrated["last_word_source_folder"], "")

    def test_v17_migration_keeps_legacy_folder_available_on_all_pages(self) -> None:
        migrated = _migrate_settings_payload(
            {
                "settings_version": 16,
                "last_source_folder": "/Users/example/Desktop/source-folder",
            },
            source_version=16,
        )

        self.assertEqual(
            migrated["last_excel_source_folder"],
            "/Users/example/Desktop/source-folder",
        )
        self.assertEqual(
            migrated["last_word_source_folder"],
            "/Users/example/Desktop/source-folder",
        )
        self.assertEqual(
            migrated["last_pdf_source_folder"],
            "/Users/example/Desktop/source-folder",
        )

    def test_v17_migration_preserves_existing_page_specific_values(self) -> None:
        migrated = _migrate_settings_payload(
            {
                "settings_version": 16,
                "last_source_folder": "/Users/example/Desktop/source.pdf",
                "last_pdf_source_folder": "/Users/example/Desktop/known.pdf",
            },
            source_version=16,
        )

        self.assertEqual(
            migrated["last_pdf_source_folder"],
            "/Users/example/Desktop/known.pdf",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
