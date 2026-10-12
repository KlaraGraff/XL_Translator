import pytest
import hashlib
import json
import subprocess
from pathlib import Path

from core.cad_translation import (
    CadPipelineOptions,
    CadProgress,
    CadTranslationPipeline,
    DxfRecord,
    extract_cad_text_units,
    scan_replacement_characters,
    scan_cad_file,
    scan_cad_paths,
    SubprocessCadConverter,
)
from core.cad_translation import _parse_dxf, _serialize_dxf
from core.cad_translation import CadPipelineError
from core.cad_translation import _is_likely_target_language


class FakeConverter:
    def dwg_to_dxf(self, source, destination):
        destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        return destination

    def dxf_to_dwg(self, source, destination):
        destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        return destination


def _dxf(*pairs):
    return "".join(f"{code}\n{value}\n" for code, value in pairs)


def test_extracts_visible_text_and_excludes_mleader_structure_and_metadata():
    records = [
        DxfRecord(0, "TEXT"), DxfRecord(1, "Door"),
        DxfRecord(0, "MTEXT"), DxfRecord(3, "Steel "), DxfRecord(1, "Door"),
        DxfRecord(0, "MULTILEADER"), DxfRecord(300, "CONTEXT_DATA"), DxfRecord(304, "Frame"),
        DxfRecord(304, "LEADER_METADATA"), DxfRecord(302, "LEADER_LINE"), DxfRecord(304, "Geometry"),
        DxfRecord(0, "ATTDEF"), DxfRecord(3, "TAG_PROMPT"), DxfRecord(1, "Default"),
        DxfRecord(0, "ATTRIB"), DxfRecord(3, "ATTRIB_PROMPT"), DxfRecord(1, "Value"),
        DxfRecord(0, "DIMENSION"), DxfRecord(1, "Dim override"),
    ]
    units = extract_cad_text_units(records)
    assert [unit.plain_text for unit in units] == ["Door", "Steel Door", "Frame", "Default", "Value", "Dim override"]
    assert all("LEADER_" not in unit.plain_text for unit in units)


def test_pipeline_uses_memory_then_glossary_then_ai_and_writes_dwg(tmp_path):
    source = tmp_path / "sample.dwg"
    source.write_text(_dxf((0, "TEXT"), (1, "Memory"), (0, "TEXT"), (1, "Term"), (0, "TEXT"), (1, "AI")), encoding="utf-8")
    output = tmp_path / "sample_zh.dwg"
    seen = []

    def memory(texts):
        return {text: "记忆译文" for text in texts if text == "Memory"}

    def translate(texts, glossary):
        seen.append((texts, dict(glossary)))
        return {"AI": "人工智能"}

    progress: list[CadProgress] = []
    result = CadTranslationPipeline(
        converter=FakeConverter(), memory_lookup=memory, translator=translate, progress=progress.append
    ).translate_file(source, output, glossary={"Term": "术语译文"})

    text = output.read_text(encoding="utf-8")
    assert r"\U+8BB0\U+5FC6" in text
    assert r"\U+672F\U+8BED" in text
    assert r"\U+4EBA\U+5DE5" in text
    assert seen == [(["AI"], {"Term": "术语译文"})]
    assert result.stats.memory_hits == 1
    assert result.stats.glossary_hits == 1
    assert result.stats.ai_translated == 1
    assert result.unresolved == []
    assert result.manifest.exists() and result.report.exists() and result.work_dxf.exists()
    assert progress[-1].stage == "verify" and progress[-1].percent == 100


