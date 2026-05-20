from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.app_paths import get_app_data_dir, get_legacy_app_data_dir


class AppPathTests(unittest.TestCase):
    def test_macos_uses_application_support(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.assertEqual(
                get_app_data_dir(system="Darwin", home=home),
                home / "Library" / "Application Support" / "Translator",
            )

    def test_windows_uses_local_app_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "LocalAppData"
            self.assertEqual(
                get_app_data_dir(
                    system="Windows",
                    home=Path(tmp) / "home",
                    local_app_data=local,
                ),
                local / "Translator",
            )

    def test_linux_uses_xdg_data_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            xdg = Path(tmp) / "xdg"
            self.assertEqual(
                get_app_data_dir(
                    system="Linux",
                    home=Path(tmp) / "home",
                    xdg_data_home=xdg,
                ),
                xdg / "Translator",
            )

    def test_legacy_path_stays_dot_xl_translator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.assertEqual(get_legacy_app_data_dir(home=home), home / ".xl_translator")


if __name__ == "__main__":
    unittest.main(verbosity=2)
