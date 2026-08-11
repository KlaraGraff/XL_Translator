"""Contracts for the in-app updater manifest (latest.json)."""

from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path

from app_meta import (
    APP_VERSION,
    macos_updater_archive_name,
    windows_updater_artifact_name,
)
from scripts.build_updater_manifest import (
    MACOS_PLATFORM_KEY,
    WINDOWS_PLATFORM_KEY,
    ManifestError,
    build_manifest,
    configured_public_key_id,
)

PUB_DATE = "2026-08-11T09:00:00Z"

# The key id every fake signature in this file claims to come from.  Real ids
# are random 8-byte values; only equality with the configured pubkey matters.
TEST_KEY_ID = bytes.fromhex("0102030405060708")


def _signature_blob(
    key_id: bytes = TEST_KEY_ID,
    comment: str = "signature from tauri secret key",
) -> str:
    """Return a plausible `tauri signer sign` output: base64 of a minisign file.

    The payload layout is minisign's: a 2-byte algorithm tag, the 8-byte key id,
    then the signature body.  Only the first ten bytes are inspected anywhere in
    the pipeline, so the body is filler.
    """
    payload = base64.b64encode(b"Ed" + key_id + b"\x00" * 64).decode("ascii")
    raw = f"untrusted comment: {comment}\n{payload}\n"
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")


def _stage_assets(directory: Path, *, macos: bool = True, windows: bool = True) -> None:
    if macos:
        name = macos_updater_archive_name("arm64")
        (directory / name).write_bytes(b"macos update archive")
        (directory / f"{name}.sig").write_text(_signature_blob(), encoding="utf-8")
    if windows:
        name = windows_updater_artifact_name()
        (directory / name).write_bytes(b"windows installer")
        (directory / f"{name}.sig").write_text(_signature_blob(), encoding="utf-8")


def _build(assets: Path, tag: str = f"v{APP_VERSION}") -> dict[str, object]:
    return build_manifest(
        assets_dir=assets, tag=tag, pub_date=PUB_DATE, public_key_id=TEST_KEY_ID
    )


class UpdaterManifestTests(unittest.TestCase):
    def test_manifest_carries_both_platforms_with_release_download_urls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            assets = Path(tmp)
            _stage_assets(assets)
            manifest = _build(assets)

        self.assertEqual(manifest["version"], APP_VERSION)
        self.assertEqual(manifest["pub_date"], PUB_DATE)
        platforms = manifest["platforms"]
        self.assertEqual(set(platforms), {MACOS_PLATFORM_KEY, WINDOWS_PLATFORM_KEY})
        self.assertTrue(
            platforms[MACOS_PLATFORM_KEY]["url"].endswith(
                f"/download/v{APP_VERSION}/{macos_updater_archive_name('arm64')}"
            )
        )
        # Windows updates download the installer itself — Tauri v2 produces no
        # separate archive for NSIS, so a manifest pointing at one would 404.
        self.assertTrue(
            platforms[WINDOWS_PLATFORM_KEY]["url"].endswith(
                f"/download/v{APP_VERSION}/{windows_updater_artifact_name()}"
            )
        )
        self.assertTrue(platforms[WINDOWS_PLATFORM_KEY]["url"].endswith(".exe"))
        for entry in platforms.values():
            self.assertTrue(entry["url"].startswith("https://github.com/"))
            self.assertTrue(entry["signature"])
        # The manifest has to survive a JSON round trip untouched: the updater
        # parses it verbatim from the release asset.
        self.assertEqual(json.loads(json.dumps(manifest)), manifest)

    def test_a_platform_without_artifacts_fails_the_release(self) -> None:
        # A manifest that quietly drops Windows would tell every Windows user
        # "you are up to date" until someone noticed months later.
        with tempfile.TemporaryDirectory() as tmp:
            assets = Path(tmp)
            _stage_assets(assets, windows=False)
            with self.assertRaises(ManifestError):
                _build(assets)

    def test_missing_signature_fails_even_when_the_artifact_is_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            assets = Path(tmp)
            _stage_assets(assets)
            (assets / f"{windows_updater_artifact_name()}.sig").unlink()
            with self.assertRaises(ManifestError):
                _build(assets)

    def test_corrupted_signature_fails_before_users_see_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            assets = Path(tmp)
            _stage_assets(assets)
            (assets / f"{macos_updater_archive_name('arm64')}.sig").write_text(
                "not base64 at all !!", encoding="utf-8"
            )
            with self.assertRaises(ManifestError):
                _build(assets)

    def test_signature_from_another_key_fails_the_release(self) -> None:
        # The Tauri CLI only *warns* when the signing key does not match the
        # shipped pubkey, so a rotated or mistyped secret otherwise produces a
        # fully green release that no user can install.
        with tempfile.TemporaryDirectory() as tmp:
            assets = Path(tmp)
            _stage_assets(assets)
            (assets / f"{macos_updater_archive_name('arm64')}.sig").write_text(
                _signature_blob(key_id=bytes.fromhex("aabbccddeeff0011")), encoding="utf-8"
            )
            with self.assertRaises(ManifestError) as caught:
                _build(assets)
        self.assertIn("key id", str(caught.exception))

    def test_tag_must_match_the_app_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            assets = Path(tmp)
            _stage_assets(assets)
            with self.assertRaises(ManifestError):
                _build(assets, tag="v0.0.1")

    def test_the_shipped_pubkey_yields_a_key_id(self) -> None:
        # Guards the real tauri.conf.json: a truncated or re-wrapped pubkey
        # would only surface at install time on a user's machine.
        self.assertEqual(len(configured_public_key_id()), 8)


if __name__ == "__main__":
    unittest.main()