def test_translate_stage_reports_progress_per_batch(tmp_path):
    """本机实测：真实 DWG 翻译时进度停在一处一分多钟，然后一下跳过去。

    翻译器接受 ``progress`` 时，每完成一批都要推进「AI 翻译」阶段（10%→90%）。
    """
    source = tmp_path / "batches.dxf"
    source.write_text(
        _dxf(*[pair for word in ("Alpha", "Bravo", "Charlie", "Delta") for pair in ((0, "TEXT"), (1, word))]),
        encoding="utf-8",
    )
    progress: list[CadProgress] = []
    seen_during_translation: list[float] = []

    def translate(texts, glossary, progress=None):
        for done in range(1, 3):
            progress(done, 2)
            seen_during_translation.append(progresses[-1].percent)
        return {text: f"{text}译" for text in texts}

    progresses = progress
    CadTranslationPipeline(converter=None, translator=translate, progress=progress.append).translate_file(
        source, tmp_path / "batches_zh.dxf", options=CadPipelineOptions(target_lang="zh", source_lang="auto")
    )

    assert seen_during_translation == [50.0, 90.0]
    translate_percents = [item.percent for item in progress if item.stage == "translate"]
    assert translate_percents == sorted(translate_percents)
    assert translate_percents[0] == 10.0


def test_two_argument_translator_still_works(tmp_path):
    source = tmp_path / "old.dxf"
    source.write_text(_dxf((0, "TEXT"), (1, "Door")), encoding="utf-8")
    result = CadTranslationPipeline(converter=None, translator=lambda texts, glossary: {"Door": "门"}).translate_file(
        source, tmp_path / "old_zh.dxf"
    )
    assert result.stats.ai_translated == 1


def test_target_language_skip_and_replacement_scan(tmp_path):
    source = tmp_path / "mixed.dxf"
    source.write_text(_dxf((0, "TEXT"), (1, "中文"), (0, "TEXT"), (1, "Bad")), encoding="utf-8")
    output = tmp_path / "mixed_zh.dxf"
    result = CadTranslationPipeline(converter=None, translator=lambda texts, glossary: {"Bad": "好"}).translate_file(source, output)
    assert result.stats.skipped_target_language == 1
    assert result.stats.changed == 1
    assert scan_replacement_characters([DxfRecord(1, "a�b")]) == ["a�b"]


def test_scan_reports_conversion_requirement_and_expands_directories(tmp_path):
    dxf = tmp_path / "a.dxf"
    dxf.write_text(_dxf((0, "TEXT"), (1, "Door")), encoding="utf-8")
    dwg = tmp_path / "b.dwg"
    dwg.write_text(_dxf((0, "TEXT"), (1, "Door")), encoding="utf-8")
    no_converter = scan_cad_file(dwg)
    assert no_converter.needs_conversion is True
    assert no_converter.counts_pending is True and no_converter.candidate_count is None
    connected = scan_cad_file(dwg, converter_available=True)
    # 扫描不转换 DWG：连上转换工具也只列出来，文字数开始翻译后由任务统计。
    assert connected.needs_conversion is False
    assert connected.counts_pending is True
    assert connected.text_entity_count is None and connected.candidate_count is None
    direct = scan_cad_file(dxf)
    assert direct.counts_pending is False and direct.candidate_count == 1 and direct.text_entity_count == 1
    summaries = scan_cad_paths([tmp_path], converter_available=True)
    assert [item.filename for item in summaries] == ["a.dxf", "b.dwg"]


def test_scan_never_calls_the_converter_for_dwg(tmp_path, monkeypatch):
    """扫描一页要几十张 DWG 时，以前每张都调一次外部转换工具，页面一直转圈。"""
    calls: list[str] = []

    def forbidden(self, source, destination, output_type):
        calls.append(str(source))
        raise AssertionError("scan must not convert")

    monkeypatch.setattr(SubprocessCadConverter, "_run", forbidden)
    for index in range(3):
        (tmp_path / f"d{index}.dwg").write_bytes(b"AC1032 fake dwg")
    (tmp_path / "plain.dxf").write_text(_dxf((0, "TEXT"), (1, "Door")), encoding="utf-8")
    skipped: list[dict[str, str]] = []
    summaries = scan_cad_paths([tmp_path], converter_available=True, skipped=skipped)
    assert calls == []
    assert skipped == []
    pending = [item.filename for item in summaries if item.counts_pending]
    assert pending == ["d0.dwg", "d1.dwg", "d2.dwg"]


