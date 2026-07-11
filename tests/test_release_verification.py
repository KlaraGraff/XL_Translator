from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from importlib.metadata import version
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from scripts.verify_macos_minimum_version import _version_tuple
from scripts.verify_release_dependencies import verify_constraints


ROOT = Path(__file__).resolve().parents[1]


class ReleaseVerificationTests(unittest.TestCase):
    def test_source_smoke_does_not_write_application_data(self):
        with tempfile.TemporaryDirectory() as app_data:
            environment = os.environ.copy()
            environment["TRANSLATOR_APP_DATA_DIR"] = app_data
            environment["QT_QPA_PLATFORM"] = "offscreen"
            result = subprocess.run(
                [sys.executable, "scripts/launch_native.py", "--smoke-test"],
                cwd=ROOT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(Path(app_data).iterdir()), [])

    def test_release_constraints_are_exact_and_unique(self):
        names: set[str] = set()
        constraints = ROOT / "constraints-release-py311.txt"
        for raw_line in constraints.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            requirement = Requirement(line)
            name = canonicalize_name(requirement.name)
            self.assertNotIn(name, names)
            names.add(name)
            specifiers = list(requirement.specifier)
            self.assertEqual(len(specifiers), 1)
            self.assertEqual(specifiers[0].operator, "==")

    def test_release_dependency_verifier_reports_mismatch(self):
        packaging_version = version("packaging")
        with tempfile.TemporaryDirectory() as temp_dir:
            constraints = Path(temp_dir) / "constraints.txt"
            constraints.write_text(
                f"packaging=={packaging_version}\n",
                encoding="utf-8",
            )
            self.assertEqual(verify_constraints(constraints), [])
            constraints.write_text("packaging==0.0.0\n", encoding="utf-8")
            self.assertTrue(verify_constraints(constraints))

    def test_macos_version_comparison_is_numeric(self):
        self.assertLess(_version_tuple("9.10"), _version_tuple("15.0"))
        self.assertEqual(_version_tuple("15"), _version_tuple("15.0.0"))
        self.assertGreater(_version_tuple("15.1"), _version_tuple("15.0"))


if __name__ == "__main__":
    unittest.main()
