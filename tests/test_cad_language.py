"""CAD 文字语种判定：拉丁字母语言之间不能只看字母，拿不准的交给模型确认。

本机实测：法文图纸（上一次翻译的输出）选「翻译为英文」，扫描显示待翻译 0 处——
以前目标是英文时，凡是只有拉丁字母的文字都算「已是英文」。
"""

import json
from pathlib import Path

import pytest

from core.cad_language import FOREIGN, TARGET, UNSURE, classify_text_language
from core.cad_translation import CadPipelineOptions, CadTranslationPipeline, scan_cad_file


def _dxf(*texts: str) -> str:
    return "".join(f"0\nTEXT\n1\n{text}\n" for text in texts)


@pytest.mark.parametrize(
    ("text", "target", "expected"),
    [
        # 法文标签翻英文：词表或重音字母给出证据，算外文。
        ("PLAN DE MASSE", "en", FOREIGN),
        ("COUPE A-A", "en", FOREIGN),
        ("MUR", "en", FOREIGN),
        ("FENETRE", "en", FOREIGN),
        ("ÉCHELLE 1/100", "en", FOREIGN),
        ("NOTES GENERALES", "en", FOREIGN),
        ("Détail de l'escalier", "en", FOREIGN),
        ("PLANTA BAJA", "en", FOREIGN),
        # 有把握已经是英文。
        ("The steel door", "en", TARGET),
        ("SAME AS ABOVE", "en", TARGET),
        # 型号、规范号、轴号：任何语言都原样保留。
        ("M10", "en", TARGET),
        ("GB/T 700", "en", TARGET),
        ("EL. +3.000", "en", TARGET),
        ("Y", "en", TARGET),
        # 只有多种语言通用的词：程序判断不了，交给模型。
        ("SCALE 1:100", "en", UNSURE),
        ("Bonjour", "fr", UNSURE),
        # 混排中文仍需翻译；书写系统不同的目标语言照旧按文字判断。
        ("Q235钢板", "en", FOREIGN),
        ("Steel door", "fr", FOREIGN),
        ("中文", "zh", TARGET),
        ("Door", "zh", FOREIGN),
        ("钢板 steel plate", "zh", FOREIGN),
        ("Привет", "ru", TARGET),
        ("Bonjour", "x-custom-foo", FOREIGN),
    ],
)
def test_classify_text_language(text, target, expected):
    assert classify_text_language(text, target) == expected


def test_french_drawing_scanned_for_english_is_not_zero(tmp_path: Path):
    drawing = tmp_path / "plan.dxf"
    drawing.write_text(
        _dxf("PLAN DE MASSE", "COUPE A-A", "Mur en béton", "The steel door", "M10", "SCALE 1:100"),
        encoding="utf-8",
    )
    summary = scan_cad_file(drawing, options=CadPipelineOptions(source_lang="auto", target_lang="en"))
    # 三条法文 + 一条拿不准；英文句子和型号不计入。
    assert summary.candidate_count == 4
    assert summary.uncertain_count == 1


def test_uncertain_text_goes_to_model_and_target_language_is_kept(tmp_path: Path):
    source = tmp_path / "plan.dxf"
    source.write_text(_dxf("PLAN DE MASSE", "SCALE 1:100", "Section B", "The steel door"), encoding="utf-8")
    plain_calls: list[list[str]] = []
    checked_calls: list[list[str]] = []

    def translate(texts, glossary):
        plain_calls.append(list(texts))
        return {"PLAN DE MASSE": "SITE PLAN"}

    def translate_with_language(texts, glossary):
        checked_calls.append(list(texts))
        # 模型回报：SCALE 已是英文（保留），Section B 是法文（照译）。
        return {"SCALE 1:100": ("SCALE 1:100", "en"), "Section B": ("SECTION B", "fr")}

    result = CadTranslationPipeline(
        converter=None, translator=translate, language_translator=translate_with_language
    ).translate_file(
        source, tmp_path / "plan_en.dxf", options=CadPipelineOptions(source_lang="auto", target_lang="en")
    )

    assert plain_calls == [["PLAN DE MASSE"]]
    assert checked_calls == [["SCALE 1:100", "Section B"]]
    assert result.stats.skipped_target_language == 1
    assert result.stats.language_checked == 2
    assert result.stats.kept_target_language == 1
    assert result.stats.ai_translated == 2
    assert result.stats.changed == 2
    # 保留原文的那条不算未完成，不让整张图进「需复核」。
    assert result.unresolved == []
    output = (tmp_path / "plan_en.dxf").read_text(encoding="utf-8")
    assert "SITE PLAN" in output and "SECTION B" in output and "SCALE 1:100" in output
    statuses = {item["source"]: item["status"] for item in json.loads(result.manifest.read_text(encoding="utf-8"))["units"]}
    assert statuses["SCALE 1:100"] == "kept"


