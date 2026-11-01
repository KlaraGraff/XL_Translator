"""Packaging-output contracts for scripts/build_tauri_package.py."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import scripts.build_tauri_package as build_tauri_package
from app_meta import windows_installer_basename, windows_updater_artifact_name


def _write(path: Path, payload: bytes = b"fixture") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


class TargetPlatformTests(unittest.TestCase):
    def test_current_resolves_to_the_native_platform(self) -> None:
        for system, expected in (("Darwin", "macos"), ("Windows", "windows")):
            with patch.object(
                build_tauri_package.platform, "system", return_value=system
            ):
                self.assertEqual(build_tauri_package.target_platform("current"), expected)

    def test_current_on_a_host_without_a_native_pipeline_is_rejected(self) -> None:
        # Linux 上「current」必须当场失败，而不是猜一个能打的平台出去。
        with patch.object(build_tauri_package.platform, "system", return_value="Linux"):
            with self.assertRaises(ValueError) as caught:
                build_tauri_package.target_platform("current")
        self.assertIn("native macOS and Windows", str(caught.exception))

    def test_cross_platform_builds_are_rejected(self) -> None:
        # 交叉编译从来不在支持范围内：Rosetta / 虚拟机里出来的包不能当原生资产发。
        with patch.object(build_tauri_package.platform, "system", return_value="Linux"):
            with self.assertRaises(RuntimeError):
                build_tauri_package.target_platform("macos")
            with self.assertRaises(RuntimeError):
                build_tauri_package.target_platform("windows")

    def test_platform_names_are_normalized_before_the_host_check(self) -> None:
        with patch.object(build_tauri_package.platform, "system", return_value="Darwin"):
            self.assertEqual(build_tauri_package.target_platform(" MacOS "), "macos")

    def test_unknown_platform_names_are_rejected(self) -> None:
        for raw in ("linux", "universal", ""):
            with self.assertRaises(ValueError, msg=raw):
                build_tauri_package.target_platform(raw)


class TauriCliResolutionTests(unittest.TestCase):
    def test_missing_cli_points_at_npm_ci(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(build_tauri_package, "UI", Path(tmp) / "ui"):
                with self.assertRaises(FileNotFoundError) as caught:
                    build_tauri_package.tauri_cli()
        self.assertIn("npm ci", str(caught.exception))

    def test_posix_runs_the_bare_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ui = Path(tmp) / "ui"
            launcher = _write(ui / "node_modules" / ".bin" / "tauri")
            with (
                patch.object(build_tauri_package, "UI", ui),
                patch.object(build_tauri_package.sys, "platform", "darwin"),
            ):
                self.assertEqual(build_tauri_package.tauri_cli(), [str(launcher)])

    def test_windows_runs_the_cmd_shim_through_cmd(self) -> None:
        # npm 在 Windows 上装出来的是 .cmd 包装脚本；直接 exec 它会以 193 失败，
        # 必须走 `cmd /c`。这条分支在 macOS 上永远不会自己跑到。
        with tempfile.TemporaryDirectory() as tmp:
            ui = Path(tmp) / "ui"
            launcher = _write(ui / "node_modules" / ".bin" / "tauri.cmd", b"@echo off\r\n")
            with (
                patch.object(build_tauri_package, "UI", ui),
                patch.object(build_tauri_package.sys, "platform", "win32"),
            ):
                self.assertEqual(
                    build_tauri_package.tauri_cli(), ["cmd", "/c", str(launcher)]
                )


class WindowsInstallerPackagingTests(unittest.TestCase):
    def test_sha256_sidecar_is_lf_only_and_shasum_compatible(self) -> None:
        # v9.0.0 首次发布失败的根因：Windows 上文本模式把 \n 写成 \r\n，
        # 发布 job 在 macOS 用 `shasum -c` 校验时找不到带 \r 的文件名。
        # 这里按字节断言 sidecar 内容，锁死跨平台一致的输出格式。
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nsis_dir = root / "src-tauri" / "target" / "release" / "bundle" / "nsis"
            nsis_dir.mkdir(parents=True)
            (nsis_dir / "Translator_x64-setup.exe").write_bytes(b"fake installer")
            dist_dir = root / "dist"
            dist_dir.mkdir()

            with (
                patch.object(build_tauri_package, "SRC_TAURI", root / "src-tauri"),
                patch.dict("os.environ", {}, clear=False),
            ):
                os.environ.pop("GITHUB_ENV", None)
                installer = build_tauri_package.package_windows_installer(
                    dist_dir=dist_dir
                )

            sidecar = installer.with_name(f"{installer.name}.sha256").read_bytes()
            self.assertNotIn(b"\r", sidecar)
            digest, _, listed_name = sidecar.decode("utf-8").rstrip("\n").partition("  ")
            self.assertEqual(len(digest), 64)
            self.assertEqual(listed_name, f"{windows_installer_basename()}.exe")
            self.assertTrue(sidecar.endswith(b"\n"))

    def test_no_installer_fails_instead_of_publishing_an_empty_release(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src-tauri" / "target" / "release" / "bundle" / "nsis").mkdir(
                parents=True
            )
            with patch.object(build_tauri_package, "SRC_TAURI", root / "src-tauri"):
                with self.assertRaises(FileNotFoundError):
                    build_tauri_package.package_windows_installer(
                        dist_dir=root / "dist"
                    )

    def test_two_installers_fail_by_name(self) -> None:
        # 拿不准该发哪个的时候宁可停：发错文件名会让已安装的应用看不到新版本。
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nsis_dir = root / "src-tauri" / "target" / "release" / "bundle" / "nsis"
            _write(nsis_dir / "Translator_10.0.0_x64-setup.exe")
            _write(nsis_dir / "Translator_10.0.1_x64-setup.exe")
            with patch.object(build_tauri_package, "SRC_TAURI", root / "src-tauri"):
                with self.assertRaises(RuntimeError) as caught:
                    build_tauri_package.package_windows_installer(
                        dist_dir=root / "dist"
                    )
        self.assertIn("Translator_10.0.1_x64-setup.exe", str(caught.exception))

    def test_an_existing_dist_installer_is_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nsis_dir = root / "src-tauri" / "target" / "release" / "bundle" / "nsis"
            _write(nsis_dir / "Translator_10.0.0_x64-setup.exe", b"new installer")
            dist_dir = root / "dist"
            _write(dist_dir / f"{windows_installer_basename()}.exe", b"stale installer")
            with (
                patch.object(build_tauri_package, "SRC_TAURI", root / "src-tauri"),
                patch.object(build_tauri_package, "_export_github_env"),
            ):
                installer = build_tauri_package.package_windows_installer(
                    dist_dir=dist_dir
                )
            self.assertEqual(installer.read_bytes(), b"new installer")


class WindowsUpdaterSignatureTests(unittest.TestCase):
    def test_signature_is_staged_next_to_the_installer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nsis_dir = root / "src-tauri" / "target" / "release" / "bundle" / "nsis"
            _write(nsis_dir / "Translator_10.0.0_x64-setup.exe.sig", b"signed bytes")
            dist_dir = root / "dist"
            _write(dist_dir / windows_updater_artifact_name(), b"installer")
            env_file = root / "github_env"

            with (
                patch.object(build_tauri_package, "SRC_TAURI", root / "src-tauri"),
                patch.dict(os.environ, {"GITHUB_ENV": str(env_file)}, clear=False),
            ):
                signature = build_tauri_package.package_windows_updater_signature(
                    dist_dir=dist_dir
                )

            self.assertEqual(signature.read_bytes(), b"signed bytes")
            self.assertEqual(signature.name, f"{windows_updater_artifact_name()}.sig")
            self.assertEqual(
                env_file.read_text(encoding="utf-8"),
                f"WINDOWS_UPDATER_SIGNATURE=dist/{windows_updater_artifact_name()}.sig\n",
            )

    def test_unsigned_installer_fails_instead_of_shipping_an_update_no_one_can_install(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nsis_dir = root / "src-tauri" / "target" / "release" / "bundle" / "nsis"
            _write(nsis_dir / "Translator_10.0.0_x64-setup.exe", b"installer")
            with patch.object(build_tauri_package, "SRC_TAURI", root / "src-tauri"):
                with self.assertRaises(FileNotFoundError) as caught:
                    build_tauri_package.package_windows_updater_signature(
                        dist_dir=root / "dist"
                    )
        self.assertIn(".exe.sig", str(caught.exception))

    def test_two_signatures_fail_by_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nsis_dir = root / "src-tauri" / "target" / "release" / "bundle" / "nsis"
            _write(nsis_dir / "Translator_10.0.0_x64-setup.exe.sig", b"a")
            _write(nsis_dir / "Translator_10.0.1_x64-setup.exe.sig", b"b")
            with patch.object(build_tauri_package, "SRC_TAURI", root / "src-tauri"):
                with self.assertRaises(RuntimeError) as caught:
                    build_tauri_package.package_windows_updater_signature(
                        dist_dir=root / "dist"
                    )
        self.assertIn("Translator_10.0.1_x64-setup.exe.sig", str(caught.exception))

    def test_an_empty_signature_fails(self) -> None:
        # 空文件长度是 0，updater 装上以后才验签失败；这里当场停。
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nsis_dir = root / "src-tauri" / "target" / "release" / "bundle" / "nsis"
            _write(nsis_dir / "Translator_10.0.0_x64-setup.exe.sig", b"")
            dist_dir = root / "dist"
            _write(dist_dir / windows_updater_artifact_name(), b"installer")
            with patch.object(build_tauri_package, "SRC_TAURI", root / "src-tauri"):
                with self.assertRaises(RuntimeError) as caught:
                    build_tauri_package.package_windows_updater_signature(
                        dist_dir=dist_dir
                    )
        self.assertIn("empty", str(caught.exception))

    def test_staging_before_renaming_the_installer_fails(self) -> None:
        # 顺序错了就发一个「签名配不上安装包」的更新，签名只在运行时才会被发现不对。
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nsis_dir = root / "src-tauri" / "target" / "release" / "bundle" / "nsis"
            _write(nsis_dir / "Translator_10.0.0_x64-setup.exe.sig", b"signed bytes")
            with patch.object(build_tauri_package, "SRC_TAURI", root / "src-tauri"):
                with self.assertRaises(FileNotFoundError) as caught:
                    build_tauri_package.package_windows_updater_signature(
                        dist_dir=root / "dist"
                    )
        self.assertIn("package_windows_installer()", str(caught.exception))


class UpdaterSigningAvailabilityTests(unittest.TestCase):
    def test_absent_or_blank_key_means_installer_only(self) -> None:
        for environment in ({}, {"TAURI_SIGNING_PRIVATE_KEY": ""}, {"TAURI_SIGNING_PRIVATE_KEY": "   "}):
            with patch.dict(os.environ, environment, clear=False):
                os.environ.pop("TAURI_SIGNING_PRIVATE_KEY", None)
                os.environ.update(environment)
                self.assertFalse(build_tauri_package.updater_signing_available())

    def test_a_present_key_allows_update_signing(self) -> None:
        with patch.dict(
            os.environ, {"TAURI_SIGNING_PRIVATE_KEY": "secret"}, clear=False
        ):
            self.assertTrue(build_tauri_package.updater_signing_available())


class BuildPackageTests(unittest.TestCase):
    def test_unsupported_platform_is_rejected(self) -> None:
        # 命令行走 target_platform() 先挡住；这里是第二道闸，防止有人从别处
        # 直接调 build_package 传了个不受支持的平台名。
        with (
            patch.object(build_tauri_package, "build_sidecar"),
            patch.object(build_tauri_package.subprocess, "run") as run,
        ):
            with self.assertRaises(ValueError) as caught:
                build_tauri_package.build_package(
                    selected_platform="linux", python=Path("/python")
                )
        self.assertIn("unsupported platform", str(caught.exception))
        run.assert_not_called()

    def test_macos_builds_an_app_bundle_without_updater_artifacts(self) -> None:
        # macOS 的更新包由签名、公证之后的 build_macos_package.sh 生成；这里打开
        # createUpdaterArtifacts 只会把未签名的 .app 打包签名出去。
        with (
            patch.object(build_tauri_package, "build_sidecar") as sidecar,
            patch.object(build_tauri_package, "tauri_cli", return_value=["tauri"]),
            patch.object(build_tauri_package.subprocess, "run") as run,
        ):
            build_tauri_package.build_package(
                selected_platform="macos", python=Path("/python")
            )

        sidecar.assert_called_once_with(python=Path("/python"))
        run.assert_called_once_with(
            ["tauri", "build", "--bundles", "app"],
            cwd=build_tauri_package.SRC_TAURI,
            check=True,
        )
        self.assertNotIn(
            build_tauri_package.UPDATER_ARTIFACT_CONFIG, run.call_args.args[0]
        )

    def test_skip_sidecar_leaves_the_existing_binary_in_place(self) -> None:
        with (
            patch.object(build_tauri_package, "build_sidecar") as sidecar,
            patch.object(build_tauri_package, "tauri_cli", return_value=["tauri"]),
            patch.object(build_tauri_package.subprocess, "run"),
        ):
            build_tauri_package.build_package(
                selected_platform="macos", python=Path("/python"), skip_sidecar=True
            )
        sidecar.assert_not_called()

    def test_windows_build_without_a_key_only_produces_the_installer(self) -> None:
        stderr = io.StringIO()
        with (
            patch.object(build_tauri_package, "tauri_cli", return_value=["tauri"]),
            patch.object(build_tauri_package.subprocess, "run") as run,
            patch.object(
                build_tauri_package, "updater_signing_available", return_value=False
            ),
            patch.object(build_tauri_package, "package_windows_installer") as installer,
            patch.object(
                build_tauri_package, "package_windows_updater_signature"
            ) as signature,
            patch.object(build_tauri_package.sys, "stderr", stderr),
        ):
            build_tauri_package.build_package(
                selected_platform="windows", python=Path("/python"), skip_sidecar=True
            )

        run.assert_called_once_with(
            ["tauri", "build", "--bundles", "nsis"],
            cwd=build_tauri_package.SRC_TAURI,
            check=True,
        )
        installer.assert_called_once_with()
        signature.assert_not_called()
        # 没有密钥时必须留下痕迹：这是同一次构建里唯一能看出来的「更新签名缺席」。
        self.assertIn("TAURI_SIGNING_PRIVATE_KEY", stderr.getvalue())

    def test_windows_build_with_a_key_signs_the_installer_for_updates(self) -> None:
        with (
            patch.object(build_tauri_package, "tauri_cli", return_value=["tauri"]),
            patch.object(build_tauri_package.subprocess, "run") as run,
            patch.object(
                build_tauri_package, "updater_signing_available", return_value=True
            ),
            patch.object(build_tauri_package, "package_windows_installer") as installer,
            patch.object(
                build_tauri_package, "package_windows_updater_signature"
            ) as signature,
        ):
            build_tauri_package.build_package(
                selected_platform="windows", python=Path("/python"), skip_sidecar=True
            )

        run.assert_called_once_with(
            [
                "tauri",
                "build",
                "--bundles",
                "nsis",
                "--config",
                build_tauri_package.UPDATER_ARTIFACT_CONFIG,
            ],
            cwd=build_tauri_package.SRC_TAURI,
            check=True,
        )
        installer.assert_called_once_with()
        signature.assert_called_once_with()


if __name__ == "__main__":
    unittest.main(verbosity=2)
