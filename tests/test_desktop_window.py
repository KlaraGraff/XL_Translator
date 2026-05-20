from __future__ import annotations

from types import SimpleNamespace
import unittest

from scripts.desktop_window import (
    configure_webview_downloads,
    should_use_system_browser_for_macos,
)


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

    def test_webview_downloads_are_enabled_for_app_window(self) -> None:
        webview = SimpleNamespace(settings={"ALLOW_DOWNLOADS": False})

        configure_webview_downloads(webview)

        self.assertTrue(webview.settings["ALLOW_DOWNLOADS"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
