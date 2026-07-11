"""Verify that a release environment matches the Python 3.11 constraints."""

from __future__ import annotations

import argparse
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from packaging.requirements import Requirement


def verify_constraints(path: Path) -> list[str]:
    errors: list[str] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            requirement = Requirement(line)
        except ValueError as exc:
            errors.append(f"{path}:{line_number}: invalid constraint: {exc}")
            continue
        if requirement.marker and not requirement.marker.evaluate():
            continue
        try:
            installed = version(requirement.name)
        except PackageNotFoundError:
            errors.append(f"missing required release dependency: {requirement.name}")
            continue
        if installed not in requirement.specifier:
            errors.append(
                f"{requirement.name} {installed} does not satisfy {requirement.specifier}"
            )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--constraints", type=Path, required=True)
    args = parser.parse_args()

    if sys.version_info[:2] != (3, 11):
        print(
            f"[ERROR] Release builds require Python 3.11; got {sys.version.split()[0]}",
            file=sys.stderr,
        )
        return 1
    if not args.constraints.is_file():
        print(f"[ERROR] Constraints file not found: {args.constraints}", file=sys.stderr)
        return 1

    errors = verify_constraints(args.constraints)
    if errors:
        for error in errors:
            print(f"[ERROR] {error}", file=sys.stderr)
        return 1
    print(f"[INFO] Release dependencies match {args.constraints}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
