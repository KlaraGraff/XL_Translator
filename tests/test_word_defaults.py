from __future__ import annotations

import unittest

from config import SETTINGS_SCHEMA_VERSION
from settings import AppSettings, WordBatchSettings, _migrate_settings_payload


class WordDefaultSettingsTests(unittest.TestCase):
    def test_word_task_parameter_defaults_match_native_ui_baseline(self) -> None:
        settings = AppSettings()

        self.assertTrue(settings.word_review.highlight_unresolved)
        self.assertEqual(settings.word_batch.max_paragraphs_per_batch, 8)
        self.assertEqual(settings.word_batch.max_chars_per_batch, 3000)
        self.assertEqual(settings.word_batch.split_paragraph_chars, 6000)
        self.assertEqual(settings.word_batch.strict_retry_attempts, 3)

    def test_word_batch_defaults_match_expected_baseline(self) -> None:
        settings = WordBatchSettings()

        self.assertEqual(settings.max_paragraphs_per_batch, 8)
        self.assertEqual(settings.max_chars_per_batch, 3000)
        self.assertEqual(settings.split_paragraph_chars, 6000)
        self.assertEqual(settings.strict_retry_attempts, 3)

    def test_schema_v10_migration_enables_word_review_highlight(self) -> None:
        migrated = _migrate_settings_payload(
            {
                "settings_version": 9,
                "word_review": {
                    "highlight_unresolved": False,
                    "highlight_color": "FFF2CC",
                },
            },
            source_version=9,
        )

        self.assertEqual(migrated["settings_version"], SETTINGS_SCHEMA_VERSION)
        self.assertTrue(migrated["word_review"]["highlight_unresolved"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
