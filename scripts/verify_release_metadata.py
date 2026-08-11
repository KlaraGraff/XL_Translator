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

# 见 core/_embedded_key.py 顶部注释：这是开发用兜底密钥的标记，任何携带它的
# 构建都没有真实的模型配置加密保护。
_DEV_EMBEDDED_KEY_ID = "dev"

# A UI constant named like ``APP_VERSION_FALLBACK`` (or ``appVersion``, etc.)
# hand-copied to a literal "X.Y.Z" string is exactly how the "About" page once
# drifted to a stale version (it showed "8.1.2" while the app had moved on to
# 9.1.0, and nothing caught it because this script only checked app_meta.py /
# tauri.conf.json / Cargo.toml / package.json, never the UI source). Ban the
# pattern outright: any *VERSION*-named binding in ui/src must be sourced from
# ui/package.json (already one of the four files checked above) at build time,
# not typed out by hand.
_UI_HARDCODED_VERSION_RE = re.compile(
    r"\b[A-Za-z_][A-Za-z0-9_]*version[A-Za-z0-9_]*\b[^=\n]*=\s*[\"'](\d+\.\d+\.\d+)[\"']",
    re.IGNORECASE,
)


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


def ui_source_version_literal_errors(root: Path) -> list[str]:
    """Flag any hand-copied "X.Y.Z" version literal in the UI TypeScript source.

    The UI has no build step that cross-checks a literal against app_meta.py,
    so any such literal is one release away from going stale. The fix is
    always the same: derive it from ui/package.json instead (whose version is
    already gated against app_meta.py by ``release_metadata_errors`` above).
    """
    ui_src_root = root / "ui" / "src"
    errors: list[str] = []
    if not ui_src_root.is_dir():
        return errors
    for path in sorted(ui_src_root.rglob("*.ts")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            errors.append(f"cannot read UI source for version scan: {path}: {exc}")
            continue
        relative = path.relative_to(root)
        for line_number, line in enumerate(lines, start=1):
            match = _UI_HARDCODED_VERSION_RE.search(line)
            if match:
                errors.append(
                    f"hardcoded version literal {match.group(1)!r} in {relative}:{line_number} — "
                    "source it from ui/package.json (import or build-time inject) instead of "
                    "hand-copying it, or it will drift on the next release"
                )
    return errors


def updater_wiring_errors(
    root: Path,
    *,
    tauri: dict[str, Any],
    cargo: dict[str, Any],
    ui_package: dict[str, Any],
) -> list[str]:
    """Check that in-app updating is wired end to end, or not claimed at all.

    Every piece below is individually silent when it goes missing: the app
    still builds, still launches, and still reports "已是最新" — it just never
    updates anyone again. The combination is what has to hold, so it is checked
    as a unit before every release.
    """
    errors: list[str] = []
    updater = tauri.get("plugins", {})
    updater = updater.get("updater", {}) if isinstance(updater, dict) else {}
    if not isinstance(updater, dict) or not updater:
        return ["tauri.conf.json is missing the plugins.updater configuration"]

    if not str(updater.get("pubkey") or "").strip():
        errors.append("tauri updater config must carry a non-empty pubkey")
    endpoints = updater.get("endpoints")
    expected_endpoint = (
        "https://github.com/KlaraGraff/XL_Translator/releases/latest/download/latest.json"
    )
    if not isinstance(endpoints, list) or endpoints != [expected_endpoint]:
        errors.append(
            f"tauri updater endpoints must be exactly [{expected_endpoint!r}], got {endpoints!r}"
        )

    dependencies = cargo.get("dependencies", {})
    for crate in ("tauri-plugin-updater", "tauri-plugin-process"):
        if crate not in dependencies:
            errors.append(f"src-tauri/Cargo.toml must depend on {crate}")

    ui_dependencies = ui_package.get("dependencies", {})
    for package in ("@tauri-apps/plugin-updater", "@tauri-apps/plugin-process"):
        if package not in ui_dependencies:
            errors.append(f"ui/package.json must depend on {package}")

    capabilities_path = root / "src-tauri" / "capabilities" / "default.json"
    try:
        capabilities = _read_json(capabilities_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return [*errors, f"cannot read {capabilities_path}: {exc}"]
    permissions = capabilities.get("permissions", [])
    # Without these two the plugins are compiled in but every call from the UI
    # is rejected at runtime — the button exists and does nothing.
    for permission in ("updater:default", "process:allow-restart"):
        if permission not in permissions:
            errors.append(f"src-tauri/capabilities/default.json must grant {permission}")
    return errors


def embedded_key_errors(root: Path) -> list[str]:
    """Flag a release that still carries the public "dev" fallback key.

    ``scripts/inject_embedded_key.py`` is supposed to overwrite
    ``core/_embedded_key.py`` with the real key before packaging. If that step
    was skipped — script not run, or the ``XLT_EMBEDDED_KEY_ID`` /
    ``XLT_EMBEDDED_PRIVATE_KEY_B64`` repo secrets were never configured — the
    build silently keeps the dev key, and every exported model config would
    be "encrypted" with a key anyone can read out of the public source tree.
    That is a silent security incident, so it must fail the release outright.
    """
    embedded_key_path = root / "core" / "_embedded_key.py"
    if not embedded_key_path.is_file():
        return ["missing required release metadata: core/_embedded_key.py"]

    try:
        tree = ast.parse(embedded_key_path.read_text(encoding="utf-8"), filename=str(embedded_key_path))
    except (OSError, SyntaxError) as exc:
        return [f"cannot read core/_embedded_key.py: {exc}"]

    key_id: str | None = None
    for statement in tree.body:
        if not isinstance(statement, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "EMBEDDED_KEY_ID"
            for target in statement.targets
        ):
            continue
        if isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str):
            key_id = statement.value.value

    if key_id is None:
        return ["core/_embedded_key.py must define EMBEDDED_KEY_ID as a string literal"]

    if key_id == _DEV_EMBEDDED_KEY_ID:
        return [
            "core/_embedded_key.py 仍是开发用兜底密钥（EMBEDDED_KEY_ID == 'dev'）："
            "发布产物会用任何人都能读到的密钥加密模型配置。检查构建流程是否漏跑了 "
            "scripts/inject_embedded_key.py，或者仓库没有配置 XLT_EMBEDDED_KEY_ID / "
            "XLT_EMBEDDED_PRIVATE_KEY_B64 这两个 secret。"
        ]
    return []


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

    errors.extend(
        updater_wiring_errors(root, tauri=tauri, cargo=cargo, ui_package=ui)
    )
    errors.extend(ui_source_version_literal_errors(root))

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
    parser.add_argument(
        "--require-embedded-key",
        action="store_true",
        help=(
            "Also fail unless core/_embedded_key.py has been overwritten with a real "
            "key by scripts/inject_embedded_key.py. Only meaningful after packaging in "
            "a platform build job, not in the pre-build validate-release job (which "
            "runs before injection ever happens)."
        ),
    )
    args = parser.parse_args()

    root = args.root.resolve()
    errors = release_metadata_errors(root, tag=args.tag)
    if args.require_embedded_key:
        errors.extend(embedded_key_errors(root))
    if errors:
        for error in errors:
            print(f"[ERROR] {error}", file=sys.stderr)
        return 1
    print("[INFO] Release metadata is consistent for macOS 12.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
