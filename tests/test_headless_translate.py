from __future__ import annotations

import unittest

from core.headless_translate import build_runtime_settings
from settings import AppSettings


class HeadlessTranslateTests(unittest.TestCase):
    def test_runtime_settings_resolve_language_aliases(self) -> None:
        settings = build_runtime_settings(
            base_settings=AppSettings(),
            source_lang="汉语",
            target_lang="法语",
        )

        self.assertEqual(settings.source_lang, "zh")
        self.assertEqual(settings.target_lang, "fr")


if __name__ == "__main__":
    unittest.main(verbosity=2)
