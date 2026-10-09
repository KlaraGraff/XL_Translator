import hashlib
import json
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
    assert no_converter.needs_conversion is True and no_converter.candidate_count == 0
    direct = scan_cad_file(dwg, converter=FakeConverter())
    assert direct.needs_conversion is False and direct.candidate_count == 1
    summaries = scan_cad_paths([tmp_path], converter=FakeConverter())
    assert [item.filename for item in summaries] == ["a.dxf", "b.dwg"]


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
    assert result.unresolved[0]["reason"] == "numeric token drift"


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
    assert _is_likely_target_language("Bonjour", "fr")
    assert _is_likely_target_language("Über", "de")
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
