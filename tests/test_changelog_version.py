from __future__ import annotations

import unittest
from pathlib import Path

from app_meta import APP_VERSION
from scripts.check_changelog_version import check_changelog


class ChangelogVersionTests(unittest.TestCase):
    def test_changelog_latest_heading_matches_app_version(self) -> None:
        changelog_path = Path(__file__).resolve().parents[1] / "docs" / "CHANGELOG.md"

        check_changelog(APP_VERSION, changelog_path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
