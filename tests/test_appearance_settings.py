from __future__ import annotations

import unittest

from settings import AppSettings


class AppearanceSettingsTests(unittest.TestCase):
    def test_invalid_theme_falls_back_to_system(self) -> None:
        settings = AppSettings.model_validate({"appearance": {"theme": "violet"}})

        self.assertEqual(settings.appearance.theme, "system")


if __name__ == "__main__":
    unittest.main(verbosity=2)
