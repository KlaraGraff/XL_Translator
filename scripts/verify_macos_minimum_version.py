"""Check the declared and actual minimum macOS versions in an app bundle."""

from __future__ import annotations

import argparse
import plistlib
import re
import subprocess
import sys
from pathlib import Path


_MACHO_MAGICS = {
    b"\xfe\xed\xfa\xce",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
    b"\xca\xfe\xba\xbf",
    b"\xbf\xba\xfe\xca",
}
_VERSION_RE = re.compile(r"^\s*(?:minos|version)\s+(\d+(?:\.\d+){0,2})\s*$")


def _version_tuple(value: str) -> tuple[int, int, int]:
    try:
        parts = tuple(int(part) for part in value.split("."))
    except ValueError as exc:
        raise ValueError(f"invalid macOS version: {value!r}") from exc
    if not 1 <= len(parts) <= 3:
        raise ValueError(f"invalid macOS version: {value!r}")
    return (parts + (0, 0, 0))[:3]


def _is_macho(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            return stream.read(4) in _MACHO_MAGICS
    except OSError:
        return False


def _minimum_versions(path: Path) -> list[str]:
    result = subprocess.run(
        ["otool", "-l", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"otool failed for {path}")

    versions: list[str] = []
    relevant_command = False
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("cmd "):
            relevant_command = stripped in {
                "cmd LC_BUILD_VERSION",
                "cmd LC_VERSION_MIN_MACOSX",
            }
            continue
        if not relevant_command:
            continue
        match = _VERSION_RE.match(line)
        if match:
            versions.append(match.group(1))
            relevant_command = False
    return versions


def _architectures(path: Path) -> set[str]:
    """Return the architecture slices present in a Mach-O file."""
    result = subprocess.run(
        ["lipo", "-archs", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"lipo failed for {path}")
    return set(result.stdout.split())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("app_bundle", type=Path)
    parser.add_argument("--declared", required=True)
    parser.add_argument(
        "--architecture",
        choices=("arm64", "x86_64"),
        help="Require every Mach-O to contain this architecture slice.",
    )
    args = parser.parse_args()

    try:
        declared = _version_tuple(args.declared)
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    info_plist = args.app_bundle / "Contents" / "Info.plist"
    try:
        with info_plist.open("rb") as stream:
            plist = plistlib.load(stream)
    except (OSError, plistlib.InvalidFileException) as exc:
        print(f"[ERROR] Cannot read {info_plist}: {exc}", file=sys.stderr)
        return 1
    plist_minimum = str(plist.get("LSMinimumSystemVersion", "")).strip()
    if plist_minimum != args.declared:
        print(
            "[ERROR] Info.plist LSMinimumSystemVersion "
            f"is {plist_minimum!r}, expected {args.declared!r}",
            file=sys.stderr,
        )
        return 1

    errors: list[str] = []
    checked = 0
    for path in sorted(args.app_bundle.rglob("*")):
        if path.is_symlink() or not path.is_file() or not _is_macho(path):
            continue
        checked += 1
        try:
            versions = _minimum_versions(path)
        except RuntimeError as exc:
            errors.append(str(exc))
            continue
        if args.architecture:
            try:
                architectures = _architectures(path)
            except RuntimeError as exc:
                errors.append(str(exc))
                continue
            if args.architecture not in architectures:
                errors.append(
                    f"{path}: architectures {sorted(architectures)!r} do not include "
                    f"required {args.architecture}"
                )
        if not versions:
            errors.append(f"no macOS minimum load command found: {path}")
            continue
        for actual_text in versions:
            if _version_tuple(actual_text) > declared:
                errors.append(
                    f"{path}: Mach-O minos {actual_text} exceeds declared {args.declared}"
                )

    if checked == 0:
        errors.append(f"no Mach-O binaries found in {args.app_bundle}")
    if errors:
        for error in errors:
            print(f"[ERROR] {error}", file=sys.stderr)
        return 1
    print(
        f"[INFO] Verified {checked} Mach-O binaries against macOS {args.declared}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
