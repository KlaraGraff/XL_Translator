from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from core import ppt_converter
from core.ppt_converter import (
    FIDELITY_COMPATIBILITY,
    FIDELITY_HIGH,
    PptConversionCache,
    PptConversionError,
    PptConversionResult,
    convert_ppt_to_pptx,
    converted_name,
    is_legacy_ppt,
    validate_pptx,
)
from core.word_converter import _find_soffice


_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'
)
_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
    'relationships/officeDocument" Target="ppt/presentation.xml"/>'
    "</Relationships>"
)
_PRESENTATION = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>'
)


OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _write_pptx(path: Path, *, presentation: str = _PRESENTATION, rels: str = _ROOT_RELS) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("ppt/presentation.xml", presentation)
    return path


def _missing(message: str):
    def converter(_path: Path) -> Path:
        raise ppt_converter._ToolMissing(message)

    return converter


def _failing(message: str):
    def converter(_path: Path) -> Path:
        raise PptConversionError(message)

    return converter


class _Workspace(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.source = self.root / "汇报.ppt"
        self.source.write_bytes(OLE + b"legacy powerpoint payload")

    def tearDown(self) -> None:
        self._temp.cleanup()

    def converters(self, native, libreoffice):
        return patch.multiple(
            ppt_converter,
            convert_with_native_powerpoint=native,
            convert_with_libreoffice=libreoffice,
        )


class NamingTests(unittest.TestCase):
    def test_legacy_suffixes(self) -> None:
        self.assertTrue(is_legacy_ppt("a.ppt"))
        self.assertTrue(is_legacy_ppt("A.PPS"))
        self.assertFalse(is_legacy_ppt("a.pptx"))
        self.assertFalse(is_legacy_ppt("a.doc"))

    def test_converted_name_only_changes_legacy_suffix(self) -> None:
        self.assertEqual(converted_name("季度 汇报.ppt"), "季度 汇报.pptx")
        self.assertEqual(converted_name("放映.PPS"), "放映.pptx")
        self.assertEqual(converted_name("已是新版.pptx"), "已是新版.pptx")


class ConversionOrderTests(_Workspace):
    def test_powerpoint_first_and_libreoffice_untouched(self) -> None:
        output = _write_pptx(self.root / "out.pptx")
        libreoffice_calls: list[Path] = []

        def libreoffice(path: Path) -> Path:
            libreoffice_calls.append(path)
            return output

        with self.converters(lambda _path: output, libreoffice):
            result = convert_ppt_to_pptx(self.source)

        self.assertEqual(result.path, output)
        self.assertEqual(result.method, "PowerPoint")
        self.assertEqual(result.fidelity, FIDELITY_HIGH)
        self.assertEqual(libreoffice_calls, [])
        self.assertEqual(result.note, "旧版 .ppt 已用 PowerPoint 转换为 .pptx，输出为 .pptx")

    def test_libreoffice_takes_over_without_confirmation(self) -> None:
        output = _write_pptx(self.root / "out.pptx")
        with self.converters(_failing("PowerPoint 未能完成本次转换。"), lambda _path: output):
            result = convert_ppt_to_pptx(self.source)

        self.assertEqual(result.method, "LibreOffice")
        self.assertEqual(result.fidelity, FIDELITY_COMPATIBILITY)
        self.assertEqual(result.fallback_messages, ["PowerPoint：PowerPoint 未能完成本次转换。"])
        self.assertIn("版式可能与原文件略有差异", result.note)

    def test_invalid_output_is_deleted_and_next_route_used(self) -> None:
        broken = self.root / "broken.pptx"
        broken.write_bytes(b"not a zip")
        output = _write_pptx(self.root / "out.pptx")
        with self.converters(lambda _path: broken, lambda _path: output):
            result = convert_ppt_to_pptx(self.source)

        self.assertEqual(result.method, "LibreOffice")
        self.assertFalse(broken.exists())
        self.assertIn("不是有效的 .pptx", result.fallback_messages[0])

    def test_nothing_installed_asks_to_save_as(self) -> None:
        with self.converters(_missing("未找到 Microsoft PowerPoint，可能尚未安装。"), _missing("本机未找到 LibreOffice。")):
            with self.assertRaises(PptConversionError) as caught:
                convert_ppt_to_pptx(self.source)

        message = str(caught.exception)
        self.assertTrue(message.startswith("本机未找到 PowerPoint 或 LibreOffice"))
        self.assertIn("请先在 PowerPoint 里将它另存为 .pptx", message)
        self.assertEqual(len(caught.exception.attempts), 2)

    def test_both_fail_lists_reasons_then_asks_to_save_as(self) -> None:
        with self.converters(_failing("PowerPoint 无响应。"), _failing("LibreOffice 未能转换此文件。")):
            with self.assertRaises(PptConversionError) as caught:
                convert_ppt_to_pptx(self.source)

        message = str(caught.exception)
        self.assertIn("PowerPoint：PowerPoint 无响应。", message)
        self.assertIn("LibreOffice：LibreOffice 未能转换此文件。", message)
        self.assertTrue(message.endswith("再翻译另存后的文件。"))

    def test_raw_automation_error_is_humanized(self) -> None:
        def denied(_path: Path) -> Path:
            raise RuntimeError("execution error: Not authorized to send Apple events to Microsoft PowerPoint. (-1743)")

        with self.converters(denied, _missing("本机未找到 LibreOffice。")):
            with self.assertRaises(PptConversionError) as caught:
                convert_ppt_to_pptx(self.source)

        self.assertIn("系统未授权本程序控制 Office", str(caught.exception))
        self.assertNotIn("-1743", str(caught.exception))

    def test_rejects_non_legacy_and_missing_files(self) -> None:
        with self.assertRaises(PptConversionError):
            convert_ppt_to_pptx(self.root / "new.pptx")
        with self.assertRaises(PptConversionError) as caught:
            convert_ppt_to_pptx(self.root / "gone.ppt")
        self.assertIn("文件不存在", str(caught.exception))

    def test_office_lock_file_never_reaches_powerpoint(self) -> None:
        # PowerPoint 开着「汇报.ppt」时旁边的「~$汇报.ppt」：165 字节，不是演示文稿。
        # 交给 PowerPoint 它会对着它等两分钟（AppleEvent timed out -1712）。
        lock = self.root / "~$汇报.ppt"
        lock.write_bytes(b"\x0bmaple" + b"\x00" * 159)
        calls: list[Path] = []

        def native(path: Path) -> Path:
            calls.append(path)
            raise AssertionError("PowerPoint must not be asked")

        with self.converters(native, native):
            with self.assertRaises(PptConversionError) as caught:
                convert_ppt_to_pptx(lock)
        self.assertEqual(calls, [])
        self.assertIn("不是有效的 PowerPoint 文件", str(caught.exception))

    def test_renamed_pptx_still_goes_to_powerpoint(self) -> None:
        renamed = _write_pptx(self.root / "其实是新版.ppt")

        def native(path: Path) -> Path:
            return _write_pptx(self.root / "converted.pptx")

        with self.converters(native, _missing("本机未找到 LibreOffice。")):
            result = convert_ppt_to_pptx(renamed)
        self.assertEqual(result.method, "PowerPoint")

    def test_native_powerpoint_unsupported_platform_is_missing_not_failure(self) -> None:
        with patch.object(ppt_converter.platform, "system", return_value="Linux"):
            with self.assertRaises(ppt_converter._ToolMissing):
                ppt_converter.convert_with_native_powerpoint(self.source)


class ValidateTests(_Workspace):
    def test_accepts_minimal_presentation(self) -> None:
        validate_pptx(_write_pptx(self.root / "ok.pptx"))

    def test_rejects_wordprocessing_package(self) -> None:
        document = '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>'
        path = _write_pptx(self.root / "doc.pptx", presentation=document)
        with self.assertRaises(PptConversionError):
            validate_pptx(path)

    def test_rejects_missing_main_part(self) -> None:
        rels = _ROOT_RELS.replace("ppt/presentation.xml", "ppt/elsewhere.xml")
        with self.assertRaises(PptConversionError):
            validate_pptx(_write_pptx(self.root / "rels.pptx", rels=rels))

    def test_rejects_empty_file(self) -> None:
        empty = self.root / "empty.pptx"
        empty.write_bytes(b"")
        with self.assertRaises(PptConversionError):
            validate_pptx(empty)


class CacheTests(_Workspace):
    def _counting(self, *, fail: bool = False):
        calls: list[Path] = []

        def converter(path: Path) -> PptConversionResult:
            calls.append(path)
            if fail:
                raise PptConversionError("转不了")
            output = _write_pptx(self.root / f"converted_{len(calls)}.pptx")
            return PptConversionResult(source=path, path=output, method="PowerPoint")

        return calls, converter

    def test_same_file_converted_once_per_task(self) -> None:
        calls, converter = self._counting()
        cache = PptConversionCache(converter)
        first = cache.convert(self.source)
        second = cache.convert(str(self.source))
        self.assertIs(first, second)
        self.assertEqual(len(calls), 1)

    def test_changed_file_is_converted_again(self) -> None:
        calls, converter = self._counting()
        cache = PptConversionCache(converter)
        cache.convert(self.source)
        self.source.write_bytes(OLE + b"legacy powerpoint payload, edited")
        cache.convert(self.source)
        self.assertEqual(len(calls), 2)

    def test_touched_but_unchanged_file_is_not_converted_again(self) -> None:
        # 同步盘、杀毒软件只改了修改时间：内容没变，读取时转的那份接着用。
        calls, converter = self._counting()
        cache = PptConversionCache(converter)
        first = cache.convert(self.source)
        stat = self.source.stat()
        os.utime(self.source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
        self.assertIs(cache.convert(self.source), first)
        self.assertEqual(len(calls), 1)
        self.assertTrue(first.path.exists())

    def test_same_size_edit_is_converted_again_and_old_copy_deleted(self) -> None:
        calls, converter = self._counting()
        cache = PptConversionCache(converter)
        first = cache.convert(self.source)
        stat = self.source.stat()
        self.source.write_bytes(OLE + b"legacy powerpoint PAYLOAD")
        os.utime(self.source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
        second = cache.convert(self.source)
        self.assertEqual(len(calls), 2)
        self.assertIsNot(second, first)
        self.assertFalse(first.path.exists())

    def test_failure_is_remembered(self) -> None:
        calls, converter = self._counting(fail=True)
        cache = PptConversionCache(converter)
        for _ in range(2):
            with self.assertRaises(PptConversionError):
                cache.convert(self.source)
        self.assertEqual(len(calls), 1)

    def test_release_forgets_one_file_and_deletes_its_copy(self) -> None:
        calls, converter = self._counting()
        other = self.root / "另一份.ppt"
        other.write_bytes(b"another legacy payload")
        cache = PptConversionCache(converter)
        first = cache.convert(self.source)
        kept = cache.convert(other)
        cache.release(self.source)
        self.assertFalse(first.path.exists())
        self.assertTrue(kept.path.exists())
        cache.convert(self.source)
        cache.convert(other)
        self.assertEqual(len(calls), 3)

    def test_release_after_failure_allows_retry(self) -> None:
        calls, converter = self._counting(fail=True)
        cache = PptConversionCache(converter)
        with self.assertRaises(PptConversionError):
            cache.convert(self.source)
        cache.release(self.source)
        with self.assertRaises(PptConversionError):
            cache.convert(self.source)
        self.assertEqual(len(calls), 2)

    def test_cleanup_removes_temporary_files(self) -> None:
        _calls, converter = self._counting()
        with PptConversionCache(converter) as cache:
            result = cache.convert(self.source)
            self.assertTrue(result.path.exists())
        self.assertFalse(result.path.exists())
        self.assertTrue(self.source.exists())


class _FakePresentation:
    def __init__(self, app: _FakeApp) -> None:
        self.app = app
        self.saved: list[tuple[str, int]] = []

    def SaveAs(self, path: str, file_format: int) -> None:  # noqa: N802 - COM 名字
        if self.app.fail_save:
            raise RuntimeError("(-2147352567, 'PowerPoint could not save')")
        self.saved.append((path, file_format))
        _write_pptx(Path(path))

    def Close(self) -> None:  # noqa: N802
        self.app.presentations.items.remove(self)
        self.app.closed += 1


class _FakePresentations:
    def __init__(self, app: _FakeApp, already_open: int) -> None:
        self.app = app
        self.items: list[object] = [object() for _ in range(already_open)]
        self.open_calls: list[tuple[str, dict]] = []

    @property
    def Count(self) -> int:  # noqa: N802
        return len(self.items)

    def Open(self, path: str, **kwargs) -> _FakePresentation:  # noqa: N802
        self.open_calls.append((path, kwargs))
        presentation = _FakePresentation(self.app)
        self.items.append(presentation)
        return presentation


class _FakeApp:
    def __init__(self, *, already_open: int = 0, fail_save: bool = False) -> None:
        self.presentations = _FakePresentations(self, already_open)
        self.DisplayAlerts = 2
        self.AutomationSecurity = 1
        self.fail_save = fail_save
        self.quit_calls = 0
        self.closed = 0

    @property
    def Presentations(self) -> _FakePresentations:  # noqa: N802
        return self.presentations

    def Quit(self) -> None:  # noqa: N802
        self.quit_calls += 1


class NativeCopyTests(_Workspace):
    """PowerPoint opens a private copy: the original is never opened, touched or closed."""

    def test_powerpoint_gets_a_copy_and_the_copy_is_deleted(self) -> None:
        seen: list[Path] = []
        before = self.source.stat().st_mtime_ns

        def convert(source: Path, output: Path) -> None:
            seen.append(source)
            self.assertNotEqual(source.resolve(), self.source.resolve())
            self.assertEqual(source.read_bytes(), self.source.read_bytes())
            self.assertEqual(source.suffix, ".ppt")
            _write_pptx(output)

        with (
            patch.object(ppt_converter.platform, "system", return_value="Darwin"),
            patch.object(ppt_converter, "get_local_powerpoint_availability", return_value=(True, "")),
            patch.object(ppt_converter, "_convert_with_macos_powerpoint", side_effect=convert),
        ):
            output = ppt_converter.convert_with_native_powerpoint(self.source)
        try:
            self.assertEqual(len(seen), 1)
            self.assertFalse(seen[0].exists())
            self.assertEqual(seen[0].parent, output.parent)
            validate_pptx(output)
            self.assertEqual(self.source.stat().st_mtime_ns, before)
        finally:
            output.unlink(missing_ok=True)

    def test_failed_conversion_deletes_copy_and_output(self) -> None:
        seen: list[tuple[Path, Path]] = []

        def convert(source: Path, output: Path) -> None:
            seen.append((source, output))
            _write_pptx(output)
            raise PptConversionError("PowerPoint 未能完成本次转换。")

        with (
            patch.object(ppt_converter.platform, "system", return_value="Windows"),
            patch.object(ppt_converter, "get_local_powerpoint_availability", return_value=(True, "")),
            patch.object(ppt_converter, "_convert_with_windows_powerpoint", side_effect=convert),
        ):
            with self.assertRaises(PptConversionError):
                ppt_converter.convert_with_native_powerpoint(self.source)
        source, output = seen[0]
        self.assertFalse(source.exists())
        self.assertFalse(output.exists())
        self.assertTrue(self.source.exists())


class WindowsPowerPointTests(_Workspace):
    def _run(self, app: _FakeApp) -> Path:
        pythoncom = types.SimpleNamespace(CoInitialize=lambda: None, CoUninitialize=lambda: None)
        client = types.SimpleNamespace(DispatchEx=lambda progid: app if progid == "PowerPoint.Application" else None)
        win32com = types.ModuleType("win32com")
        win32com.client = client
        output = self.root / "out.pptx"
        with patch.dict(sys.modules, {"pythoncom": pythoncom, "win32com": win32com, "win32com.client": client}):
            ppt_converter._convert_with_windows_powerpoint(self.source, output)
        return output

    def test_opens_read_only_without_window_and_saves_as_pptx(self) -> None:
        app = _FakeApp()
        output = self._run(app)

        path, kwargs = app.presentations.open_calls[0]
        self.assertEqual(path, str(self.source.resolve()))
        self.assertEqual(kwargs, {"ReadOnly": -1, "Untitled": 0, "WithWindow": 0})
        validate_pptx(output)
        self.assertEqual(app.closed, 1)
        self.assertEqual(app.quit_calls, 1)

    def test_never_quits_when_user_already_had_presentations_open(self) -> None:
        app = _FakeApp(already_open=1)
        self._run(app)

        self.assertEqual(app.quit_calls, 0)
        self.assertEqual(app.DisplayAlerts, 2)
        self.assertEqual(app.AutomationSecurity, 1)
        self.assertEqual(app.presentations.Count, 1)

    def test_failed_save_still_closes_and_reports_sentence(self) -> None:
        app = _FakeApp(fail_save=True)
        with self.assertRaises(PptConversionError):
            self._run(app)
        self.assertEqual(app.closed, 1)
        self.assertEqual(app.quit_calls, 1)


class MacPowerPointTests(_Workspace):
    def _run(self, *, stdout: str = "", returncode: int = 0, stderr: str = "", running: list[bool]):
        """Fake osascript plus ``pgrep``; ``running`` answers each 「PowerPoint 在不在」 check in turn."""
        scripts: list[list[str]] = []
        checks: list[bool] = []

        def run(command, **kwargs):
            if Path(command[0]).name == "pgrep":
                checks.append(running.pop(0) if running else False)
                return subprocess.CompletedProcess(command, 0 if checks[-1] else 1, b"", b"")
            scripts.append(command)
            return subprocess.CompletedProcess(command, returncode, stdout, stderr)

        with (
            patch.object(ppt_converter.subprocess, "run", side_effect=run),
            patch.object(ppt_converter.time, "sleep"),
        ):
            ppt_converter._convert_with_macos_powerpoint(self.source, self.root / "out.pptx")
        return scripts, checks

    def test_script_saves_open_xml_and_quits_only_when_nobody_was_using_powerpoint(self) -> None:
        scripts, _checks = self._run(stdout="kept\n", running=[True])

        command = scripts[0]
        script = command[2]
        self.assertIn('tell application "Microsoft PowerPoint"', script)
        self.assertIn("as save as Open XML presentation", script)
        self.assertIn("set shouldQuit to not wasRunning", script)
        self.assertIn("if openBefore is 0 then", script)
        self.assertIn("if shouldQuit then quit saving no", script)
        self.assertEqual(command[3:], [str(self.source.resolve()), str(self.root / "out.pptx")])

    def test_waits_until_powerpoint_has_really_quit(self) -> None:
        # 退出后立刻再转下一份，会把正在退出的 PowerPoint 当成用户开着的，转完不退。
        _scripts, checks = self._run(stdout="quit\n", running=[False, True, True, False])
        self.assertEqual(checks, [False, True, True, False])

    def test_does_not_wait_when_powerpoint_stays_open_for_the_user(self) -> None:
        _scripts, checks = self._run(stdout="kept\n", running=[True, True])
        self.assertEqual(checks, [True])

    def test_permission_denial_becomes_sentence(self) -> None:
        with self.assertRaises(PptConversionError) as caught:
            self._run(returncode=1, stderr="Not authorized to send Apple events (-1743)", running=[True])
        self.assertIn("自动化", str(caught.exception))


_FLAT_ODP = """<?xml version="1.0" encoding="UTF-8"?>
<office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
 xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0"
 office:version="1.3" office:mimetype="application/vnd.oasis.opendocument.presentation">
 <office:body><office:presentation>
  <draw:page draw:name="p1"><draw:frame svg:width="20cm" svg:height="3cm" svg:x="2cm" svg:y="2cm">
   <draw:text-box><text:p>项目进度汇报</text:p></draw:text-box></draw:frame></draw:page>
  <draw:page draw:name="p2"><draw:frame svg:width="20cm" svg:height="3cm" svg:x="2cm" svg:y="2cm">
   <draw:text-box><text:p>第二页：施工计划</text:p></draw:text-box></draw:frame></draw:page>
 </office:presentation></office:body></office:document>
"""


@unittest.skipIf(_find_soffice() is None, "需要 LibreOffice")
class RealLibreOfficeTests(unittest.TestCase):
    """真的跑一遍 soffice：先把一份两页的演示文稿存成 .ppt，再用转换器转回 .pptx。"""

    def test_legacy_ppt_round_trip_keeps_slides_and_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "deck.fodp").write_text(_FLAT_ODP, encoding="utf-8")
            subprocess.run(
                [
                    str(_find_soffice()),
                    "--headless",
                    f"-env:UserInstallation={(root / 'profile').as_uri()}",
                    "--convert-to",
                    "ppt",
                    "--outdir",
                    str(root),
                    str(root / "deck.fodp"),
                ],
                capture_output=True,
                check=True,
                timeout=120,
            )
            legacy = root / "项目 汇报.ppt"
            (root / "deck.ppt").rename(legacy)
            self.assertTrue(legacy.read_bytes().startswith(b"\xd0\xcf\x11\xe0"), "应是旧版二进制格式")

            with PptConversionCache() as cache:
                with patch.object(
                    ppt_converter, "convert_with_native_powerpoint", _missing("未找到 Microsoft PowerPoint。")
                ):
                    result = cache.convert(legacy)
                    again = cache.convert(legacy)
                self.assertIs(result, again)
                self.assertEqual(result.method, "LibreOffice")
                self.assertTrue(result.path.name.startswith("项目 汇报_"))
                with zipfile.ZipFile(result.path) as archive:
                    slides = sorted(
                        name for name in archive.namelist()
                        if name.startswith("ppt/slides/slide") and name.endswith(".xml")
                    )
                    text = "".join(archive.read(name).decode("utf-8") for name in slides)
                self.assertEqual(len(slides), 2)
                self.assertIn("项目进度汇报", text)
                self.assertIn("第二页：施工计划", text)
            self.assertFalse(result.path.exists())


if __name__ == "__main__":
    unittest.main()
