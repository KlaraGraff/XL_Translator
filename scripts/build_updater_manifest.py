"""Assemble ``latest.json`` — the manifest the in-app updater polls.

The updater plugin fetches one static JSON file from the newest published
release and compares its ``version`` against the running build.  For every
platform it needs two things: where to download the update archive, and the
minisign signature of that exact archive.  Without a matching signature the
plugin refuses to install, which is the whole point — the download comes from
a URL, but the trust comes from a key that never leaves the maintainer's
machine.

This script runs in the release job, after both platform jobs have uploaded
their archives and ``.sig`` files.  It refuses to emit a half-filled manifest:
a manifest missing the Windows entry would silently tell every Windows user
"you are up to date" forever.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app_meta import (  # noqa: E402
    APP_VERSION,
    macos_updater_archive_name,
    windows_updater_artifact_name,
)

GITHUB_REPO = "KlaraGraff/XL_Translator"
MACOS_ARCHITECTURE = "arm64"
TAURI_CONFIG = ROOT / "src-tauri" / "tauri.conf.json"

# The plugin's own platform keys.  These are not free-form labels: the updater
# looks up exactly the string built from the target triple it was compiled for.
MACOS_PLATFORM_KEY = "darwin-aarch64"
WINDOWS_PLATFORM_KEY = "windows-x86_64"

# The About page renders the release notes it gets from the GitHub API, not
# this field, so there is nothing to keep in sync here.  Anything longer would
# be a second copy of the notes that no one reads and everyone forgets to
# update.
MANIFEST_NOTES = "详细更新说明见本次 Release 页面。"


class ManifestError(RuntimeError):
    """A manifest input is missing or unusable."""


def _minisign_key_id(blob: str, *, label: str) -> bytes:
    """Return the 8-byte key id embedded in a base64-wrapped minisign file.

    Both the public key and the signature are stored the same way: a two-line
    minisign file (``untrusted comment:`` then one base64 payload), and *that
    whole file* is base64-encoded again — by ``tauri signer sign`` for the
    signature, and by hand into ``tauri.conf.json`` for the pubkey.  The inner
    payload starts with a 2-byte algorithm tag followed by the key id, which is
    what ties a signature to the key that will be asked to verify it.
    """
    try:
        outer = base64.b64decode(blob, validate=True).decode("utf-8")
    except Exception as exc:  # noqa: BLE001 - any decode failure is fatal here.
        raise ManifestError(f"{label} is not valid base64") from exc
    if "untrusted comment:" not in outer:
        raise ManifestError(f"{label} does not look like a minisign file")
    payload = ""
    for line in outer.splitlines():
        line = line.strip()
        if not line or line.startswith("untrusted comment:"):
            continue
        payload = line
        break
    if not payload:
        raise ManifestError(f"{label} has no minisign payload line")
    try:
        raw = base64.b64decode(payload, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise ManifestError(f"{label} payload is not valid base64") from exc
    if len(raw) < 10:
        raise ManifestError(f"{label} payload is too short to carry a key id")
    return raw[2:10]


def configured_public_key_id(config_path: Path = TAURI_CONFIG) -> bytes:
    """Return the key id of the pubkey the shipped app will verify against."""
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot read {config_path}: {exc}") from exc
    pubkey = str(config.get("plugins", {}).get("updater", {}).get("pubkey") or "").strip()
    if not pubkey:
        raise ManifestError(f"{config_path} carries no plugins.updater.pubkey")
    return _minisign_key_id(pubkey, label="updater pubkey")


def _read_signature(assets_dir: Path, archive_name: str, *, public_key_id: bytes) -> str:
    """Return the minisign signature for *archive_name*, validating both files."""
    archive = assets_dir / archive_name
    signature_path = assets_dir / f"{archive_name}.sig"
    if not archive.is_file():
        raise ManifestError(f"update artifact is missing: {archive}")
    if archive.stat().st_size == 0:
        raise ManifestError(f"update artifact is empty: {archive}")
    if not signature_path.is_file():
        raise ManifestError(f"update signature is missing: {signature_path}")

    signature = signature_path.read_text(encoding="utf-8").strip()
    if not signature:
        raise ManifestError(f"update signature is empty: {signature_path}")
    # Signing with the wrong key is not a build error anywhere upstream: the
    # Tauri CLI only logs a warning on a key mismatch, so a rotated or
    # mistyped secret produces a fully green release that fails on every
    # user's machine at install time.  Compare the key ids here, where it
    # still costs nothing to stop.
    signature_key_id = _minisign_key_id(signature, label=f"signature {signature_path.name}")
    if signature_key_id != public_key_id:
        raise ManifestError(
            f"{signature_path.name} was signed with key id {signature_key_id.hex()}, but the app "
            f"verifies against {public_key_id.hex()} — the release would fail to install for "
            "every user"
        )
    return signature


def build_manifest(
    *,
    assets_dir: Path,
    tag: str,
    pub_date: str,
    version: str = APP_VERSION,
    repo: str = GITHUB_REPO,
    public_key_id: bytes | None = None,
) -> dict[str, object]:
    """Return the manifest payload, or raise when an artifact is missing."""
    normalized_tag = tag.strip()
    if not normalized_tag.startswith("v"):
        raise ManifestError(f"release tag must look like vX.Y.Z, got {tag!r}")
    if normalized_tag[1:] != version:
        raise ManifestError(
            f"tag version {normalized_tag[1:]!r} does not match app version {version!r}"
        )

    key_id = configured_public_key_id() if public_key_id is None else public_key_id
    platforms = {
        MACOS_PLATFORM_KEY: macos_updater_archive_name(MACOS_ARCHITECTURE),
        WINDOWS_PLATFORM_KEY: windows_updater_artifact_name(),
    }
    return {
        "version": version,
        "notes": MANIFEST_NOTES,
        "pub_date": pub_date,
        "platforms": {
            key: {
                "signature": _read_signature(assets_dir, artifact, public_key_id=key_id),
                "url": f"https://github.com/{repo}/releases/download/{normalized_tag}/{artifact}",
            }
            for key, artifact in platforms.items()
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets-dir", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument(
        "--pub-date",
        required=True,
        help="RFC 3339 publication timestamp, for example 2026-08-11T09:00:00Z",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    try:
        manifest = build_manifest(
            assets_dir=args.assets_dir.resolve(),
            tag=args.tag,
            pub_date=args.pub_date,
        )
    except ManifestError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"[INFO] Updater manifest written: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
