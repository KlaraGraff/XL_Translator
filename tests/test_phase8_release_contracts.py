"""Static and fixture-level contracts for the macOS-only release pipeline."""

from __future__ import annotations

import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.verify_macos_minimum_version import verify_app_bundle
from scripts.verify_release_metadata import is_stable_tag, release_metadata_errors


ROOT = Path(__file__).resolve().parents[1]


class Phase8ReleaseContractsTests(unittest.TestCase):
    def test_versions_macos_baseline_and_webview_target_are_aligned(self) -> None:
        self.assertEqual(release_metadata_errors(ROOT), [])

    def test_only_stable_three_component_tags_are_release_eligible(self) -> None:
        self.assertTrue(is_stable_tag("v8.0.0"))
        for invalid in ("8.0.0", "v8.0", "v08.0.0", "v8.0.0-rc.1", "v8.0.0.1"):
            self.assertFalse(is_stable_tag(invalid), invalid)

    def test_release_metadata_rejects_mismatched_or_prerelease_tags(self) -> None:
        self.assertEqual(release_metadata_errors(ROOT, tag="v8.0.0"), [])
        self.assertTrue(release_metadata_errors(ROOT, tag="v8.0.1"))
        self.assertTrue(release_metadata_errors(ROOT, tag="v8.0.0-rc.1"))

    def test_workflow_is_macos_only_and_fails_closed_for_official_tags(self) -> None:
        workflow = (
            ROOT / ".github" / "workflows" / "build-distributions.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("macos-14", workflow)
        self.assertIn("macos-15-intel", workflow)
        self.assertIn("architecture: arm64", workflow)
        self.assertIn("architecture: x86_64", workflow)
        self.assertNotIn("windows-latest", workflow.lower())
        self.assertNotIn("nsis", workflow.lower())
        self.assertIn("Only stable vX.Y.Z tags", workflow)
        self.assertIn("APPLE_DEVELOPER_ID_CERTIFICATE_BASE64", workflow)
        self.assertIn("APPLE_NOTARY_PRIVATE_KEY_BASE64", workflow)
        self.assertIn("xcrun notarytool store-credentials", workflow)
        self.assertIn("needs.validate-release.outputs.formal_release == '1'", workflow)
        self.assertIn("artifact_channel=unsigned-test", workflow)
        self.assertIn("shasum -a 256 -c", workflow)
        self.assertIn("python -m venv .venv", workflow)
        self.assertIn("PYTHON_BIN=./.venv/bin/python3", workflow)
        self.assertIn("./.venv/bin/python3 -m unittest discover -s tests", workflow)

    def test_build_script_scans_before_signing_and_marks_manual_artifacts(self) -> None:
        script = (ROOT / "scripts" / "build_macos_package.sh").read_text(encoding="utf-8")
        self.assertIn("XL_TRANSLATOR_FORMAL_RELEASE", script)
        self.assertIn("_UNSIGNED_TEST.dmg", script)
        self.assertIn("MACOSX_DEPLOYMENT_TARGET=12.0", script)
        self.assertIn("Native macOS release build required", script)
        self.assertIn("--report \"$REPORT_PATH\"", script)
        self.assertIn("spctl --assess --type open", script)
        self.assertLess(
            script.index("scripts/verify_macos_minimum_version.py"),
            script.index("scripts/sign_macos_app.sh"),
        )

    def test_formal_signing_requires_hardened_runtime_and_apple_events_entitlement(self) -> None:
        entitlements = (
            ROOT / "packaging" / "macos" / "translator.entitlements"
        ).read_text(encoding="utf-8")
        signing_script = (ROOT / "scripts" / "sign_macos_app.sh").read_text(
            encoding="utf-8"
        )
        info_plist = (ROOT / "src-tauri" / "Info.plist").read_text(encoding="utf-8")
        self.assertIn("com.apple.security.automation.apple-events", entitlements)
        self.assertIn("--options runtime", signing_script)
        self.assertIn("--entitlements \"$ENTITLEMENTS_PATH\"", signing_script)
        self.assertIn("Developer ID Application", signing_script)
        self.assertIn("Apple Events automation entitlement", signing_script)
        self.assertIn("NSAppleEventsUsageDescription", info_plist)

    def test_readme_and_release_guide_are_macos_only(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        guide = (ROOT / "docs" / "TAURI_DISTRIBUTION_WORKFLOW.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("macOS 12.0 Monterey", readme)
        self.assertIn("Translator_macOS_arm64_<版本>.dmg", readme)
        self.assertIn("Translator_macOS_x64_<版本>.dmg", readme)
        self.assertNotIn("### Windows", readme)
        self.assertNotIn("build_windows_package", guide)
        self.assertIn("Apple 公证", guide)
        self.assertIn("UNSIGNED_TEST", guide)
        self.assertFalse((ROOT / "启动应用.bat").exists())
        self.assertFalse((ROOT / "分发应用.bat").exists())

    def test_macho_scan_report_records_every_binary_and_missing_architecture(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app = Path(temp_dir) / "Fixture.app"
            executable = app / "Contents" / "MacOS" / "fixture"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"\xcf\xfa\xed\xfefixture")
            with (app / "Contents" / "Info.plist").open("wb") as stream:
                plistlib.dump({"LSMinimumSystemVersion": "12.0"}, stream)

            with (
                patch(
                    "scripts.verify_macos_minimum_version._minimum_versions",
                    return_value=["12.0"],
                ),
                patch(
                    "scripts.verify_macos_minimum_version._architectures",
                    return_value={"arm64"},
                ),
            ):
                report = verify_app_bundle(app, declared="12.0", architecture="arm64")
                self.assertTrue(report["ok"])
                self.assertEqual(report["checked_macho_count"], 1)
                self.assertEqual(report["binaries"][0]["architectures"], ["arm64"])

                incompatible = verify_app_bundle(
                    app, declared="12.0", architecture="x86_64"
                )
            self.assertFalse(incompatible["ok"])
            self.assertIn("required x86_64", "\n".join(incompatible["errors"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
