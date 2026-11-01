"""Static and fixture-level contracts for the macOS + Windows release pipeline."""

from __future__ import annotations

import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app_meta import APP_VERSION
from scripts.verify_macos_minimum_version import verify_app_bundle
from scripts.verify_release_metadata import is_stable_tag, release_metadata_errors


ROOT = Path(__file__).resolve().parents[1]

_PLAN = ROOT / "docs" / "upgrade-functional-migration-implementation-plan.md"
_DECISIONS = ROOT / "docs" / "upgrade-functional-migration-decisions.md"
_ROUTE_B = ROOT / "docs" / "validation" / "2026-07-16_route_b_tauri_final_acceptance.md"


class Phase8ReleaseContractsTests(unittest.TestCase):
    def test_versions_macos_baseline_and_webview_target_are_aligned(self) -> None:
        self.assertEqual(release_metadata_errors(ROOT), [])

    def test_only_stable_three_component_tags_are_release_eligible(self) -> None:
        self.assertTrue(is_stable_tag("v8.0.0"))
        for invalid in ("8.0.0", "v8.0", "v08.0.0", "v8.0.0-rc.1", "v8.0.0.1"):
            self.assertFalse(is_stable_tag(invalid), invalid)

    def test_release_metadata_rejects_mismatched_or_prerelease_tags(self) -> None:
        # Derive the tags from APP_VERSION rather than hardcoding one: the assertion
        # is about tag-vs-app-version agreement, not about any particular release, and
        # a literal here turns every version bump into an unrelated test failure.
        current = f"v{APP_VERSION}"
        stale = "v8.0.0"
        self.assertNotEqual(stale, current, "pick a different stale tag for this test")
        self.assertEqual(release_metadata_errors(ROOT, tag=current), [])
        self.assertTrue(release_metadata_errors(ROOT, tag=stale))
        self.assertTrue(release_metadata_errors(ROOT, tag=f"{current}-rc.1"))

    def test_workflow_builds_both_platforms_and_fails_closed_for_official_tags(
        self,
    ) -> None:
        workflow = (
            ROOT / ".github" / "workflows" / "build-distributions.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("macos-14", workflow)
        self.assertIn("architecture: arm64", workflow)
        # macOS distribution is Apple Silicon only; the Intel matrix leg must
        # not come back silently.
        self.assertNotIn("macos-15-intel", workflow)
        self.assertNotIn("architecture: x86_64", workflow)
        # Windows distribution is a native x64 NSIS installer, unsigned, and
        # the release job refuses to publish without both platform assets.
        self.assertIn("windows-latest", workflow)
        self.assertIn("Windows x64 NSIS installer", workflow)
        self.assertIn("--platform windows", workflow)
        self.assertIn('test "$PROCESSOR_ARCHITECTURE" = "AMD64"', workflow)
        self.assertIn("Translator_Windows_x64_${version}_Setup.exe", workflow)
        self.assertIn("Translator_Windows_x64_${version}_Setup.exe.sha256", workflow)
        self.assertIn("Only stable vX.Y.Z tags", workflow)
        # Secret 名与 lantern 仓库对齐：证书 .p12 + Apple ID/App 专用密码公证。
        self.assertIn("APPLE_CERTIFICATE", workflow)
        self.assertIn("APPLE_TEAM_ID", workflow)
        self.assertNotIn("APPLE_DEVELOPER_ID_CERTIFICATE_BASE64", workflow)
        self.assertNotIn("APPLE_NOTARY_KEY_ID", workflow)
        self.assertIn("xcrun notarytool store-credentials", workflow)
        self.assertIn('--apple-id "$APPLE_ID"', workflow)
        self.assertIn("needs.validate-release.outputs.formal_release == '1'", workflow)
        self.assertIn("needs.validate-release.outputs.temporary_signing == '1'", workflow)
        # 每个稳定 tag 都按正式发布出去，不再因为缺少签名密钥而降级成 Pre-release。
        self.assertIn("prerelease: false", workflow)
        self.assertNotIn("prerelease: ${{", workflow)
        # 工作流只产出草稿：正文那句「整理中」的占位不能出现在公开的 Release 上。
        # 写完手工说明的人执行 `gh release edit <tag> --draft=false` 才算发布。
        self.assertIn("draft: true", workflow)
        self.assertNotIn("draft: ${{", workflow)
        self.assertIn("发布说明整理中", workflow)
        self.assertIn("TEMP_SIGNED_TEST.dmg", workflow)
        # The release job checksums both platform assets before publishing.
        self.assertIn('shasum -a 256 -c "${expected[1]}"', workflow)
        self.assertIn('shasum -a 256 -c "${expected[3]}"', workflow)
        self.assertIn("channel=unsigned-test", workflow)
        self.assertIn("channel=temporary-test", workflow)
        self.assertIn('echo "artifact_channel=$channel"', workflow)
        self.assertIn("temporary_signing=1", workflow)
        # 应用内更新只跟正式发布走：临时通道的 macOS 产物没有公证，一旦也发更新包，
        # 那个 Release 被取消草稿就会把所有已安装的机器换成未公证版本。
        self.assertIn("Not a formal release; skipping in-app update artifacts", workflow)
        # 正式发布缺 minisign 私钥必须整体失败，不能降级成「只发安装包」：那个
        # Release 一旦取消草稿就成了 releases/latest，已安装的机器仍然会报出新版本，
        # 而应用内更新永远取不到 latest.json，用户只能对着「重试」反复失败。
        self.assertIn(
            "TAURI_SIGNING_PRIVATE_KEY is missing on a formal release", workflow
        )
        self.assertIn(
            "TAURI_SIGNING_PRIVATE_KEY: ${{ needs.validate-release.outputs.updater_enabled"
            " == '1' && secrets.TAURI_SIGNING_PRIVATE_KEY || '' }}",
            workflow,
        )
        # Windows 的更新载荷就是安装器本身，Tauri v2 不再另出 .nsis.zip。
        # .sig 只用来生成 latest.json，不随 Release 上传；.sha256 必须上传，
        # 已安装的版本靠它判断新版本是否就绪（core/update_checker.py）。
        self.assertNotIn("release-assets/*.exe.sig", workflow)
        self.assertNotIn("release-assets/*.app.tar.gz.sig", workflow)
        self.assertIn("release-assets/*.dmg.sha256", workflow)
        self.assertIn("release-assets/*.exe.sha256", workflow)
        self.assertIn("release-assets/latest.json", workflow)
        self.assertNotIn(".nsis.zip", workflow)
        self.assertIn("python -m venv .venv", workflow)
        self.assertIn("PYTHON_BIN=./.venv/bin/python3", workflow)
        self.assertIn("PYTHON_BIN=./.venv/Scripts/python.exe", workflow)
        # tests/ 里有 pytest 风格的用例，unittest discover 会在导入时报错。
        self.assertIn("./.venv/bin/python3 -m pip install --requirement requirements-test.txt", workflow)
        self.assertIn("./.venv/bin/python3 scripts/release_env_tests.py", workflow)
        self.assertNotIn("unittest discover", workflow)

    def test_build_script_scans_before_signing_and_marks_manual_artifacts(self) -> None:
        script = (ROOT / "scripts" / "build_macos_package.sh").read_text(encoding="utf-8")
        self.assertIn("XL_TRANSLATOR_FORMAL_RELEASE", script)
        self.assertIn("XL_TRANSLATOR_TEMPORARY_SIGNING", script)
        self.assertIn("_TEMP_SIGNED_TEST.dmg", script)
        self.assertIn("_UNSIGNED_TEST.dmg", script)
        self.assertIn("MACOSX_DEPLOYMENT_TARGET=12.0", script)
        self.assertIn("Native macOS release build required", script)
        self.assertIn("--report \"$REPORT_PATH\"", script)
        self.assertIn("spctl --assess --type open", script)
        self.assertLess(
            script.index("scripts/verify_macos_minimum_version.py"),
            script.index("scripts/sign_macos_app.sh"),
        )

    def test_installer_size_optimizations_stay_in_place(self) -> None:
        script = (ROOT / "scripts" / "build_macos_package.sh").read_text(encoding="utf-8")
        # Tauri follows symlinks when it copies resources; the sidecar must be
        # put back with its symlinks before anything is signed or verified.
        restore = script.index('ditto "$SIDECAR_SOURCE_DIR" "$SIDECAR_BUNDLED_DIR"')
        self.assertLess(restore, script.index("scripts/verify_macos_minimum_version.py"))
        self.assertLess(restore, script.index("scripts/sign_macos_app.sh"))
        self.assertIn("-format ULMO", script)
        self.assertNotIn("-format UDZO", script)

        cargo = (ROOT / "src-tauri" / "Cargo.toml").read_text(encoding="utf-8")
        profile = cargo.split("[profile.release]", 1)[1]
        for line in ("lto = true", "codegen-units = 1", 'opt-level = "s"', "strip = true"):
            self.assertIn(line, profile)
        # SidecarShutdownHook stops the sidecar from Drop; abort would skip it.
        self.assertNotIn('panic = "abort"', profile)

        spec = (ROOT / "packaging" / "sidecar" / "translator_sidecar.spec").read_text(encoding="utf-8")
        for module in ("pygments", "_pytest", "win32ui", "pywin"):
            self.assertIn(f'"{module}"', spec)
        self.assertNotIn('ROOT / "assets"', spec)

    def test_formal_signing_requires_hardened_runtime_and_apple_events_entitlement(self) -> None:
        entitlements = (
            ROOT / "packaging" / "macos" / "translator.entitlements"
        ).read_text(encoding="utf-8")
        signing_script = (ROOT / "scripts" / "sign_macos_app.sh").read_text(
            encoding="utf-8"
        )
        adhoc_entitlements = (
            ROOT / "packaging" / "macos" / "translator-adhoc.entitlements"
        ).read_text(encoding="utf-8")
        info_plist = (ROOT / "src-tauri" / "Info.plist").read_text(encoding="utf-8")
        self.assertIn("com.apple.security.automation.apple-events", entitlements)
        self.assertIn("--options runtime", signing_script)
        self.assertIn("--entitlements \"$ENTITLEMENTS_PATH\"", signing_script)
        self.assertIn("Developer ID Application", signing_script)
        self.assertIn("Apple Events automation entitlement", signing_script)
        self.assertIn("find \"$SIDECAR_DIR\" -type f -print0", signing_script)
        self.assertIn("com.apple.security.cs.disable-library-validation", adhoc_entitlements)
        # The PDF component (PyMuPDF) is loaded from the app data directory at run time.
        self.assertIn("com.apple.security.cs.disable-library-validation", entitlements)
        self.assertIn("NSAppleEventsUsageDescription", info_plist)

    def test_readme_and_release_guide_cover_both_platforms(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        guide = (ROOT / "docs" / "TAURI_DISTRIBUTION_WORKFLOW.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("macOS 12.0 Monterey", readme)
        self.assertIn("Translator_macOS_arm64_<版本>.dmg", readme)
        self.assertIn("Translator_Windows_x64_<版本>_Setup.exe", readme)
        self.assertIn("Windows 10", readme)
        self.assertIn("### Windows 安装与首次打开", readme)
        self.assertIn("Get-FileHash", readme)
        self.assertIn("SmartScreen", readme)
        self.assertIn("WebView2", readme)
        self.assertNotIn("Translator_macOS_x64_<版本>.dmg", readme)
        self.assertIn("Translator_Windows_x64_<version>_Setup.exe", guide)
        self.assertIn("--platform windows", guide)
        self.assertIn("PROCESSOR_ARCHITECTURE", guide)
        self.assertIn("Apple 公证", guide)
        self.assertIn("UNSIGNED_TEST", guide)
        self.assertIn("TEMP_SIGNED_TEST", guide)
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


# ── 真实 Key 暂缓项登记（最终发布门 14.2）───────────────────────────────
#
# `docs/upgrade-functional-migration-implementation-plan.md:602` 把「真实 Key 暂缓项
# 单独列出，不伪装成通过」写进最终发布门。这条门要的不是真实 Key 跑通（外部条件一直
# 没提供，按既定例外继续暂缓），而是暂缓这件事本身白纸黑字摆着：哪几项、为什么没跑、
# 要什么条件才能跑，并且不能给它们盖上 PASS。下面三个用例钉住这份账，防的是最容易被
# 顺手改坏的两件事——把暂缓表删掉/合并成「已完成」，或者给某一行补一个 PASS。


def _section(text: str, heading: str) -> str:
    """Return the lines under one ``##`` heading, up to the next same-level heading."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == heading:
            collected: list[str] = []
            for following in lines[index + 1:]:
                if following.startswith("## "):
                    break
                collected.append(following)
            return "\n".join(collected)
    raise AssertionError(f"找不到小节：{heading}")


def _table_rows(section: str) -> list[list[str]]:
    """Split a markdown table into rows of cells, dropping the separator row."""
    rows: list[list[str]] = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if all(set(cell) <= set("-: ") and cell for cell in cells):
            continue
        rows.append(cells)
    return rows


class DeferredRealKeyRegisterTests(unittest.TestCase):
    def test_the_release_gate_still_requires_deferred_real_key_items_be_listed(self) -> None:
        gate = _section(
            _PLAN.read_text(encoding="utf-8"),
            "### 14.2 最终发布门",
        )
        self.assertIn(
            "所有模块的标准本地全链路测试通过；真实 Key 暂缓项单独列出，不伪装成通过。",
            gate,
        )

    def test_the_route_b_acceptance_keeps_its_external_deferred_table(self) -> None:
        deferred = _section(
            _ROUTE_B.read_text(encoding="utf-8"),
            "## 外部验收项（已获准暂缓）",
        )
        self.assertIn("| 项目 | 未执行原因 | 完成所需条件 |", deferred)

        rows = _table_rows(deferred)
        self.assertEqual(rows[0], ["项目", "未执行原因", "完成所需条件"])
        projects = [row[0] for row in rows[1:]]
        # 四项真实服务与真实翻译对照都必须还在表里，而且各自写着没执行的原因和
        # 完成条件——只有一句「待补」不算把账列清楚。
        self.assertIn("四家真实服务的连通性", projects)
        self.assertIn("Excel、Word、PDF 各一单真实翻译与 V7.4 输出对照", projects)
        for row in rows[1:]:
            self.assertEqual(len(row), 3, row)
            self.assertTrue(row[1], f"暂缓项没有写未执行原因：{row[0]}")
            self.assertTrue(row[2], f"暂缓项没有写完成所需条件：{row[0]}")

    def test_no_deferred_external_item_is_ever_marked_as_passed(self) -> None:
        deferred = _section(
            _ROUTE_B.read_text(encoding="utf-8"),
            "## 外部验收项（已获准暂缓）",
        )
        self.assertNotIn("PASS", deferred.upper())
        # 结论也要明说这些项没有被当成通过，别只靠表格本身。
        conclusion = _section(_ROUTE_B.read_text(encoding="utf-8"), "## 结论")
        self.assertIn("没有被本记录或自动化测试视为已通过", conclusion)

    def test_the_frozen_decision_still_defers_real_key_acceptance(self) -> None:
        decisions = _DECISIONS.read_text(encoding="utf-8")
        self.assertIn(
            "任一架构未完成 macOS 12 验收或存在阻断问题时不得正式发布；真实 Key 验收继续按既定例外暂缓。",
            decisions,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