def test_long_mtext_is_chunked_without_splitting_unicode_escapes(tmp_path):
    source = tmp_path / "long.dxf"
    source.write_text(_dxf((0, "MTEXT"), (3, "Long "), (1, "paragraph")), encoding="utf-8")
    output = tmp_path / "long_zh.dxf"
    CadTranslationPipeline(converter=None, translator=lambda texts, glossary: {"Long paragraph": "中" * 120}).translate_file(source, output)
    lines = output.read_text(encoding="utf-8").splitlines()
    values = [lines[index + 1] for index in range(0, len(lines), 2) if lines[index] in {"1", "3"}]
    assert all(len(value) <= 240 for value in values)
    assert "".join(values).count(r"\U+4E2D") == 120


def test_parser_roundtrip_preserves_code_spacing_and_line_endings():
    raw = "  0\r\nMTEXT\r\n  1\r\nDoor\r\n"
    assert _serialize_dxf(_parse_dxf(raw)) == raw


def test_untouched_dxf_bytes_survive_pipeline(tmp_path):
    source = tmp_path / "untouched.dxf"
    raw = b"  0\r\nTEXT\r\n  1\r\nDoor\r\n  8\r\nLayer_A\r\n"
    source.write_bytes(raw)
    output = tmp_path / "untouched_zh.dxf"
    CadTranslationPipeline(converter=None, translator=lambda texts, glossary: {}).translate_file(source, output)
    assert output.read_bytes() == raw
    manifest = json.loads((output.with_suffix(output.suffix + ".manifest.json")).read_text(encoding="utf-8"))
    assert manifest["source_sha256"] == hashlib.sha256(raw).hexdigest()


def test_unicode_escape_before_ascii_word_is_one_visible_run(tmp_path):
    source = tmp_path / "unicode.dxf"
    source.write_text(_dxf((0, "MTEXT"), (1, r"\U+4E00Door")), encoding="utf-8")
    output = tmp_path / "unicode_zh.dxf"
    seen = []
    CadTranslationPipeline(converter=None, translator=lambda texts, glossary: seen.extend(texts) or {}).translate_file(
        source, output, options=CadPipelineOptions(skip_target_language=False)
    )
    assert seen == ["一Door"]


def test_stop_requested_is_checked_before_work(tmp_path):
    source = tmp_path / "stop.dxf"
    source.write_text(_dxf((0, "TEXT"), (1, "Door")), encoding="utf-8")
    try:
        CadTranslationPipeline(converter=None, translator=lambda texts, glossary: {}).translate_file(
            source, tmp_path / "stop_zh.dxf", stop_requested=lambda: True
        )
    except CadPipelineError as exc:
        assert "停止" in str(exc)
    else:
        raise AssertionError("stop callback was ignored")


def test_mtext_translates_plain_runs_without_sending_format_controls(tmp_path):
    source = tmp_path / "runs.dxf"
    source.write_text(_dxf((0, "MTEXT"), (1, r"{\fArial|b1;Door \P"), (3, r"Next}")), encoding="utf-8")
    output = tmp_path / "runs_zh.dxf"
    seen = []

    def translate(texts, glossary):
        seen.append(texts)
        return {"Door": "门", "Next": "下一项"}

    CadTranslationPipeline(converter=None, translator=translate).translate_file(source, output)
    text = output.read_text(encoding="utf-8")
    assert seen == [["Door", "Next"]]
    assert r"\fArial|b1;" in text and r"\P" in text and r"\U+95E8" in text
    assert r"\U+4E0B\U+4E00\U+9879" in text


def test_invalid_numeric_translation_is_kept_out_of_output(tmp_path):
    source = tmp_path / "number.dxf"
    source.write_text(_dxf((0, "TEXT"), (1, "Bolt 12")), encoding="utf-8")
    output = tmp_path / "number_zh.dxf"
    result = CadTranslationPipeline(converter=None, translator=lambda texts, glossary: {"Bolt 12": "螺栓 13"}).translate_file(source, output)
    assert "Bolt 12" in output.read_text(encoding="utf-8")
    assert result.unresolved[0]["reason"] == "译文中的数字与原文不一致"


