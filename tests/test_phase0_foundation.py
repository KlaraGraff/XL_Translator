from __future__ import annotations

import json
import os
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.app_paths import get_app_data_dir
from scripts.verify_macos_minimum_version import _architectures, _version_tuple
from tests.phase0_foundation import MockTranslationProvider, create_phase0_fixtures


ROOT = Path(__file__).resolve().parents[1]


class Phase0FoundationTests(unittest.TestCase):
    def test_fixture_factory_covers_supported_input_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = create_phase0_fixtures(Path(temporary))
            self.assertTrue(fixture.excel.is_file())
            self.assertTrue(fixture.word.is_file())
            self.assertTrue(fixture.pdf.read_bytes().startswith(b"%PDF-1.4"))
            self.assertEqual(fixture.image.suffix, ".png")

            payload = json.loads(fixture.tm_export.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], 3)
            self.assertTrue(payload["custom_target_languages"][0]["code"].startswith("x-custom-"))
            self.assertEqual(payload["entries"][0]["target_lang"], fixture.custom_target_code)

    def test_mock_provider_returns_actual_language_pair_and_keeps_calls_local(self) -> None:
        provider = MockTranslationProvider()

        result = provider.translate("Hello", source_lang="en", target_lang="zh")

        self.assertEqual(result["source_lang"], "en")
        self.assertEqual(result["target_lang"], "zh")
        self.assertEqual(result["translation"], "[zh] Hello")
        self.assertEqual(provider.calls, [{"text": "Hello", "source_lang": "en", "target_lang": "zh"}])

    def test_isolated_app_data_does_not_touch_legacy_sentinel(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = create_phase0_fixtures(Path(temporary))
            isolated = fixture.root / "runtime-app-data"
            with patch.dict(os.environ, {"TRANSLATOR_APP_DATA_DIR": str(isolated)}):
                self.assertEqual(get_app_data_dir(), isolated)
                isolated.mkdir()
                (isolated / "settings.json").write_text("{}\n", encoding="utf-8")

            self.assertEqual(
                (fixture.legacy_data / "settings.json").read_text(encoding="utf-8"),
                '{"sentinel":"legacy-data-must-stay-untouched"}\n',
            )
            self.assertEqual(list(fixture.legacy_data.iterdir()), [fixture.legacy_data / "settings.json"])

    def test_tauri_declares_macos12_and_vite_targets_safari151(self) -> None:
        with (ROOT / "src-tauri" / "tauri.conf.json").open("rb") as stream:
            config = json.load(stream)
        self.assertEqual(config["bundle"]["macOS"]["minimumSystemVersion"], "12.0")

        vite_config = (ROOT / "ui" / "vite.config.ts").read_text(encoding="utf-8")
        self.assertIn("safari15.1", vite_config)

    def test_version_and_architecture_helpers_are_numeric_and_deterministic(self) -> None:
        self.assertEqual(_version_tuple("12"), (12, 0, 0))
        self.assertLessEqual(_version_tuple("11.7"), _version_tuple("12.0"))
        completed = subprocess.CompletedProcess(
            ["lipo", "-archs", "fixture"],
            0,
            stdout="arm64 x86_64\n",
            stderr="",
        )
        with patch(
            "scripts.verify_macos_minimum_version.subprocess.run",
            return_value=completed,
        ):
            self.assertEqual(_architectures(Path("fixture")), {"arm64", "x86_64"})

    def test_verifier_rejects_bundle_without_macho_or_plist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bundle = Path(temporary) / "Translator.app"
            (bundle / "Contents").mkdir(parents=True)
            with (bundle / "Contents" / "Info.plist").open("wb") as stream:
                plistlib.dump({"LSMinimumSystemVersion": "12.0"}, stream)
            completed = subprocess.run(
                [
                    str(ROOT / ".venv" / "bin" / "python3"),
                    "scripts/verify_macos_minimum_version.py",
                    str(bundle),
                    "--declared",
                    "12.0",
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("no Mach-O binaries", completed.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
