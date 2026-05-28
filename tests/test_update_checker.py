from __future__ import annotations

import unittest

from core.update_checker import (
    build_update_result_from_release_payload,
    is_major_upgrade,
    is_newer_version,
    major_version,
)


class UpdateCheckerTests(unittest.TestCase):
    def test_version_compare_treats_patchless_and_patch_versions_as_equal(self) -> None:
        self.assertFalse(is_newer_version("v4.1.0", "4.1"))
        self.assertTrue(is_newer_version("v4.2", "4.1.9"))

    def test_major_version_helpers_require_parseable_semver(self) -> None:
        self.assertEqual(major_version("v5.1.2"), 5)
        self.assertTrue(is_major_upgrade("6.0", "5.2.1"))
        self.assertFalse(is_major_upgrade("5.3", "5.2.1"))
        self.assertIsNone(major_version("release-2026.05"))

    def test_build_update_result_selects_macos_dmg_asset(self) -> None:
        payload = {
            "tag_name": "v4.2",
            "html_url": "https://github.com/KlaraGraff/XL_Translator/releases/tag/v4.2",
            "body": "- 新增更新说明",
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
        self.assertEqual(result.release_notes, "- 新增更新说明")

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

    def test_build_update_result_skips_unparseable_release_tags(self) -> None:
        payload = {
            "tag_name": "release-2026.05",
            "html_url": "https://github.com/KlaraGraff/XL_Translator/releases/tag/release-2026.05",
            "assets": [],
        }

        result = build_update_result_from_release_payload(
            payload,
            current_version="5.0",
            platform_name="Darwin",
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "unknown")
        self.assertFalse(result.has_update)


if __name__ == "__main__":
    unittest.main(verbosity=2)