def test_roundtrip_false_does_not_read_back_dwg(tmp_path):
    source = tmp_path / "sample.dwg"
    source.write_text(_dxf((0, "TEXT"), (1, "Door")), encoding="utf-8")

    class NoReadback(FakeConverter):
        def dwg_to_dxf(self, source, destination):
            if "staged" in source.name:
                raise AssertionError("roundtrip readback should be disabled")
            return super().dwg_to_dxf(source, destination)

    output = tmp_path / "sample_zh.dwg"
    CadTranslationPipeline(converter=NoReadback(), translator=lambda texts, glossary: {"Door": "门"}).translate_file(
        source, output, options=CadPipelineOptions(verify_roundtrip=False)
    )
    assert output.exists()


def test_target_language_detection_covers_scripts_and_latin_families():
    assert _is_likely_target_language("中文", "zh")
    assert _is_likely_target_language("Plan de masse du bâtiment", "fr")
    assert _is_likely_target_language("Straße", "de")
    assert _is_likely_target_language("مرحبا", "ar")
    assert _is_likely_target_language("日本語", "ja")
    assert not _is_likely_target_language("Bonjour", "x-custom-foo")


def test_residual_scan_respects_non_english_target_language(tmp_path):
    source = tmp_path / "french.dxf"
    source.write_text(_dxf((0, "TEXT"), (1, "Bonjour")), encoding="utf-8")
    output = tmp_path / "french_zh.dxf"
    result = CadTranslationPipeline(
        converter=None,
        translator=lambda texts, glossary: {},
    ).translate_file(source, output, options=CadPipelineOptions(target_lang="fr"))
    assert result.stats.residual_foreign_text_count == 0


def test_macos_oda_does_not_force_missing_offscreen_qt_plugin(tmp_path, monkeypatch):
    source = tmp_path / "sample.dxf"
    source.write_text("DXF", encoding="utf-8")
    destination = tmp_path / "sample.dwg"
    captured = {}

    def fake_run(command, *, check, timeout, cwd, env, stdout, stderr, text):
        captured["env"] = env
        output_dir = Path(command[2])
        (output_dir / "sample.dwg").write_text("DWG", encoding="utf-8")

    monkeypatch.setattr("core.cad_translation.platform.system", lambda: "Darwin")
    monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)
    monkeypatch.setattr("core.cad_translation.subprocess.run", fake_run)

    SubprocessCadConverter("/tmp/ODAFileConverter").dxf_to_dwg(source, destination)

    assert destination.read_text(encoding="utf-8") == "DWG"
    assert "QT_QPA_PLATFORM" not in captured["env"]


def _fake_oda_app(tmp_path, monkeypatch, on_run):
    """A fake ODA .app; ``on_run(command, timeout)`` stands in for ``open -W``."""
    app_executable = tmp_path / "ODAFileConverter.app" / "Contents" / "MacOS" / "ODAFileConverter"
    app_executable.parent.mkdir(parents=True)
    app_executable.write_text("binary", encoding="utf-8")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[0] == "/usr/bin/open":
            return on_run(command, kwargs["timeout"])
        return None

    monkeypatch.setattr("core.cad_translation.platform.system", lambda: "Darwin")
    monkeypatch.setattr("core.cad_translation.subprocess.run", fake_run)
    monkeypatch.setattr("core.cad_translation.shutil.which", lambda name: "/usr/bin/open")
    return app_executable, calls


def test_macos_oda_app_is_launched_through_launchservices_and_waited_for(tmp_path, monkeypatch):
    source = tmp_path / "sample.dxf"
    source.write_text("DXF", encoding="utf-8")
    destination = tmp_path / "sample.dwg"

    def on_run(command, timeout):
        (Path(command[8]) / "sample.dwg").write_text("DWG", encoding="utf-8")

    app_executable, calls = _fake_oda_app(tmp_path, monkeypatch, on_run)

    SubprocessCadConverter(app_executable, timeout=2).dxf_to_dwg(source, destination)

    assert destination.read_text(encoding="utf-8") == "DWG"
    command = calls[0][0]
    # -W：open 等这个 ODA 实例退出（转换结束）才返回，而不是一启动就返回。
    assert command[:7] == [
        "/usr/bin/open", "-n", "-g", "-W", "-a", str(app_executable.parent.parent.parent), "--args"
    ]
    assert command[7].endswith("/in")
    assert command[8].endswith("/out")
    assert calls[0][1]["timeout"] == 2


