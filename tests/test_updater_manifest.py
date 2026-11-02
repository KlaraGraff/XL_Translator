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
    _minisign_key_id,
    _read_signature,
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


def _wrapped_minisign(inner: str) -> str:
    """按 signer 的层级包一层：minisign 文件再整体 base64 一次。"""
    return base64.b64encode(inner.encode("utf-8")).decode("ascii")


class MinisignParsingTests(unittest.TestCase):
    """_minisign_key_id 的每个失败分支。

    这个函数是「签名的钥匙」和「应用认的钥匙」之间唯一的对账点：它认错了不会
    报错，只会让整版更新在所有用户的机器上装不上。所以每条拒绝理由都单独钉住，
    免得哪天被改成「解析不了就当一样」。
    """

    def test_wrapped_file_is_not_base64(self) -> None:
        with self.assertRaises(ManifestError) as caught:
            _minisign_key_id("!!! not base64 !!!", label="updater pubkey")
        self.assertIn("not valid base64", str(caught.exception))

    def test_wrapped_file_has_no_untrusted_comment_line(self) -> None:
        with self.assertRaises(ManifestError) as caught:
            _minisign_key_id(
                base64.b64encode(b"hello world\n").decode("ascii"),
                label="updater pubkey",
            )
        self.assertIn("does not look like a minisign file", str(caught.exception))

    def test_file_with_only_the_comment_line_has_no_payload(self) -> None:
        with self.assertRaises(ManifestError) as caught:
            _minisign_key_id(
                _wrapped_minisign("untrusted comment: only a comment\n"),
                label="updater pubkey",
            )
        self.assertIn("no minisign payload line", str(caught.exception))

    def test_payload_line_is_not_base64(self) -> None:
        with self.assertRaises(ManifestError) as caught:
            _minisign_key_id(
                _wrapped_minisign("untrusted comment: x\n@@@@\n"),
                label="updater pubkey",
            )
        self.assertIn("payload is not valid base64", str(caught.exception))

    def test_payload_too_short_to_carry_a_key_id(self) -> None:
        # 2 字节算法标签之后才是 8 字节 key id；短一位就只能读出一半。
        short = base64.b64encode(b"Ed\x01\x02").decode("ascii")
        with self.assertRaises(ManifestError) as caught:
            _minisign_key_id(
                _wrapped_minisign(f"untrusted comment: x\n{short}\n"),
                label="updater pubkey",
            )
        self.assertIn("too short", str(caught.exception))

    def test_label_reaches_the_message(self) -> None:
        # 两个输入（公钥 / 每个 .sig）走同一个函数，报错必须说清是哪一个坏了。
        with self.assertRaises(ManifestError) as caught:
            _minisign_key_id("nope", label="signature Translator_10.0.0.exe.sig")
        self.assertIn("signature Translator_10.0.0.exe.sig", str(caught.exception))

    def test_key_id_is_the_eight_bytes_after_the_algorithm_tag(self) -> None:
        key_id = bytes.fromhex("0011223344556677")
        blob = _signature_blob(key_id=key_id)
        self.assertEqual(_minisign_key_id(blob, label="signature"), key_id)


class ConfiguredPublicKeyTests(unittest.TestCase):
    def _config(self, directory: Path, payload: object) -> Path:
        path = directory / "tauri.conf.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_unreadable_config_is_reported_with_its_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ManifestError) as caught:
                configured_public_key_id(Path(tmp) / "missing.json")
        self.assertIn("cannot read", str(caught.exception))

    def test_malformed_json_is_reported_instead_of_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tauri.conf.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(ManifestError) as caught:
                configured_public_key_id(path)
        self.assertIn("cannot read", str(caught.exception))

    def test_missing_pubkey_stops_before_any_signature_is_compared(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._config(Path(tmp), {"plugins": {"updater": {}}})
            with self.assertRaises(ManifestError) as caught:
                configured_public_key_id(path)
        self.assertIn("plugins.updater.pubkey", str(caught.exception))

    def test_blank_pubkey_counts_as_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._config(Path(tmp), {"plugins": {"updater": {"pubkey": "   "}}})
            with self.assertRaises(ManifestError):
                configured_public_key_id(path)


class SignatureReadingTests(unittest.TestCase):
    def test_missing_archive_is_reported_by_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ManifestError) as caught:
                _read_signature(Path(tmp), "gone.tar.gz", public_key_id=TEST_KEY_ID)
        self.assertIn("update artifact is missing", str(caught.exception))

    def test_empty_archive_is_rejected(self) -> None:
        # 0 字节的压缩包能通过「文件存在」这一关，装的时候才炸。
        with tempfile.TemporaryDirectory() as tmp:
            assets = Path(tmp)
            (assets / "archive.tar.gz").write_bytes(b"")
            with self.assertRaises(ManifestError) as caught:
                _read_signature(assets, "archive.tar.gz", public_key_id=TEST_KEY_ID)
        self.assertIn("update artifact is empty", str(caught.exception))

    def test_blank_signature_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            assets = Path(tmp)
            name = windows_updater_artifact_name()
            (assets / name).write_bytes(b"installer")
            (assets / f"{name}.sig").write_text("\n  \n", encoding="utf-8")
            with self.assertRaises(ManifestError) as caught:
                _read_signature(assets, name, public_key_id=TEST_KEY_ID)
        self.assertIn("update signature is empty", str(caught.exception))

    def test_matching_signature_is_returned_verbatim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            assets = Path(tmp)
            name = macos_updater_archive_name("arm64")
            (assets / name).write_bytes(b"archive")
            signature = _signature_blob()
            (assets / f"{name}.sig").write_text(f"  {signature}  \n", encoding="utf-8")
            self.assertEqual(
                _read_signature(assets, name, public_key_id=TEST_KEY_ID),
                signature,
            )


class ManifestInputValidationTests(unittest.TestCase):
    def test_tag_without_the_v_prefix_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ManifestError) as caught:
                build_manifest(
                    assets_dir=Path(tmp),
                    tag=APP_VERSION,
                    pub_date=PUB_DATE,
                    public_key_id=TEST_KEY_ID,
                )
        self.assertIn("must look like vX.Y.Z", str(caught.exception))

    def test_tag_padding_is_tolerated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            assets = Path(tmp)
            _stage_assets(assets)
            manifest = build_manifest(
                assets_dir=assets,
                tag=f"  v{APP_VERSION}\n",
                pub_date=PUB_DATE,
                public_key_id=TEST_KEY_ID,
            )
        self.assertEqual(manifest["version"], APP_VERSION)
        self.assertNotIn(" ", manifest["platforms"][MACOS_PLATFORM_KEY]["url"])

    def test_manifest_uses_the_repository_given(self) -> None:
        # 仓库名写错时下载地址全部 404，而签名和版本号都是对的，看起来完全正常。
        with tempfile.TemporaryDirectory() as tmp:
            assets = Path(tmp)
            _stage_assets(assets)
            manifest = build_manifest(
                assets_dir=assets,
                tag=f"v{APP_VERSION}",
                pub_date=PUB_DATE,
                public_key_id=TEST_KEY_ID,
                repo="someone/fork",
            )
        for entry in manifest["platforms"].values():
            self.assertTrue(
                entry["url"].startswith(
                    "https://github.com/someone/fork/releases/download/"
                )
            )


if __name__ == "__main__":
    unittest.main()
