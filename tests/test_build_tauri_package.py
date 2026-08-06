"""Packaging-output contracts for scripts/build_tauri_package.py."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import scripts.build_tauri_package as build_tauri_package
from app_meta import windows_installer_basename


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
                import os

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