def test_macos_oda_dxf_without_eof_after_exit_is_a_conversion_failure(tmp_path, monkeypatch):
    # ODA 中途崩溃留下的半截 DXF，要报转换失败，不能当成图纸本身损坏去解析。
    def on_run(command, timeout):
        (Path(command[8]) / "sample.dxf").write_text(_dxf((0, "SECTION"), (2, "ENTITIES")), encoding="utf-8")

    source = tmp_path / "sample.dwg"
    source.write_bytes(b"AC1032")
    app_executable, _calls = _fake_oda_app(tmp_path, monkeypatch, on_run)

    with pytest.raises(CadPipelineError, match="中途退出"):
        SubprocessCadConverter(app_executable).dwg_to_dxf(source, tmp_path / "out.dxf")


def test_macos_oda_complete_dxf_is_returned(tmp_path, monkeypatch):
    complete = _dxf((0, "SECTION"), (2, "ENTITIES"), (0, "TEXT"), (1, "门"), (0, "ENDSEC"), (0, "EOF"))

    def on_run(command, timeout):
        (Path(command[8]) / "sample.dxf").write_text(complete, encoding="utf-8")

    source = tmp_path / "sample.dwg"
    source.write_bytes(b"AC1032")
    app_executable, _calls = _fake_oda_app(tmp_path, monkeypatch, on_run)

    destination = SubprocessCadConverter(app_executable).dwg_to_dxf(source, tmp_path / "out.dxf")

    assert destination.read_text(encoding="utf-8") == complete


def test_macos_oda_without_output_reports_no_file(tmp_path, monkeypatch):
    source = tmp_path / "sample.dwg"
    source.write_bytes(b"AC1032")
    app_executable, _calls = _fake_oda_app(tmp_path, monkeypatch, lambda command, timeout: None)

    with pytest.raises(CadPipelineError, match="未生成文件"):
        SubprocessCadConverter(app_executable).dwg_to_dxf(source, tmp_path / "out.dxf")


def test_macos_oda_timeout_stops_only_this_conversion(tmp_path, monkeypatch):
    def on_run(command, timeout):
        raise subprocess.TimeoutExpired(command, timeout)

    source = tmp_path / "sample.dwg"
    source.write_bytes(b"AC1032")
    app_executable, calls = _fake_oda_app(tmp_path, monkeypatch, on_run)

    with pytest.raises(CadPipelineError, match="超时"):
        SubprocessCadConverter(app_executable, timeout=5).dwg_to_dxf(source, tmp_path / "out.dxf")

    open_command = calls[0][0]
    assert calls[1][0] == ["pkill", "-f", open_command[7]]
    assert "xl-cad-convert-" in open_command[7]


def test_conversion_timeout_grows_with_drawing_size(tmp_path):
    small = tmp_path / "small.dwg"
    small.write_bytes(b"x" * 1000)
    large = tmp_path / "large.dwg"
    large.write_bytes(b"x" * (5 * 1024 * 1024))
    converter = SubprocessCadConverter("/tmp/ODAFileConverter", timeout=600)

    assert converter._timeout_for(small) == 600
    assert converter._timeout_for(large) == 600 + 5 * 30
    assert converter._timeout_for(tmp_path / "missing.dwg") == 600


def test_text_with_unicode_line_separators_is_not_split():
    # str.splitlines 会在这些字符处断行，把正常图纸错位成「格式无法识别」。
    raw = _dxf((0, "SECTION"), (2, "ENTITIES"), (0, "MTEXT"),
               (1, "甲\x1c乙\x0b丙\x0c丁\x85戊\u2028己\u2029庚\r辛"),
               (0, "ENDSEC"), (0, "EOF")).replace("\n", "\r\n")
    records = _parse_dxf(raw)
    assert [record.code for record in records] == [0, 2, 0, 1, 0, 0]
    assert records[3].value == "甲\x1c乙\x0b丙\x0c丁\x85戊\u2028己\u2029庚\r辛"
    assert _serialize_dxf(records) == raw


