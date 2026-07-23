from __future__ import annotations

import unittest

from config import SUPPORTED_LANGS, SUPPORTED_SOURCE_LANGS
from core.language_registry import (
    AUTO_SOURCE_LANG,
    append_custom_target_lang,
    build_lang_pair,
    get_language_catalog,
    get_source_language_options,
    get_target_language_options,
    get_tm_language_pairs,
    get_supported_source_languages,
    is_valid_language_pair,
    resolve_language_code,
    update_custom_target_lang_display,
)


class Phase1LanguageContractTests(unittest.TestCase):
    def test_single_catalog_has_59_builtin_languages_for_both_directions(self) -> None:
        self.assertEqual(len(SUPPORTED_LANGS), 59)
        self.assertEqual(len(SUPPORTED_SOURCE_LANGS), 59)
        catalog = get_language_catalog()
        self.assertEqual(len(catalog), 59)
        self.assertTrue(all(item["can_source"] and item["can_target"] for item in catalog))
        self.assertEqual(get_source_language_options()[0]["code"], AUTO_SOURCE_LANG)
        self.assertEqual(len(get_target_language_options()), 59)

    def test_search_resolves_english_name_and_iso(self) -> None:
        supported = get_supported_source_languages()
        self.assertEqual(resolve_language_code("French", supported), "fr")
        self.assertEqual(resolve_language_code("fr", supported), "fr")
        self.assertEqual(resolve_language_code("法语", supported), "fr")

    def test_auto_never_becomes_a_tm_pair(self) -> None:
        self.assertEqual(get_tm_language_pairs(("auto", "mixed", "fr"), "en"), ["fr-en"])
        self.assertFalse(is_valid_language_pair("auto", "en"))
        with self.assertRaises(ValueError):
            build_lang_pair("en", source_lang="auto")

    def test_custom_display_edit_preserves_opaque_code_and_target_only(self) -> None:
        languages, code = append_custom_target_lang([], "Reviewer dialect", "first")
        renamed = update_custom_target_lang_display(
            languages,
            code,
            "Reviewer language",
            "updated",
        )
        self.assertEqual(renamed[0].code, code)
        self.assertEqual(renamed[0].name, "Reviewer language")
        self.assertEqual(get_language_catalog(renamed)[-1]["code"], code)
        self.assertFalse(any(item["code"] == code for item in get_source_language_options(renamed)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
