from __future__ import annotations

import unittest

from core.mixed_language import (
    MIXED_MARK_FOREIGN_NOISE,
    MIXED_MARK_SEMANTIC,
    MIXED_MARK_UNRESOLVED,
)
from settings import AppSettings, WordBatchSettings


class WordDefaultSettingsTests(unittest.TestCase):
    def test_word_task_parameter_defaults_match_native_ui_baseline(self) -> None:
        settings = AppSettings()

        self.assertTrue(settings.word_review.highlight_unresolved)
        self.assertEqual(settings.word_review.existing_highlight_policy, "red_underline")
        self.assertEqual(
            settings.word_review.mark_colors,
            {
                MIXED_MARK_SEMANTIC: "FFF2CC",
                MIXED_MARK_UNRESOLVED: "FCE4D6",
                MIXED_MARK_FOREIGN_NOISE: "F4CCCC",
            },
        )
        self.assertTrue(settings.excel_review.mark_review_items)
        self.assertEqual(settings.excel_review.existing_fill_policy, "red_font")
        self.assertEqual(settings.word_batch.max_paragraphs_per_batch, 8)
        self.assertEqual(settings.word_batch.max_chars_per_batch, 3000)
        self.assertEqual(settings.word_batch.split_paragraph_chars, 3000)
        self.assertEqual(settings.word_batch.strict_retry_attempts, 3)
        self.assertTrue(settings.word_conversion.use_native_preprocessing)
        self.assertTrue(settings.word_conversion.prefer_native_word)

    def test_word_batch_defaults_match_expected_baseline(self) -> None:
        settings = WordBatchSettings()

        self.assertEqual(settings.max_paragraphs_per_batch, 8)
        self.assertEqual(settings.max_chars_per_batch, 3000)
        self.assertEqual(settings.split_paragraph_chars, 3000)
        self.assertEqual(settings.strict_retry_attempts, 3)

    def test_language_aliases_are_normalized_without_falling_back_to_chinese(self) -> None:
        settings = AppSettings(source_lang="汉语", target_lang="法语")

        self.assertEqual(settings.source_lang, "zh")
        self.assertEqual(settings.target_lang, "fr")

    def test_same_chinese_source_and_target_repairs_to_recent_non_chinese_target(self) -> None:
        settings = AppSettings(source_lang="zh", target_lang="zh", recent_target_langs=["fr", "zh"])

        self.assertEqual(settings.source_lang, "zh")
        self.assertEqual(settings.target_lang, "fr")

if __name__ == "__main__":
    unittest.main(verbosity=2)