def test_mixed_cjk_text_is_not_skipped_for_latin_targets():
    # "Q235钢板" 里有拉丁字母，但中文部分仍需翻成英文/法文，不能当作"已是目标语言"跳过。
    assert not _is_likely_target_language("Q235钢板", "en")
    assert not _is_likely_target_language("M10螺栓", "fr")
    assert _is_likely_target_language("Q235 steel plate", "en")


def test_part_codes_do_not_block_cjk_target_detection():
    assert _is_likely_target_language("M10螺栓", "zh")
    assert _is_likely_target_language("GB/T 700 Q235钢板", "zh")
    assert not _is_likely_target_language("钢板 steel plate", "zh")


def test_numbers_and_other_language_text_do_not_force_review(tmp_path):
    source = tmp_path / "plan.dxf"
    source.write_text(
        _dxf((0, "TEXT"), (1, "1200"), (0, "TEXT"), (1, "Door"), (0, "TEXT"), (1, "M10螺栓")),
        encoding="utf-8",
    )
    result = CadTranslationPipeline(
        converter=FakeConverter(), translator=lambda texts, glossary: {"Door": "门"}
    ).translate_file(source, tmp_path / "plan_zh.dxf", options=CadPipelineOptions(source_lang="en", target_lang="zh"))
    assert result.stats.unsupported_entities == 0
    assert result.stats.skipped_non_text == 1
    # "M10螺栓" 已是中文（M10 是规范代号），既不算残留外文，也不需要翻译。
    assert result.stats.residual_foreign_text_count == 0


def test_stop_raises_dedicated_stop_error(tmp_path):
    from core.cad_translation import CadStopRequested

    source = tmp_path / "a.dxf"
    source.write_text(_dxf((0, "TEXT"), (1, "Door")), encoding="utf-8")
    try:
        CadTranslationPipeline(converter=FakeConverter()).translate_file(
            source, tmp_path / "a_zh.dxf", stop_requested=lambda: True
        )
    except CadStopRequested:
        pass
    else:
        raise AssertionError("expected CadStopRequested")


def test_scan_paths_collects_unreadable_files_when_asked(tmp_path):
    good = tmp_path / "good.dxf"
    good.write_text(_dxf((0, "TEXT"), (1, "Door")), encoding="utf-8")
    bad = tmp_path / "bad.dxf"  # 非 UTF-8 内容：单个文件读不动
    bad.write_bytes(b"0\nTEXT\n1\n\xff\xfe\x00bad\n")
    skipped: list[dict[str, str]] = []
    summaries = scan_cad_paths([tmp_path], skipped=skipped)
    assert [item.filename for item in summaries] == ["good.dxf"]
    assert [entry["name"] for entry in skipped] == ["bad.dxf"]


def test_numbers_next_to_chinese_characters_are_kept_in_the_check():
    from core.cad_translation import _numeric_tokens, _valid_translation

    assert _numeric_tokens("共12页") == ["12"]
    assert _numeric_tokens("M10 螺栓 Q235") == []
    assert _valid_translation("Fire exit route 12345", "消防通道12345") == (True, "")
    assert _valid_translation("Room 101", "房间102")[0] is False
    # 乘号后面的尺寸也要核对：2830X1700 改成 2830X2100 不能通过。
    assert _numeric_tokens("设备基坑：2830X1700mm") == ["2830", "1700"]
    assert _valid_translation("设备基坑：2830X1700mm", "Equipment pit: 2830X2100mm")[0] is False
    assert _valid_translation("设备基坑：2830X1700mm", "Equipment pit: 2830 × 1700 mm") == (True, "")


def test_truncated_dxf_is_rejected_instead_of_passing_as_empty_drawing():
    complete = _dxf((0, "SECTION"), (2, "ENTITIES"), (0, "TEXT"), (1, "门"), (0, "ENDSEC"), (0, "EOF"))
    assert [record.value for record in _parse_dxf(complete)][-1] == "EOF"
    truncated = _dxf((0, "SECTION"), (2, "ENT"))
    with pytest.raises(CadPipelineError, match="不完整"):
        _parse_dxf(truncated)