def test_language_check_failure_is_unresolved(tmp_path: Path):
    source = tmp_path / "plan.dxf"
    source.write_text(_dxf("SCALE 1:100"), encoding="utf-8")
    result = CadTranslationPipeline(
        converter=None,
        translator=lambda texts, glossary: {},
        language_translator=lambda texts, glossary: {},
    ).translate_file(source, tmp_path / "plan_en.dxf", options=CadPipelineOptions(target_lang="en"))
    assert result.stats.kept_target_language == 0
    assert [item["source"] for item in result.unresolved] == ["SCALE 1:100"]


def test_without_language_translator_uncertain_text_is_translated_normally(tmp_path: Path):
    source = tmp_path / "plan.dxf"
    source.write_text(_dxf("SCALE 1:100", "PLAN DE MASSE"), encoding="utf-8")
    calls: list[list[str]] = []

    def translate(texts, glossary):
        calls.append(list(texts))
        return {text: text for text in texts}

    CadTranslationPipeline(converter=None, translator=translate).translate_file(
        source, tmp_path / "plan_en.dxf", options=CadPipelineOptions(target_lang="en")
    )
    assert calls == [["SCALE 1:100", "PLAN DE MASSE"]]


def test_multibyte_escaped_chinese_is_decoded_and_translated(tmp_path: Path):
    """本机实测：电气总说明图 3250 条文字里 2223 条是 \\M+5XXXX 存的 GBK 中文。

    以前不解码，「\\M+5B5A5\\M+5CFF2」看起来是一串 ASCII，目标英文时被当成
    「已是英文」跳过，整张图的中文一处没翻。
    """
    source = tmp_path / "notes.dxf"
    source.write_text(
        "0\nTEXT\n1\n\\M+5B5A5\\M+5CFF2\n"
        "0\nMTEXT\n1\n{\\fSimSun|b0;\\M+5B5A5\\M+5CFF2}\\PMEB\n"
        "0\nTEXT\n1\nE\n",
        encoding="utf-8",
    )
    summary = scan_cad_file(source, options=CadPipelineOptions(target_lang="en"))
    assert summary.candidate_count == 2
    # 目标是中文时，这些已经是中文，不再送去翻译。
    assert scan_cad_file(source, options=CadPipelineOptions(target_lang="zh")).candidate_count == 1

    seen: list[list[str]] = []

    def translate(texts, glossary):
        seen.append(list(texts))
        return {"单向": "One-way"}

    result = CadTranslationPipeline(converter=None, translator=translate).translate_file(
        source, tmp_path / "notes_en.dxf", options=CadPipelineOptions(target_lang="en")
    )
    assert seen == [["单向", "MEB"]]
    output = (tmp_path / "notes_en.dxf").read_text(encoding="utf-8")
    assert "\\M+5B5A5" not in output
    assert output.count("One-way") == 2
    # 格式命令照旧保留。
    assert "{\\fSimSun|b0;One-way}\\PMEB" in output
    assert result.stats.residual_foreign_text_count == 0


def test_undecodable_multibyte_escape_is_left_as_is():
    from core.cad_translation import _plain_text

    assert _plain_text("\\M+5FFFF") == "\\M+5FFFF"
