from __future__ import annotations

import io
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from importlib.metadata import version
from pathlib import Path
from unittest.mock import patch

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from scripts.verify_macos_minimum_version import _architectures, _version_tuple
from scripts.verify_release_dependencies import main as verify_release_main
from scripts.verify_release_dependencies import verify_constraints


ROOT = Path(__file__).resolve().parents[1]


class ReleaseDependencyVerifierTests(unittest.TestCase):
    """verify_release_dependencies.py 的失败分支。

    这个脚本是构建机上的最后一道依赖门：它放行了一个版本不对（或干脆没装）
    的包，PyInstaller 打出来的 sidecar 会在用户机器上 import 失败，而构建当天
    全绿。所以每条拒绝理由都单独钉住。
    """

    def _constraints(self, directory: Path, body: str) -> Path:
        path = directory / "constraints.txt"
        path.write_text(body, encoding="utf-8")
        return path

    def test_installed_version_mismatch_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            constraints = self._constraints(Path(tmp), "packaging==0.0.0\n")
            errors = verify_constraints(constraints)
        self.assertEqual(len(errors), 1)
        self.assertIn("packaging", errors[0])
        self.assertIn("does not satisfy", errors[0])

    def test_missing_package_is_reported_instead_of_raised(self) -> None:
        # PackageNotFoundError 漏出去会让脚本以 traceback 结束，日志里看不出缺哪个包。
        with tempfile.TemporaryDirectory() as tmp:
            constraints = self._constraints(
                Path(tmp), "translator-no-such-package==1.0\n"
            )
            errors = verify_constraints(constraints)
        self.assertEqual(
            errors, ["missing required release dependency: translator-no-such-package"]
        )

    def test_unparseable_constraint_is_reported_with_line_number(self) -> None:
        # 锁文件里手滑写坏一行，必须指到第几行；「invalid constraint」而不是崩掉。
        with tempfile.TemporaryDirectory() as tmp:
            constraints = self._constraints(
                Path(tmp), "# 注释\n\npackaging==\n"
            )
            errors = verify_constraints(constraints)
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith(f"{constraints}:3: invalid constraint:"), errors[0])

    def test_comments_and_blank_lines_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            packaging_version = version("packaging")
            constraints = self._constraints(
                Path(tmp),
                f"# 锁文件\n\n  packaging=={packaging_version}  \n\n",
            )
            self.assertEqual(verify_constraints(constraints), [])

    def test_marker_that_does_not_apply_is_ignored(self) -> None:
        # 只锁 Windows 的包在 macOS 构建机上本来就不该装；按标记跳过，不报缺。
        with tempfile.TemporaryDirectory() as tmp:
            constraints = self._constraints(
                Path(tmp), "translator-no-such-package==1.0; sys_platform == \"win32\"\n"
            )
            self.assertEqual(verify_constraints(constraints), [])

    def test_every_problem_is_reported_not_just_the_first(self) -> None:
        # 逐个修再重跑的循环很贵（每次都要重装整棵依赖树），一次报全。
        with tempfile.TemporaryDirectory() as tmp:
            constraints = self._constraints(
                Path(tmp), "packaging==0.0.0\ntranslator-no-such-package==1.0\n"
            )
            errors = verify_constraints(constraints)
        self.assertEqual(len(errors), 2)

    def test_main_refuses_a_python_other_than_311(self) -> None:
        # 构建机是 3.11：别的版本上跑出来的 sidecar 不能当发布产物。
        with tempfile.TemporaryDirectory() as tmp:
            constraints = self._constraints(Path(tmp), "")
            with (
                patch.object(sys, "version_info", (3, 12, 0)),
                patch.object(sys, "stderr", new_callable=io.StringIO) as stderr,
                patch.object(
                    sys, "argv", ["verify_release_dependencies.py", "--constraints", str(constraints)]
                ),
            ):
                self.assertEqual(verify_release_main(), 1)
        self.assertIn("Python 3.11", stderr.getvalue())

    def test_main_reports_a_missing_constraints_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope.txt"
            with (
                patch.object(sys, "version_info", (3, 11, 0)),
                patch.object(sys, "stderr", new_callable=io.StringIO) as stderr,
                patch.object(
                    sys,
                    "argv",
                    ["verify_release_dependencies.py", "--constraints", str(missing)],
                ),
            ):
                self.assertEqual(verify_release_main(), 1)
        self.assertIn("Constraints file not found", stderr.getvalue())

    def test_main_exits_nonzero_when_a_dependency_is_wrong(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            constraints = self._constraints(Path(tmp), "packaging==0.0.0\n")
            with (
                patch.object(sys, "version_info", (3, 11, 0)),
                patch.object(sys, "stderr", new_callable=io.StringIO) as stderr,
                patch.object(
                    sys,
                    "argv",
                    ["verify_release_dependencies.py", "--constraints", str(constraints)],
                ),
            ):
                self.assertEqual(verify_release_main(), 1)
        self.assertIn("[ERROR]", stderr.getvalue())

    def test_main_succeeds_on_a_satisfied_constraint_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            constraints = self._constraints(
                Path(tmp), f"packaging=={version('packaging')}\n"
            )
            with (
                patch.object(sys, "version_info", (3, 11, 0)),
                patch.object(
                    sys,
                    "argv",
                    ["verify_release_dependencies.py", "--constraints", str(constraints)],
                ),
                patch("builtins.print") as printed,
            ):
                self.assertEqual(verify_release_main(), 0)
        printed.assert_called_once()
        self.assertIn("Release dependencies match", printed.call_args.args[0])


class ReleaseScriptReferenceTests(unittest.TestCase):
    """发布文档/工作流里点名的 scripts/ 文件必须真的存在。

    scripts/ 没有 README 可供对照，文档漂移只能靠测试兜：改名或删掉一个脚本，
    照着 AGENTS.md / 发版步骤做的人会在发版当天撞上「文件不存在」。
    只查操作类的文档——历史验收记录里的旧路径是留档，不该被改写。
    """

    _OPERATIONAL_SOURCES = (
        "AGENTS.md",
        "README.md",
        "docs/TAURI_DISTRIBUTION_WORKFLOW.md",
    )

    def _sources(self) -> list[Path]:
        sources = [ROOT / name for name in self._OPERATIONAL_SOURCES]
        sources += sorted((ROOT / ".github" / "workflows").glob("*.yml"))
        return [path for path in sources if path.is_file()]

    def test_every_script_named_by_a_release_document_exists(self) -> None:
        pattern = re.compile(r"scripts/([\w.-]+\.(?:py|sh|mjs))")
        missing: dict[str, list[str]] = {}
        for path in self._sources():
            broken = sorted(
                {
                    name
                    for name in pattern.findall(path.read_text(encoding="utf-8"))
                    if not (ROOT / "scripts" / name).is_file()
                }
            )
            if broken:
                missing[str(path.relative_to(ROOT))] = broken
        self.assertEqual(missing, {}, f"发布文档引用了不存在的脚本：{missing}")

    def test_the_three_release_scripts_are_reachable_from_a_document(self) -> None:
        # 这三个脚本只在发布 job 里跑，平时没人打开；至少保证改名时有人被文档拽住。
        corpus = "\n".join(
            path.read_text(encoding="utf-8") for path in self._sources()
        )
        for name in (
            "build_tauri_package.py",
            "build_updater_manifest.py",
            "verify_release_dependencies.py",
        ):
            self.assertIn(name, corpus, f"没有任何发布文档提到 {name}")


class ReleaseVerificationTests(unittest.TestCase):
    def test_bundled_license_attribution_is_consistent(self):
        license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        attribution = next(
            line for line in license_text.splitlines() if line.startswith("Copyright (c) ")
        )
        year_and_author = attribution.removeprefix("Copyright (c) ")
        year, author = year_and_author.split(" ", 1)
        cargo = tomllib.loads((ROOT / "src-tauri/Cargo.toml").read_text(encoding="utf-8"))
        settings_ui = (ROOT / "ui/src/views/settings.ts").read_text(encoding="utf-8")
        self.assertEqual(cargo["package"]["authors"], [author])
        # 关于页只写许可名，不带署名。
        self.assertIn('licenseText.textContent = "MIT License";', settings_ui)
        self.assertNotIn(f"© {year} {author}", settings_ui)

    def test_source_smoke_does_not_write_application_data(self):
        with tempfile.TemporaryDirectory() as app_data:
            environment = os.environ.copy()
            environment["TRANSLATOR_APP_DATA_DIR"] = app_data
            result = subprocess.run(
                [sys.executable, "scripts/launch_sidecar.py", "--smoke-test"],
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

    def test_macos_architecture_parser_accepts_fat_binary_slices(self):
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
            self.assertEqual(
                _architectures(Path("fixture")),
                {"arm64", "x86_64"},
            )

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS codesign")
    def test_macos_signing_helper_seals_unsigned_app_bundle(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = Path(temp_dir) / "Fixture.app"
            executable = app / "Contents" / "MacOS" / "fixture"
            resources = app / "Contents" / "Resources"
            sidecar = resources / "sidecar" / "translator-sidecar"
            nested_library = sidecar.parent / "_internal" / "Python"
            executable.parent.mkdir(parents=True)
            nested_library.parent.mkdir(parents=True)
            shutil.copyfile("/usr/bin/true", executable)
            shutil.copyfile("/usr/bin/true", sidecar)
            shutil.copyfile("/usr/bin/true", nested_library)
            executable.chmod(0o755)
            sidecar.chmod(0o755)
            nested_library.chmod(0o755)
            (resources / "fixture.txt").write_text("sealed\n", encoding="utf-8")
            with (app / "Contents" / "Info.plist").open("wb") as stream:
                plistlib.dump(
                    {
                        "CFBundleExecutable": "fixture",
                        "CFBundleIdentifier": "com.klara-graff.translator.fixture",
                        "CFBundleName": "Fixture",
                        "CFBundlePackageType": "APPL",
                    },
                    stream,
                )

            unsigned = subprocess.run(
                ["codesign", "--verify", "--deep", "--strict", str(app)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(unsigned.returncode, 0)

            environment = os.environ.copy()
            environment.pop("XL_TRANSLATOR_MACOS_CODESIGN_IDENTITY", None)
            subprocess.run(
                ["bash", "scripts/sign_macos_app.sh", str(app), str(sidecar)],
                cwd=ROOT,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            verified = subprocess.run(
                ["codesign", "--verify", "--deep", "--strict", str(app)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(verified.returncode, 0, verified.stderr)
            details = subprocess.run(
                ["codesign", "-dv", "--verbose=4", str(app)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(details.returncode, 0, details.stderr)
            self.assertIn("Signature=adhoc", details.stderr)
            self.assertIn("Sealed Resources version=2", details.stderr)
            nested_verified = subprocess.run(
                ["codesign", "--verify", "--strict", str(nested_library)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(nested_verified.returncode, 0, nested_verified.stderr)
            sidecar_entitlements = subprocess.run(
                ["codesign", "-d", "--entitlements", ":-", str(sidecar)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(sidecar_entitlements.returncode, 0)
            self.assertIn(
                "com.apple.security.cs.disable-library-validation",
                sidecar_entitlements.stdout,
            )


if __name__ == "__main__":
    unittest.main()
