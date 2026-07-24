"""Check the declared and actual minimum macOS versions in an app bundle."""

from __future__ import annotations

import argparse
import json
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


def verify_app_bundle(
    app_bundle: Path,
    *,
    declared: str,
    architecture: str | None = None,
) -> dict[str, object]:
    """Scan every Mach-O inside an app and return an auditable verification report."""
    report: dict[str, object] = {
        "app_bundle": str(app_bundle),
        "declared_macos_minimum": declared,
        "required_architecture": architecture,
        "checked_macho_count": 0,
        "binaries": [],
        "errors": [],
    }
    errors = report["errors"]
    binaries = report["binaries"]
    assert isinstance(errors, list)
    assert isinstance(binaries, list)
    try:
        declared_version = _version_tuple(declared)
    except ValueError as exc:
        errors.append(str(exc))
        return report

    info_plist = app_bundle / "Contents" / "Info.plist"
    try:
        with info_plist.open("rb") as stream:
            plist = plistlib.load(stream)
    except (OSError, plistlib.InvalidFileException) as exc:
        errors.append(f"Cannot read {info_plist}: {exc}")
        return report
    plist_minimum = str(plist.get("LSMinimumSystemVersion", "")).strip()
    report["info_plist_minimum"] = plist_minimum
    if plist_minimum != declared:
        errors.append(
            "Info.plist LSMinimumSystemVersion "
            f"is {plist_minimum!r}, expected {declared!r}"
        )

    for path in sorted(app_bundle.rglob("*")):
        if path.is_symlink() or not path.is_file() or not _is_macho(path):
            continue
        report["checked_macho_count"] = int(report["checked_macho_count"]) + 1
        relative = str(path.relative_to(app_bundle))
        binary: dict[str, object] = {"path": relative, "minos": [], "architectures": []}
        binaries.append(binary)
        try:
            versions = _minimum_versions(path)
            binary["minos"] = versions
        except RuntimeError as exc:
            errors.append(str(exc))
            continue
        if architecture:
            try:
                architectures = _architectures(path)
                binary["architectures"] = sorted(architectures)
            except RuntimeError as exc:
                errors.append(str(exc))
                continue
            if architecture not in architectures:
                errors.append(
                    f"{path}: architectures {sorted(architectures)!r} do not include "
                    f"required {architecture}"
                )
        if not versions:
            errors.append(f"no macOS minimum load command found: {path}")
            continue
        for actual_text in versions:
            if _version_tuple(actual_text) > declared_version:
                errors.append(
                    f"{path}: Mach-O minos {actual_text} exceeds declared {declared}"
                )

    if report["checked_macho_count"] == 0:
        errors.append(f"no Mach-O binaries found in {app_bundle}")
    report["ok"] = not errors
    return report


def _write_report(path: Path, report: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("app_bundle", type=Path)
    parser.add_argument("--declared", required=True)
    parser.add_argument(
        "--architecture",
        choices=("arm64", "x86_64"),
        help="Require every Mach-O to contain this architecture slice.",
    )
    parser.add_argument("--report", type=Path, help="Write the full Mach-O scan report as JSON.")
    args = parser.parse_args()

    report = verify_app_bundle(
        args.app_bundle,
        declared=args.declared,
        architecture=args.architecture,
    )
    if args.report:
        _write_report(args.report, report)
    errors = report["errors"]
    assert isinstance(errors, list)
    if errors:
        for error in errors:
            print(f"[ERROR] {error}", file=sys.stderr)
        return 1
    print(
        "[INFO] Verified "
        f"{report['checked_macho_count']} Mach-O binaries against macOS {args.declared}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
