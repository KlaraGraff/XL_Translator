"""Fail release builds when docs/CHANGELOG.md is not updated."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


VERSION_HEADING_RE = re.compile(r"^##\s+V(?P<version>\d+(?:\.\d+)*)\s*$", re.MULTILINE)


def _root_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def _expected_version(version: str | None) -> str:
    if version:
        return version.strip().removeprefix("v").removeprefix("V")
    sys.path.insert(0, str(_root_dir()))
    from app_meta import APP_VERSION

    return APP_VERSION.strip()


def check_changelog(expected_version: str, changelog_path: Path) -> None:
    text = changelog_path.read_text(encoding="utf-8")
    if "## 当前开发版" in text:
        raise AssertionError("docs/CHANGELOG.md still contains an unreleased '当前开发版' section.")

    headings = list(VERSION_HEADING_RE.finditer(text))
    if not headings:
        raise AssertionError("docs/CHANGELOG.md does not contain any '## Vx.y' release headings.")

    latest = headings[0].group("version")
    if latest != expected_version:
        raise AssertionError(
            f"docs/CHANGELOG.md latest heading is V{latest}, expected V{expected_version}. "
            "Add the new release notes at the top before packaging."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", help="Expected release version, for example 6.4.")
    args = parser.parse_args(argv)

    expected_version = _expected_version(args.version)
    changelog_path = _root_dir() / "docs" / "CHANGELOG.md"
    check_changelog(expected_version, changelog_path)
    print(f"CHANGELOG.md release heading is current: V{expected_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