def test_gbk_dxf_is_read_and_written_back_in_its_own_codepage(tmp_path):
    """老格式 DXF 按 $DWGCODEPAGE（国内基本是 ANSI_936/GBK）存字，不能当 UTF-8 拒收。"""
    text = _dxf(
        (0, "SECTION"), (2, "HEADER"), (9, "$DWGCODEPAGE"), (3, "ANSI_936"), (0, "ENDSEC"),
        (0, "SECTION"), (2, "ENTITIES"), (0, "TEXT"), (8, "图层"), (1, "设备基础"),
        (0, "ENDSEC"), (0, "EOF"),
    )
    source = tmp_path / "old.dxf"
    source.write_bytes(text.encode("gbk"))
    assert scan_cad_file(source, options=CadPipelineOptions(target_lang="fr")).candidate_count == 1

    output = tmp_path / "old_fr.dxf"
    CadTranslationPipeline(
        converter=None,
        translator=lambda texts, glossary: {t: "Fondation d'équipement" for t in texts},
    ).translate_file(source, output, options=CadPipelineOptions(target_lang="fr"))
    written = output.read_bytes()
    decoded = written.decode("gbk")
    assert "图层" in decoded
    assert "\\U+00E9" in decoded or "é" in decoded


def _label_value_drawing(*, label_style="_TCH_DIM", font="SIMPLEX.shx", value_offset=1474.47, label_width=(41, "0.6")):
    """天正常见的「标签 + 数值」：两段单行文字紧挨着（坐标、字高取自真实图纸）。"""
    return _dxf(
        (0, "SECTION"), (2, "TABLES"),
        (0, "TABLE"), (2, "STYLE"), (70, "1"),
        (0, "STYLE"), (2, label_style), (70, "0"), (40, "0"), (41, "0.6"), (3, font), (4, "GBCBIG.shx"),
        (0, "ENDTAB"), (0, "ENDSEC"),
        (0, "SECTION"), (2, "ENTITIES"),
        (0, "TEXT"), (8, "PUB_TEXT"), (10, "5280.6"), (20, "2167.9"), (40, "675.18"), *([label_width] if label_width else []),
        (7, label_style), (1, "设备基坑："),
        (0, "TEXT"), (8, "PUB_TEXT"), (10, str(5280.6 + value_offset)), (20, "2167.9"), (40, "595"), (41, "0.4235"),
        (7, label_style), (1, "2830X1700mm"),
        (0, "TEXT"), (8, "PUB_TEXT"), (10, "9000"), (20, "9000"), (40, "675.18"), (7, label_style), (1, "坑底标高"),
        (0, "ENDSEC"), (0, "EOF"),
    )


def _text_fields(path):
    records = _parse_dxf(path.read_text(encoding="utf-8"))
    entities = []
    current = None
    for record in records:
        if record.code == 0:
            current = {} if record.value == "TEXT" else None
            if current is not None:
                entities.append(current)
        elif current is not None:
            current.setdefault(record.code, record.value)
    return entities


def _translate_label_value(tmp_path, drawing, **options):
    source = tmp_path / "pit.dxf"
    source.write_text(drawing, encoding="utf-8")
    output = tmp_path / "pit_en.dxf"
    messages = []
    translations = {"设备基坑：": "Equipment pit:", "坑底标高": "Pit bottom elevation"}
    result = CadTranslationPipeline(
        converter=None,
        translator=lambda texts, glossary: {text: translations[text] for text in texts},
        progress=lambda event: messages.append(event.message),
    ).translate_file(source, output, options=CadPipelineOptions(target_lang="en", **options))
    return result, _text_fields(output), messages


def test_translated_label_is_narrowed_instead_of_running_into_the_value(tmp_path):
    from core.cad_layout import estimate_text_width

    result, (label, value, lone), messages = _translate_label_value(tmp_path, _label_value_drawing())
    assert label[1] == "Equipment pit:"
    height, width_factor = float(label[40]), float(label[41])
    assert height < 675.18 and width_factor < 1.0
    # 缩小后的译文停在数值前面；数值本身原样不动。
    assert 5280.6 + estimate_text_width("Equipment pit:", height, width_factor) < float(value[10])
    assert (value[1], value[40], value[41]) == ("2830X1700mm", "595", "0.4235")
    # 旁边没有别的文字的译文不缩。
    assert lone[1] == "Pit bottom elevation" and lone[40] == "675.18" and 41 not in lone
    assert result.stats.layout_fitted == 1
    assert any("1 处译文比原文长" in message for message in messages)


