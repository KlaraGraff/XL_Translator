"""The English/French interface must not silently fall back to Chinese.

Runs scripts/check_i18n.py: every t("…") key in ui/src needs an English and a
French entry with the same placeholders, and no Chinese literal may be shown
without going through t() (unless the line says ``i18n-ignore``).
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class UiCatalogTests(unittest.TestCase):
    def test_every_interface_string_has_english_and_french(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "check_i18n.py")],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stdout[-4000:] + result.stderr[-2000:])


if __name__ == "__main__":
    unittest.main(verbosity=2)
