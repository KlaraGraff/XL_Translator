from __future__ import annotations

import unittest

from config import SETTINGS_SCHEMA_VERSION
from core.mixed_language import (
    MIXED_MARK_FOREIGN_NOISE,
    MIXED_MARK_SEMANTIC,
    MIXED_MARK_UNRESOLVED,
)
from settings import AppSettings, WordBatchSettings, _migrate_settings_payload


class WordDefaultSettingsTests(unittest.TestCase):
    def test_word_task_parameter_defaults_match_native_ui_baseline(self) -> None:
        settings = AppSettings()

        self.assertTrue(settings.word_review.highlight_unresolved)
        self.assertEqual(settings.word_review.existing_highlight_policy, "skip")
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

    def test_word_conversion_legacy_native_preference_maps_to_preprocessing(self) -> None:
        settings = AppSettings(word_conversion={"prefer_native_word": False})

        self.assertFalse(settings.word_conversion.use_native_preprocessing)
        self.assertFalse(settings.word_conversion.prefer_native_word)

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
        self.assertEqual(migrated["word_review"]["existing_highlight_policy"], "skip")
        self.assertEqual(
            migrated["word_review"]["mark_colors"],
            {
                MIXED_MARK_SEMANTIC: "FFF2CC",
                MIXED_MARK_UNRESOLVED: "FCE4D6",
                MIXED_MARK_FOREIGN_NOISE: "F4CCCC",
            },
        )
        self.assertTrue(migrated["excel_review"]["mark_review_items"])
        self.assertEqual(migrated["excel_review"]["existing_fill_policy"], "red_font")
        self.assertTrue(migrated["word_conversion"]["use_native_preprocessing"])
        self.assertTrue(migrated["word_conversion"]["prefer_native_word"])

    def test_word_review_custom_legacy_highlight_seeds_mark_colors(self) -> None:
        migrated = _migrate_settings_payload(
            {
                "settings_version": 20,
                "word_review": {
                    "highlight_unresolved": True,
                    "highlight_color": "DDEBFF",
                    "existing_highlight_policy": "skip",
                },
                "excel_review": {"existing_fill_policy": "red_font"},
            },
            source_version=20,
        )

        self.assertEqual(migrated["settings_version"], SETTINGS_SCHEMA_VERSION)
        self.assertEqual(
            migrated["word_review"]["mark_colors"],
            {
                MIXED_MARK_SEMANTIC: "DDEBFF",
                MIXED_MARK_UNRESOLVED: "DDEBFF",
                MIXED_MARK_FOREIGN_NOISE: "DDEBFF",
            },
        )

    def test_schema_v22_migration_swaps_previous_default_pair(self) -> None:
        migrated = _migrate_settings_payload(
            {
                "settings_version": 21,
                "word_review": {
                    "mark_colors": {
                        MIXED_MARK_SEMANTIC: "FFF2CC",
                        MIXED_MARK_UNRESOLVED: "F4CCCC",
                        MIXED_MARK_FOREIGN_NOISE: "FCE4D6",
                    },
                },
            },
            source_version=21,
        )

        self.assertEqual(migrated["settings_version"], SETTINGS_SCHEMA_VERSION)
        self.assertEqual(
            migrated["word_review"]["mark_colors"],
            {
                MIXED_MARK_SEMANTIC: "FFF2CC",
                MIXED_MARK_UNRESOLVED: "FCE4D6",
                MIXED_MARK_FOREIGN_NOISE: "F4CCCC",
            },
        )

    def test_schema_v23_migration_enables_excel_review_mark(self) -> None:
        migrated = _migrate_settings_payload(
            {
                "settings_version": 22,
                "excel_review": {"existing_fill_policy": "skip"},
            },
            source_version=22,
        )

        self.assertEqual(migrated["settings_version"], SETTINGS_SCHEMA_VERSION)
        self.assertTrue(migrated["excel_review"]["mark_review_items"])
        self.assertEqual(migrated["excel_review"]["existing_fill_policy"], "skip")

    def test_schema_v24_migration_normalizes_language_aliases(self) -> None:
        migrated = _migrate_settings_payload(
            {
                "settings_version": 23,
                "source_lang": "汉语",
                "target_lang": "法语",
            },
            source_version=23,
        )

        self.assertEqual(migrated["settings_version"], SETTINGS_SCHEMA_VERSION)
        self.assertEqual(migrated["source_lang"], "zh")
        self.assertEqual(migrated["target_lang"], "fr")


if __name__ == "__main__":
    unittest.main(verbosity=2)
