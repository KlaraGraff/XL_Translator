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

    def test_output_dir_routes_to_surface_output_settings(self) -> None:
        settings = build_runtime_settings(
            base_settings=AppSettings(),
            output_dir="~/translated-out",
        )

        for surface_output in (settings.excel_output, settings.word_output):
            self.assertTrue(surface_output.use_custom_output_dir)
            self.assertTrue(surface_output.custom_output_dir.endswith("translated-out"))

    def test_no_output_dir_clears_surface_output_settings(self) -> None:
        base = AppSettings()
        base.excel_output.use_custom_output_dir = True
        base.excel_output.custom_output_dir = "/tmp/stale"
        base.word_output.use_custom_output_dir = True
        base.word_output.custom_output_dir = "/tmp/stale"

        settings = build_runtime_settings(base_settings=base, output_dir=None)

        for surface_output in (settings.excel_output, settings.word_output):
            self.assertFalse(surface_output.use_custom_output_dir)
            self.assertEqual(surface_output.custom_output_dir, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