def test_missing_width_factor_is_added_next_to_the_height(tmp_path):
    from core.cad_layout import estimate_text_width

    # 没写 41 的单行文字宽度比例是 1：收缩时要补上这一项，并且紧跟在字高后面。
    _result, (label, value, _lone), _messages = _translate_label_value(
        tmp_path, _label_value_drawing(label_width=None, value_offset=2560),
    )
    width_factor = float(label[41])
    assert width_factor < 1.0
    assert 5280.6 + estimate_text_width("Equipment pit:", float(label[40]), width_factor) < float(value[10])
    text = (tmp_path / "pit_en.dxf").read_text(encoding="utf-8")
    assert f"40\n{label[40]}\n41\n{label[41]}\n" in text


def test_label_with_room_to_spare_and_disabled_fitting_are_left_alone(tmp_path):
    _result, (label, *_rest), _messages = _translate_label_value(
        tmp_path, _label_value_drawing(value_offset=20000),
    )
    assert (label[40], label[41]) == ("675.18", "0.6")

    (tmp_path / "pit_en.dxf").unlink()
    for leftover in tmp_path.glob("pit_en.*"):
        if leftover.is_file():
            leftover.unlink()
    result, (label, *_rest), _messages = _translate_label_value(
        tmp_path, _label_value_drawing(), fit_layout=False,
    )
    assert (label[40], label[41]) == ("675.18", "0.6")
    assert result.stats.layout_fitted == 0


def test_text_on_another_line_or_in_another_block_is_not_a_neighbour():
    from core.cad_layout import fit_translated_texts
    from core.cad_translation import _plain_text

    records = _parse_dxf(_dxf(
        (0, "SECTION"), (2, "BLOCKS"),
        (0, "BLOCK"), (2, "A"),
        (0, "TEXT"), (10, "0"), (20, "0"), (40, "100"), (1, "Equipment pit:"),
        (0, "ENDBLK"),
        (0, "BLOCK"), (2, "B"),
        (0, "TEXT"), (10, "400"), (20, "0"), (40, "100"), (1, "2830X1700mm"),
        (0, "ENDBLK"),
        (0, "ENDSEC"),
        (0, "SECTION"), (2, "ENTITIES"),
        (0, "TEXT"), (10, "0"), (20, "0"), (40, "100"), (1, "Pit bottom elevation"),
        (0, "TEXT"), (10, "400"), (20, "500"), (40, "100"), (1, "-0.090"),
        (0, "ENDSEC"), (0, "EOF"),
    ))
    before = list(records)
    # 实体序号：SECTION=0, BLOCK=1, TEXT=2, ...；ENTITIES 里的标签是第 9 个。
    assert fit_translated_texts(records, {2: "设备基坑：", 9: "坑底标高"}, _plain_text) == 0
    assert records == before


def test_scan_log_explains_what_was_left_out(tmp_path):
    drawing = _dxf(
        (0, "SECTION"), (2, "ENTITIES"),
        (0, "TEXT"), (1, "门窗表"),
        (0, "TEXT"), (1, "-0.090"),
        (0, "TEXT"), (1, "C1518"),
        (0, "TEXT"), (1, "3100X2100mm"),
        (0, "TEXT"), (1, "APPROVED BY"),
        (0, "ENDSEC"), (0, "EOF"),
    )
    source = tmp_path / "plan.dxf"
    source.write_text(drawing, encoding="utf-8")
    messages = []
    result = CadTranslationPipeline(
        converter=None,
        translator=lambda texts, glossary: {text: "Door and window schedule" for text in texts},
        progress=lambda event: messages.append(event.message),
    ).translate_file(source, tmp_path / "plan_en.dxf", options=CadPipelineOptions(target_lang="en"))
    assert (result.stats.skipped_non_text, result.stats.skipped_codes, result.stats.skipped_target_language) == (1, 2, 3)
    assert "共 5 处文字，不需要翻译的：纯数字和符号 1 处，编号、轴号、型号和尺寸 2 处，已是目标语言 1 处" in messages
