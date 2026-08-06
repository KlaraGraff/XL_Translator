from __future__ import annotations

import unittest
from unittest.mock import patch

from core.update_checker import (
    build_update_result_from_release_payload,
    check_for_updates,
    is_major_upgrade,
    is_newer_version,
    major_version,
    normalized_architecture,
)


SHA256 = "a" * 64


def _release_payload(*, version: str = "8.1.0", include_digest: bool = True) -> dict:
    def asset(name: str, url: str) -> dict:
        payload = {"name": name, "browser_download_url": url}
        if include_digest and (name.endswith(".dmg") or name.endswith(".exe")):
            payload["digest"] = f"sha256:{SHA256}"
        return payload

    return {
        "tag_name": f"v{version}",
        "html_url": f"https://example.test/releases/v{version}",
        "published_at": "2026-07-24T12:00:00Z",
        "body": "- release notes",
        "assets": [
            asset(
                f"Translator_macOS_arm64_{version}.dmg",
                f"https://example.test/{version}/arm64.dmg",
            ),
            asset(
                f"Translator_macOS_arm64_{version}.dmg.sha256",
                f"https://example.test/{version}/arm64.dmg.sha256",
            ),
            asset(
                f"Translator_Windows_x64_{version}_Setup.exe",
                f"https://example.test/{version}/windows-x64-setup.exe",
            ),
            asset(
                f"Translator_Windows_x64_{version}_Setup.exe.sha256",
                f"https://example.test/{version}/windows-x64-setup.exe.sha256",
            ),
        ],
    }


class _Response:
    def __init__(self, *, payload: dict | None = None, text: str = "") -> None:
        self._payload = payload
        self.text = text

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        assert self._payload is not None
        return self._payload


class _ReleaseClient:
    def __init__(self, release: dict, checksum: str) -> None:
        self.release = release
        self.checksum = checksum
        self.request_urls: list[str] = []

    def __enter__(self) -> _ReleaseClient:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def get(self, url: str, **_kwargs: object) -> _Response:
        self.request_urls.append(url)
        if url.endswith(".sha256"):
            return _Response(text=self.checksum)
        return _Response(payload=self.release)


