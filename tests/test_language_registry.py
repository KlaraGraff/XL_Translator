from __future__ import annotations

import unittest

from core.language_registry import (
    get_supported_languages,
    get_supported_source_languages,
    resolve_language_code,
)


class LanguageRegistryTests(unittest.TestCase):
    def test_common_chinese_aliases_resolve_to_zh(self) -> None:
        supported = get_supported_source_languages()

        self.assertEqual(resolve_language_code("汉语", supported), "zh")
        self.assertEqual(resolve_language_code("华语", supported), "zh")
        self.assertEqual(resolve_language_code("普通话", supported), "zh")

    def test_wen_yu_suffixes_resolve_without_per_language_overrides(self) -> None:
        supported = get_supported_languages(include_optional=True)

        self.assertEqual(resolve_language_code("法语", supported), "fr")
        self.assertEqual(resolve_language_code("俄文", supported), "ru")


if __name__ == "__main__":
    unittest.main(verbosity=2)
