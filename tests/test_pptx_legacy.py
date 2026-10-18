"""Old .ppt / .pps files on the 「PPT」 page (``adapters/pptx`` + ``core/ppt_converter``).

Reading converts the file once to a temporary .pptx; extracting and writing
reuse that copy, writing deletes it, and the output is .pptx.  The conversion
itself is faked here except in the last test, which runs the real LibreOffice.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from api.surfaces.adapter import AdapterSurface
from core.format_adapter import AdapterError, InProcessAdapterHost
from core.ppt_converter import (
    FIDELITY_COMPATIBILITY,
    PptConversionCache,
    PptConversionError,
    PptConversionResult,
)
from core.task_runner_messages import DoneMsg
from core.word_converter import _find_soffice
from tests.test_pptx_adapter import FIXTURES, PPTX_DIR, FrenchEngine, PptxTaskTests, _texts, french
from translator_adapter import MODE_BILINGUAL, MODE_TRANSLATION_ONLY, TranslatedUnit


class FakeConversions:
    """Stands in for PowerPoint: "converts" by copying a .pptx fixture."""

    def __init__(self, fixture: Path, *, method: str = "PowerPoint", fail: str = "") -> None:
        self.fixture = fixture
        self.method = method
        self.fail = fail
        self.calls: list[Path] = []
        self.outputs: list[Path] = []
        self.workdir = Path(tempfile.mkdtemp(prefix="fake_ppt_"))

    def __call__(self, path: Path) -> PptConversionResult:
        self.calls.append(Path(path))
        if self.fail:
            raise PptConversionError(self.fail)
        output = self.workdir / f"converted_{len(self.calls)}.pptx"
        shutil.copyfile(self.fixture, output)
        self.outputs.append(output)
        fidelity = FIDELITY_COMPATIBILITY if self.method == "LibreOffice" else "high_fidelity"
        return PptConversionResult(source=Path(path), path=output, method=self.method, fidelity=fidelity)


def _host_with(conversions: FakeConversions) -> InProcessAdapterHost:
    host = InProcessAdapterHost(PPTX_DIR)
    module = host._load()
    patcher = patch.object(module, "_LEGACY", PptConversionCache(conversions))
    patcher.start()
    host._patcher = patcher  # type: ignore[attr-defined]
    return host


def _legacy(tmp_path: Path, name: str = "讲义.ppt") -> Path:
    path = tmp_path / name
    path.write_bytes(b"\xd0\xcf\x11\xe0 legacy powerpoint")
    return path


def test_read_extract_write_share_one_conversion(tmp_path: Path) -> None:
    conversions = FakeConversions(FIXTURES / "training.pptx")
    host = _host_with(conversions)
    try:
        source = _legacy(tmp_path)
        scanned = host.scan(source)
        expected = InProcessAdapterHost(PPTX_DIR).scan(FIXTURES / "training.pptx")
        assert scanned.unit_count == expected.unit_count
        assert scanned.warnings == ["旧版 .ppt 已用 PowerPoint 转换为 .pptx，输出为 .pptx", *expected.warnings]

        units = host.extract(source)
        translated = [TranslatedUnit(unit.id, unit.text, f"FR {unit.text}") for unit in units]
        output = host.write(source, translated, MODE_TRANSLATION_ONLY, tmp_path / "讲义_法文.ppt", {"target_lang": "fr"})

        assert len(conversions.calls) == 1
        assert Path(output.path) == tmp_path / "讲义_法文.pptx"
        assert not (tmp_path / "讲义_法文.ppt").exists()
        assert _texts(Path(output.path), "ppt/slides/slide1.xml") == ["FR 安全生产培训", "FR 新员工入职第一课"]
        # The temporary copy goes once the translation is written; the source stays.
        assert not conversions.outputs[0].exists()
        assert source.exists()
    finally:
        host._patcher.stop()  # type: ignore[attr-defined]


def test_task_reuses_the_reading_conversion_after_the_file_was_touched(tmp_path: Path) -> None:
    # 读取后文件的修改时间被碰过（同步盘、杀毒软件），内容没变：开始翻译不再拉起 PowerPoint。
    conversions = FakeConversions(FIXTURES / "training.pptx")
    host = _host_with(conversions)
    try:
        source = _legacy(tmp_path)
        host.scan(source)
        stat = source.stat()
        os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
        host.scan(source)
        units = host.extract(source)
        host.write(source, [], MODE_TRANSLATION_ONLY, tmp_path / "讲义_法文.ppt", {"target_lang": "fr"})
        assert len(conversions.calls) == 1
        assert len(units) > 0
    finally:
        host._patcher.stop()  # type: ignore[attr-defined]


def test_libreoffice_conversion_says_layout_may_differ(tmp_path: Path) -> None:
    host = _host_with(FakeConversions(FIXTURES / "training.pptx", method="LibreOffice"))
    try:
        warnings = host.scan(_legacy(tmp_path, "放映.PPS")).warnings
        assert warnings[0] == "旧版 .pps 已用 LibreOffice 转换为 .pptx，输出为 .pptx，版式可能与原文件略有差异"
    finally:
        host._patcher.stop()  # type: ignore[attr-defined]


def test_output_name_keeps_old_suffix_when_a_pptx_twin_exists(tmp_path: Path) -> None:
    conversions = FakeConversions(FIXTURES / "training.pptx")
    host = _host_with(conversions)
    try:
        source = _legacy(tmp_path, "汇报.ppt")
        shutil.copyfile(FIXTURES / "training.pptx", tmp_path / "汇报.pptx")
        translated = [TranslatedUnit(unit.id, unit.text, unit.text) for unit in host.extract(source)]
        out = tmp_path / "out"
        out.mkdir()
        output = host.write(source, translated, MODE_BILINGUAL, out / "汇报_法文_双语.ppt", {})
        assert Path(output.path).name == "汇报_法文_双语_ppt.pptx"
    finally:
        host._patcher.stop()  # type: ignore[attr-defined]


def test_failed_conversion_is_a_readable_error_and_retried_next_time(tmp_path: Path) -> None:
    message = "本机未找到 PowerPoint 或 LibreOffice，无法转换旧版文件。请先在 PowerPoint 里将它另存为 .pptx，再翻译另存后的文件。"
    conversions = FakeConversions(FIXTURES / "training.pptx", fail=message)
    host = _host_with(conversions)
    try:
        source = _legacy(tmp_path)
        for _ in range(2):
            with pytest.raises(AdapterError) as caught:
                host.scan(source)
            assert str(caught.value) == message
        assert len(conversions.calls) == 2
    finally:
        host._patcher.stop()  # type: ignore[attr-defined]


def test_new_format_files_never_touch_the_converter(tmp_path: Path) -> None:
    conversions = FakeConversions(FIXTURES / "training.pptx", fail="不该调用")
    host = _host_with(conversions)
    try:
        assert host.scan(FIXTURES / "training.pptx").unit_count > 0
        assert conversions.calls == []
    finally:
        host._patcher.stop()  # type: ignore[attr-defined]


class LegacyTaskTests(PptxTaskTests):
    """A whole task on the shared executor with an old .ppt as input."""

    def setUp(self) -> None:
        super().setUp()
        self.conversions = FakeConversions(FIXTURES / "mixed.pptx")
        host = InProcessAdapterHost(PPTX_DIR, self.manifest)
        patcher = patch.object(host._load(), "_LEGACY", PptConversionCache(self.conversions))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.surface = AdapterSurface(self.manifest, host)

    def test_a_whole_task_on_an_old_ppt_writes_pptx(self) -> None:
        path = self.source / "项目进展汇报.ppt"
        path.write_bytes(b"\xd0\xcf\x11\xe0 legacy powerpoint")
        scanned = self.surface.scan_sources(self.source, None)
        assert [item.name for item in scanned.items] == ["项目进展汇报.ppt"]
        assert scanned.items[0].warnings[0] == "旧版 .ppt 已用 PowerPoint 转换为 .pptx，输出为 .pptx"
        messages = self._run(FrenchEngine(), [path])
        done = next(message for message in messages if isinstance(message, DoneMsg))
        result = done.file_results[0]
        out = Path(result["output_path"])
        assert out.name == "项目进展汇报_法文.pptx" and out.is_file()
        # Same as the .pptx task: the vertical text box on slide 7 is listed for review.
        assert [(item["location"], item["issue"]) for item in done.review["items"]] == [
            ("第 7 页 · TextBox 1", "竖排文本框，译文可能需要改为横排")
        ]
        assert result["status"] == "needs_review" and result["review_count"] == 1, result
        assert _texts(out, "ppt/slides/slide2.xml")[0] == french("项目概况")
        # Read, extracted and written from one conversion, deleted afterwards.
        assert len(self.conversions.calls) == 1
        assert not self.conversions.outputs[0].exists()

    # The inherited .pptx cases already run in test_pptx_adapter.
    test_a_whole_task_writes_a_translated_deck = None  # type: ignore[assignment]
    test_bilingual_option_writes_both = None  # type: ignore[assignment]


@pytest.mark.skipif(_find_soffice() is None, reason="需要 LibreOffice")
class RealLibreOfficeTaskTests(PptxTaskTests):
    """No fakes on the conversion: soffice saves a deck as .ppt, the task converts it back."""

    def test_old_ppt_is_translated_through_libreoffice(self) -> None:
        work = self.app_data_dir.parent / "make"
        work.mkdir()
        subprocess.run(
            [
                str(_find_soffice()),
                "--headless",
                f"-env:UserInstallation={(work / 'profile').as_uri()}",
                "--convert-to",
                "ppt",
                "--outdir",
                str(work),
                str(FIXTURES / "training.pptx"),
            ],
            capture_output=True,
            check=True,
            timeout=120,
        )
        path = self.source / "安全培训.ppt"
        shutil.move(str(work / "training.ppt"), path)
        assert path.read_bytes().startswith(b"\xd0\xcf\x11\xe0")

        with patch("core.ppt_converter.convert_with_native_powerpoint", _no_powerpoint):
            scanned = self.surface.scan_sources(self.source, None)
            assert scanned.items[0].warnings[0].startswith("旧版 .ppt 已用 LibreOffice 转换为 .pptx")
            messages = self._run(FrenchEngine(), [path], options={"translate_notes": False})
        done = next(message for message in messages if isinstance(message, DoneMsg))
        result = done.file_results[0]
        out = Path(result["output_path"])
        assert out.name == "安全培训_法文.pptx", result
        assert result["status"] in {"completed", "needs_review"}, result
        assert _texts(out, "ppt/slides/slide1.xml") == [french("安全生产培训"), french("新员工入职第一课")]

    test_a_whole_task_writes_a_translated_deck = None  # type: ignore[assignment]
    test_bilingual_option_writes_both = None  # type: ignore[assignment]


def _no_powerpoint(_path: Any) -> Path:
    from core.ppt_converter import _ToolMissing

    raise _ToolMissing("未找到 Microsoft PowerPoint。")