class UpdateCheckerTests(unittest.TestCase):
    def test_version_compare_treats_patchless_and_patch_versions_as_equal(self) -> None:
        self.assertFalse(is_newer_version("v4.1.0", "4.1"))
        self.assertTrue(is_newer_version("v4.2", "4.1.9"))

    def test_major_version_helpers_require_parseable_semver(self) -> None:
        self.assertEqual(major_version("v5.1.2"), 5)
        self.assertTrue(is_major_upgrade("6.0", "5.2.1"))
        self.assertFalse(is_major_upgrade("5.3", "5.2.1"))
        self.assertIsNone(major_version("release-2026.05"))

    def test_normalized_architecture_mac_apple_silicon_only(self) -> None:
        self.assertEqual(
            normalized_architecture("arm64", platform_name="Darwin"), "arm64"
        )
        self.assertEqual(
            normalized_architecture("aarch64", platform_name="Darwin"), "arm64"
        )
        # Intel Macs are no longer published; no asset is offered.
        self.assertIsNone(normalized_architecture("x86_64", platform_name="Darwin"))

    def test_normalized_architecture_windows_machine_codes(self) -> None:
        for machine in ("amd64", "AMD64", "x86_64", "x64"):
            self.assertEqual(
                normalized_architecture(machine, platform_name="Windows"), "x64"
            )
        self.assertIsNone(normalized_architecture("arm64", platform_name="Windows"))

    def test_normalized_architecture_other_platforms_are_unsupported(self) -> None:
        self.assertIsNone(normalized_architecture("x86_64", platform_name="Linux"))

    def test_selects_only_the_matching_native_dmg_and_checksum(self) -> None:
        payload = _release_payload()
        result = build_update_result_from_release_payload(
            payload,
            current_version="8.0.0",
            platform_name="Darwin",
            machine="arm64",
        )

        self.assertTrue(result.has_update)
        self.assertEqual(result.status, "available")
        self.assertEqual(result.architecture, "arm64")
        self.assertEqual(result.asset_name, "Translator_macOS_arm64_8.1.0.dmg")
        self.assertEqual(result.checksum_asset_name, "Translator_macOS_arm64_8.1.0.dmg.sha256")
        self.assertEqual(result.sha256, SHA256)
        self.assertEqual(result.release_notes, "- release notes")

    def test_windows_selects_installer_and_checksum(self) -> None:
        payload = _release_payload()
        result = build_update_result_from_release_payload(
            payload,
            current_version="8.0.0",
            platform_name="Windows",
            machine="AMD64",
        )

        self.assertTrue(result.has_update)
        self.assertEqual(result.status, "available")
        self.assertEqual(result.architecture, "x64")
        self.assertEqual(result.asset_name, "Translator_Windows_x64_8.1.0_Setup.exe")
        self.assertEqual(
            result.checksum_asset_name, "Translator_Windows_x64_8.1.0_Setup.exe.sha256"
        )
        self.assertEqual(result.sha256, SHA256)

    def test_mac_intel_host_is_unsupported(self) -> None:
        result = build_update_result_from_release_payload(
            _release_payload(),
            current_version="8.0.0",
            platform_name="Darwin",
            machine="x86_64",
        )

        self.assertEqual(result.status, "unsupported_platform")
        self.assertFalse(result.has_update)
        self.assertEqual(result.download_url, "")

    def test_asset_name_not_matching_current_version_returns_no_update_asset(self) -> None:
        # Release is tagged 8.1.0 but assets still carry the previous version's
        # name (a partially-published release); no asset should ever match.
        payload = _release_payload(version="8.1.0")
        payload["assets"] = [
            {
                "name": "Translator_macOS_arm64_8.0.9.dmg",
                "browser_download_url": "https://example.test/stale/arm64.dmg",
                "digest": f"sha256:{SHA256}",
            },
            {
                "name": "Translator_macOS_arm64_8.0.9.dmg.sha256",
                "browser_download_url": "https://example.test/stale/arm64.dmg.sha256",
            },
        ]
        result = build_update_result_from_release_payload(
            payload,
            current_version="8.0.0",
            platform_name="Darwin",
            machine="arm64",
        )
        self.assertEqual(result.status, "release_not_ready")
        self.assertEqual(result.download_url, "")

    def test_missing_or_wrong_architecture_checksum_never_offers_update(self) -> None:
        payload = _release_payload()
        payload["assets"] = [
            asset
            for asset in payload["assets"]
            if asset["name"] != "Translator_macOS_arm64_8.1.0.dmg.sha256"
        ]
        result = build_update_result_from_release_payload(
            payload,
            current_version="8.0.0",
            platform_name="Darwin",
            machine="arm64",
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "release_not_ready")
        self.assertFalse(result.has_update)
        self.assertEqual(result.download_url, "")

    def test_windows_missing_checksum_never_offers_update(self) -> None:
        payload = _release_payload()
        payload["assets"] = [
            asset
            for asset in payload["assets"]
            if asset["name"] != "Translator_Windows_x64_8.1.0_Setup.exe.sha256"
        ]
        result = build_update_result_from_release_payload(
            payload,
            current_version="8.0.0",
            platform_name="Windows",
            machine="x86_64",
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "release_not_ready")
        self.assertFalse(result.has_update)
        self.assertEqual(result.download_url, "")

    def test_unsupported_hosts_never_receive_a_download(self) -> None:
        result = build_update_result_from_release_payload(
            _release_payload(),
            current_version="8.0.0",
            platform_name="Linux",
            machine="x86_64",
        )
        self.assertEqual(result.status, "unsupported_platform")
        self.assertFalse(result.has_update)
        self.assertEqual(result.download_url, "")

    def test_draft_prerelease_and_non_stable_tag_are_not_updates(self) -> None:
        for tag_name in ("v8.1.0-rc.1", "release-2026.07"):
            payload = _release_payload()
            payload["tag_name"] = tag_name
            result = build_update_result_from_release_payload(
                payload,
                current_version="8.0.0",
                platform_name="Darwin",
                machine="arm64",
            )
            self.assertEqual(result.status, "unknown")
        draft = _release_payload()
        draft["draft"] = True
        self.assertEqual(
            build_update_result_from_release_payload(
                draft,
                current_version="8.0.0",
                platform_name="Darwin",
                machine="arm64",
            ).status,
            "unknown",
        )

    def test_mock_release_api_fetches_the_exact_checksum_sidecar(self) -> None:
        release = _release_payload(include_digest=False)
        client = _ReleaseClient(
            release,
            "a" * 64 + "  Translator_macOS_arm64_8.1.0.dmg\n",
        )
        with patch("core.update_checker.httpx.Client", return_value=client):
            result = check_for_updates(
                current_version="8.0.0",
                platform_name="Darwin",
                machine="arm64",
            )
        self.assertTrue(result.has_update)
        self.assertEqual(result.sha256, SHA256)
        self.assertEqual(len(client.request_urls), 2)
        self.assertTrue(client.request_urls[-1].endswith("arm64.dmg.sha256"))

    def test_mock_release_api_fetches_windows_checksum_sidecar(self) -> None:
        release = _release_payload(include_digest=False)
        client = _ReleaseClient(
            release,
            "a" * 64 + "  Translator_Windows_x64_8.1.0_Setup.exe\n",
        )
        with patch("core.update_checker.httpx.Client", return_value=client):
            result = check_for_updates(
                current_version="8.0.0",
                platform_name="Windows",
                machine="x86_64",
            )
        self.assertTrue(result.has_update)
        self.assertEqual(result.sha256, SHA256)
        self.assertEqual(len(client.request_urls), 2)
        self.assertTrue(client.request_urls[-1].endswith("windows-x64-setup.exe.sha256"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
