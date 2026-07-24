"""Validate the metadata that identifies an official macOS release."""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
STABLE_TAG_RE = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def is_stable_tag(value: str) -> bool:
    """Return whether *value* is the only tag form eligible for a release."""
    return bool(STABLE_TAG_RE.fullmatch(value.strip()))


def _app_meta_string(path: Path, variable: str) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for statement in tree.body:
        if not isinstance(statement, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == variable
            for target in statement.targets
        ):
            continue
        if isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str):
            return statement.value.value
    raise ValueError(f"{variable} is not a string literal in {path}")


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected an object in {path}")
    return payload


def release_metadata_errors(root: Path = ROOT, *, tag: str | None = None) -> list[str]:
    """Return all static release-metadata errors without invoking a build."""
    paths = {
        "app_meta.py": root / "app_meta.py",
        "src-tauri/tauri.conf.json": root / "src-tauri" / "tauri.conf.json",
        "src-tauri/Cargo.toml": root / "src-tauri" / "Cargo.toml",
        "ui/package.json": root / "ui" / "package.json",
        "ui/vite.config.ts": root / "ui" / "vite.config.ts",
    }
    errors: list[str] = []
    for label, path in paths.items():
        if not path.is_file():
            errors.append(f"missing required release metadata: {label}")
    if errors:
        return errors

    try:
        app_version = _app_meta_string(paths["app_meta.py"], "APP_VERSION")
        app_macos_minimum = _app_meta_string(
            paths["app_meta.py"], "MACOS_MINIMUM_SYSTEM_VERSION"
        )
        tauri = _read_json(paths["src-tauri/tauri.conf.json"])
        cargo = tomllib.loads(paths["src-tauri/Cargo.toml"].read_text(encoding="utf-8"))
        ui = _read_json(paths["ui/package.json"])
    except (OSError, ValueError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        return [f"cannot read release metadata: {exc}"]

    versions = {
        "app_meta.py": app_version,
        "src-tauri/tauri.conf.json": str(tauri.get("version", "")),
        "src-tauri/Cargo.toml": str(cargo.get("package", {}).get("version", "")),
        "ui/package.json": str(ui.get("version", "")),
    }
    expected = app_version
    for label, actual in versions.items():
        if actual != expected:
            errors.append(f"version mismatch: {label} is {actual!r}, expected {expected!r}")

    if app_macos_minimum != "12.0":
        errors.append("app_meta.py MACOS_MINIMUM_SYSTEM_VERSION must be exactly '12.0'")

    macos = tauri.get("bundle", {}).get("macOS", {})
    if not isinstance(macos, dict) or macos.get("minimumSystemVersion") != "12.0":
        errors.append("tauri macOS minimumSystemVersion must be exactly '12.0'")
    if "windows" in tauri.get("bundle", {}):
        errors.append("tauri bundle must not contain a Windows release configuration")
    if "safari15.1" not in paths["ui/vite.config.ts"].read_text(encoding="utf-8"):
        errors.append("Vite build target must retain the Safari 15.1 Monterey baseline")

    if tag is not None:
        normalized_tag = tag.strip()
        if not is_stable_tag(normalized_tag):
            errors.append(f"official releases require a stable vX.Y.Z tag, got {tag!r}")
        elif normalized_tag[1:] != expected:
            errors.append(
                f"tag version {normalized_tag[1:]!r} does not match app version {expected!r}"
            )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--tag", help="Official tag to validate, for example v8.0.0")
    args = parser.parse_args()

    errors = release_metadata_errors(args.root.resolve(), tag=args.tag)
    if errors:
        for error in errors:
            print(f"[ERROR] {error}", file=sys.stderr)
        return 1
    print("[INFO] Release metadata is consistent for macOS 12.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
