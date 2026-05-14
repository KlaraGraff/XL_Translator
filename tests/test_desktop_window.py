from __future__ import annotations

import unittest

from scripts.desktop_window import should_use_system_browser_for_macos


class DesktopWindowTests(unittest.TestCase):
    def test_macos_12_uses_system_browser(self) -> None:
        self.assertTrue(
            should_use_system_browser_for_macos(
                platform_name="darwin",
                macos_version="12.7.6",
            )
        )

    def test_macos_13_keeps_webview(self) -> None:
        self.assertFalse(
            should_use_system_browser_for_macos(
                platform_name="darwin",
                macos_version="13.6.9",
            )
        )

    def test_non_macos_keeps_webview_path(self) -> None:
        self.assertFalse(
            should_use_system_browser_for_macos(
                platform_name="win32",
                macos_version="12.7.6",
            )
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
