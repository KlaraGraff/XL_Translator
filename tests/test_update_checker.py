from __future__ import annotations

import unittest

from core.update_checker import (
    build_update_result_from_release_payload,
    is_newer_version,
)


class UpdateCheckerTests(unittest.TestCase):
    def test_version_compare_treats_patchless_and_patch_versions_as_equal(self) -> None:
        self.assertFalse(is_newer_version("v4.1.0", "4.1"))
        self.assertTrue(is_newer_version("v4.2", "4.1.9"))

    def test_build_update_result_selects_macos_dmg_asset(self) -> None:
        payload = {
            "tag_name": "v4.2",
            "html_url": "https://github.com/KlaraGraff/XL_Translator/releases/tag/v4.2",
            "assets": [
                {
                    "name": "Translator_Windows_4.2_Setup.exe",
                    "browser_download_url": "https://example.test/windows.exe",
                },
                {
                    "name": "Translator_macOS_4.2.dmg",
                    "browser_download_url": "https://example.test/macos.dmg",
                },
            ],
        }

        result = build_update_result_from_release_payload(
            payload,
            current_version="4.1",
            platform_name="Darwin",
        )

        self.assertTrue(result.has_update)
        self.assertEqual(result.latest_version, "4.2")
        self.assertEqual(result.asset_name, "Translator_macOS_4.2.dmg")
        self.assertEqual(result.download_url, "https://example.test/macos.dmg")

    def test_build_update_result_reports_current_version(self) -> None:
        payload = {
            "tag_name": "v4.1.0",
            "html_url": "https://github.com/KlaraGraff/XL_Translator/releases/tag/v4.1.0",
            "assets": [],
        }

        result = build_update_result_from_release_payload(
            payload,
            current_version="4.1",
            platform_name="Windows",
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "current")
        self.assertFalse(result.has_update)


if __name__ == "__main__":
    unittest.main(verbosity=2)
