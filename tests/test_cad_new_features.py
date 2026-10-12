"""CAD：字号缩放档位、锁定图块/图层、双语显示、译文兼容字体、DWG 转换缓存。"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.cad_cache import CadDxfCache
from core.cad_layout import FitSettings, _pick_scale
from core.cad_translation import (
    CadPipelineOptions,
    CadTranslationPipeline,
    _parse_dxf,
    extract_cad_text_units,
    inspect_cad_file,
    list_cad_text_containers,
    parse_lock_patterns,
    scan_cad_file,
)
from core.cad_writeback import APP_NAME, TRANSLATION_STYLE


def _dxf(*pairs):
    return "".join(f"{code}\n{value}\n" for code, value in pairs)


def _drawing(*entities, blocks=()):
    """R2018 式 DXF：带文件头、样式表、应用名表、句柄。"""
    return _dxf(
        (0, "SECTION"), (2, "HEADER"),
        (9, "$ACADVER"), (1, "AC1032"),
        (9, "$HANDSEED"), (5, "200"),
        (0, "ENDSEC"),
        (0, "SECTION"), (2, "TABLES"),
        (0, "TABLE"), (2, "STYLE"), (5, "3"), (330, "0"), (100, "AcDbSymbolTable"), (70, "1"),
        (0, "STYLE"), (5, "11"), (330, "3"), (100, "AcDbSymbolTableRecord"), (100, "AcDbTextStyleTableRecord"),
        (2, "Standard"), (70, "0"), (40, "0.0"), (41, "1.0"), (50, "0.0"), (71, "0"), (42, "2.5"),
        (3, "tssdeng.shx"), (4, "hztxt.shx"),
        (1001, "ACAD"), (1000, "tssdeng"),
        (0, "ENDTAB"),
        (0, "TABLE"), (2, "APPID"), (5, "9"), (330, "0"), (100, "AcDbSymbolTable"), (70, "1"),
        (0, "APPID"), (5, "12"), (330, "9"), (100, "AcDbSymbolTableRecord"), (100, "AcDbRegAppTableRecord"),
        (2, "ACAD"), (70, "0"),
        (0, "ENDTAB"),
        (0, "ENDSEC"),
        (0, "SECTION"), (2, "BLOCKS"), *blocks, (0, "ENDSEC"),
        (0, "SECTION"), (2, "ENTITIES"), *entities, (0, "ENDSEC"),
        (0, "EOF"),
    )


def _text(handle, text, *, x=0, y=0, height=2.5, layer="0", extra=()):
    return (
        (0, "TEXT"), (5, handle), (330, "1F"), (100, "AcDbEntity"), (8, layer),
        (100, "AcDbText"), (10, str(x)), (20, str(y)), (30, "0"), (40, str(height)), (1, text),
        (7, "Standard"), *extra, (100, "AcDbText"),
    )


def _translate(tmp_path, drawing, translations, name="plan", **options):
    source = tmp_path / f"{name}.dxf"
    source.write_text(drawing, encoding="utf-8")
    output = tmp_path / f"{name}_out.dxf"
    messages = []
    result = CadTranslationPipeline(
        converter=None,
        translator=lambda texts, glossary: {text: translations[text] for text in texts if text in translations},
        progress=lambda event: messages.append(event.message),
    ).translate_file(source, output, options=CadPipelineOptions(target_lang=options.pop("target_lang", "fr"), **options))
    return result, output, messages


def _entities(path, kind):
    records = _parse_dxf(path.read_text(encoding="utf-8"))
    out = []
    current = None
    for record in records:
        if record.code == 0:
            current = [] if record.value == kind else None
            if current is not None:
                out.append(current)
        elif current is not None:
            current.append(record)
    return out


def _field(body, code, default=None):
    return next((record.value for record in body if record.code == code), default)


# ── 字号缩放档位 ──────────────────────────────────────────────


def test_fit_tiers_start_at_the_default_and_step_down_to_the_minimum():
    assert FitSettings(0.7, 0.4).tiers() == [0.7, 0.6, 0.5, 0.4]
    assert FitSettings(0.75, 0.5).tiers() == [0.75, 0.65, 0.55, 0.5]
    # 最小比例不能大于默认比例；超出范围的值收回范围内。
    assert FitSettings(0.6, 0.9).normalized() == FitSettings(0.6, 0.6)
    assert FitSettings(2, 0.1).normalized() == FitSettings(1.0, 0.3)


def test_overflow_shrinks_to_the_default_first_even_when_less_would_do():
    settings = FitSettings(0.7, 0.4)
    assert _pick_scale(0.95, settings) == (0.7, True)
    assert _pick_scale(0.55, settings) == (0.5, True)
    assert _pick_scale(0.2, settings) == (0.4, False)


def test_text_that_cannot_fit_at_the_minimum_is_reported(tmp_path):
    drawing = _drawing(
        *_text("20", "坑", x=0),
        *_text("21", "-0.090", x=3),
    )
    result, output, messages = _translate(
        tmp_path, drawing, {"坑": "Pit bottom elevation of the equipment"}, fit_min_scale=0.5,
    )
    assert result.stats.layout_fitted == 1 and result.stats.layout_unresolved == 1
    label = _entities(output, "TEXT")[0]
    # 停在最小比例：字宽 0.7、字高 0.5/0.7 倍。
    assert float(_field(label, 41)) == pytest.approx(0.7)
    assert float(_field(label, 40)) == pytest.approx(2.5 * 0.5 / 0.7)
    assert any("缩到最小比例仍会压到旁边的文字" in message for message in messages)
    report = (tmp_path / "plan_out.dxf.report.json").read_text(encoding="utf-8")
    assert "Pit bottom elevation of the equipment" in report


# ── 锁定图块 / 图层 ──────────────────────────────────────────


def test_lock_patterns_accept_common_separators_and_wildcards():
    assert parse_lock_patterns("TKA1/TKA3/图框A0/") == ("TKA1", "TKA3", "图框A0")
    assert parse_lock_patterns("tk*，签名栏\nTK*；") == ("TK*", "签名栏")
    assert parse_lock_patterns("") == ()


def _framed_drawing():
    frame = (
        (0, "BLOCK"), (5, "30"), (8, "0"), (2, "TK-A1"), (70, "2"),
        *_text("31", "设计单位"),
        (0, "INSERT"), (5, "32"), (8, "0"), (2, "LOGO"),
        (0, "ATTDEF"), (5, "33"), (8, "0"), (1, "图名"), (3, "图名"), (2, "TITLE"), (70, "0"),
        (0, "ENDBLK"), (5, "34"),
        (0, "BLOCK"), (5, "35"), (8, "0"), (2, "LOGO"), (70, "0"),
        *_text("36", "公司标志"),
        (0, "ENDBLK"), (5, "37"),
        (0, "BLOCK"), (5, "38"), (8, "0"), (2, "DOOR"), (70, "0"),
        *_text("39", "防火门"),
        (0, "ENDBLK"), (5, "3A"),
    )
    entities = (
        (0, "INSERT"), (5, "40"), (8, "图框"), (2, "TK-A1"), (66, "1"),
        (0, "ATTRIB"), (5, "41"), (8, "图框"), (1, "一层平面图"), (2, "TITLE"), (70, "0"),
        (0, "SEQEND"), (5, "42"),
        (0, "INSERT"), (5, "43"), (8, "门窗"), (2, "DOOR"),
        *_text("44", "设备基础", layer="说明"),
        *_text("45", "会签栏", layer="签字"),
    )
    return _drawing(*entities, blocks=frame)


def test_locked_block_keeps_its_text_attributes_and_nested_blocks():
    records = _parse_dxf(_framed_drawing())
    every = {unit.plain_text for unit in extract_cad_text_units(records)}
    assert {"设计单位", "公司标志", "图名", "一层平面图", "防火门", "设备基础", "会签栏"} <= every
    from core.cad_translation import CadLockReport

    report = CadLockReport()
    kept = {
        unit.plain_text
        for unit in extract_cad_text_units(
            records, locked_blocks=parse_lock_patterns("tk*, 不存在的块"), locked_layers=parse_lock_patterns("签字"),
            lock_report=report,
        )
    }
    assert kept == {"防火门", "设备基础"}
    assert report.locked_texts == 5
    assert report.block_matches == {"TK*": 1, "不存在的块": 0}
    assert report.layer_matches == {"签字": 1}


def test_block_contents_on_layer_zero_follow_the_insert_layer():
    records = _parse_dxf(_framed_drawing())
    kept = {unit.plain_text for unit in extract_cad_text_units(records, locked_layers=("门窗",))}
    assert "防火门" not in kept and "设备基础" in kept


def test_scan_counts_leave_out_locked_text_and_translation_warns_about_unmatched_names(tmp_path):
    source = tmp_path / "framed.dxf"
    source.write_text(_framed_drawing(), encoding="utf-8")
    plain = scan_cad_file(source, options=CadPipelineOptions(target_lang="fr"))
    locked = scan_cad_file(source, options=CadPipelineOptions(target_lang="fr", locked_blocks=("TK*",)))
    assert plain.text_entity_count - locked.text_entity_count == 4

    translations = {"防火门": "Porte coupe-feu", "设备基础": "Socle", "会签栏": "Visas"}
    result, output, messages = _translate(
        tmp_path, _framed_drawing(), translations, name="framed", locked_blocks=("TK*", "TKA9"),
    )
    assert result.stats.locked_texts == 4
    assert any("锁定不翻译的图块和图层里有 4 处文字" in message for message in messages)
    assert any("没有找到这些图块：TKA9" in message for message in messages)
    written = output.read_text(encoding="utf-8")
    assert "设计单位" in written and "一层平面图" in written and "Porte coupe-feu" in written


def test_text_containers_list_named_blocks_with_text_and_layers():
    containers = list_cad_text_containers(_parse_dxf(_framed_drawing()))
    names = {item["name"]: item for item in containers["blocks"]}
    assert names["TK-A1"] == {"name": "TK-A1", "texts": 2, "inserts": 1}
    assert names["DOOR"]["texts"] == 1 and "LOGO" in names
    layers = {item["name"]: item["texts"] for item in containers["layers"]}
    assert layers["说明"] == 1 and layers["签字"] == 1


# ── 双语显示 ────────────────────────────────────────────────


def _bilingual_drawing():
    return _drawing(
        *_text("20", "设备基坑", x=10, y=100, height=5),
        (0, "MTEXT"), (5, "21"), (330, "1F"), (100, "AcDbEntity"), (8, "说明"), (100, "AcDbMText"),
        (10, "0"), (20, "50"), (30, "0"), (40, "3"), (41, "100"), (71, "1"), (1, "{\\fSimSun|b0;施工说明}"),
        (0, "INSERT"), (5, "22"), (8, "0"), (2, "TAG"), (66, "1"),
        (0, "ATTRIB"), (5, "23"), (8, "0"), (10, "0"), (20, "0"), (40, "2"), (1, "一层"), (2, "T"), (70, "0"),
        (0, "SEQEND"), (5, "24"),
        blocks=((0, "BLOCK"), (5, "25"), (8, "0"), (2, "TAG"), (70, "2"), (0, "ENDBLK"), (5, "26")),
    )


_BILINGUAL = {"设备基坑": "Fosse d'équipement", "施工说明": "Notes de construction", "一层": "RDC"}


def test_source_first_adds_a_smaller_translation_line_below_single_line_text(tmp_path):
    result, output, _messages = _translate(tmp_path, _bilingual_drawing(), _BILINGUAL, display_mode="source_first")
    assert result.stats.bilingual_added == 1
    original, added = _entities(output, "TEXT")
    assert _field(original, 1) == "设备基坑" and _field(original, 5) == "20"
    assert _field(added, 1) == "Fosse d'équipement"
    assert float(_field(added, 40)) == pytest.approx(4.0)
    # 同一图层，新句柄，放在原文下方：原文底边 100，空 0.4×5，再放 4 高的译文。
    assert _field(added, 8) == "0"
    assert _field(added, 5) not in {"20", "21", "22", "23", "24"}
    assert float(_field(added, 20)) == pytest.approx(100 - 2 - 4)
    assert float(_field(added, 10)) == pytest.approx(10)
    # 文件头的句柄种子推到新句柄之后。
    text = output.read_text(encoding="utf-8")
    seed = int(text.split("$HANDSEED\n  5\n" if "$HANDSEED\n  5\n" in text else "$HANDSEED\n5\n")[1].split("\n")[0], 16)
    assert seed > int(_field(added, 5), 16)
    mtext = _entities(output, "MTEXT")[0]
    assert _field(mtext, 1) == "{\\fSimSun|b0;施工说明}\\P{\\H0.8x;{\\fSimSun|b0;Notes de construction}}"
    attrib = _entities(output, "ATTRIB")[0]
    assert _field(attrib, 1) == "一层 / RDC"
    # 应用名登记过，改成双语的实体带标记。
    assert APP_NAME in [_field(body, 2) for body in _entities(output, "APPID")]
    assert ("1001", APP_NAME) in [(str(r.code), r.value) for r in added]


def test_translation_first_puts_the_translation_above_and_first(tmp_path):
    _result, output, _messages = _translate(tmp_path, _bilingual_drawing(), _BILINGUAL, display_mode="translation_first")
    _original, added = _entities(output, "TEXT")
    assert float(_field(added, 20)) == pytest.approx(100 + 5 + 2)
    assert _field(_entities(output, "ATTRIB")[0], 1) == "RDC / 一层"
    assert _field(_entities(output, "MTEXT")[0], 1).startswith("{\\H0.8x;")


def test_translating_a_bilingual_output_again_replaces_the_old_translation(tmp_path):
    _result, first, _messages = _translate(tmp_path, _bilingual_drawing(), _BILINGUAL, display_mode="source_first")
    again = {"设备基坑": "Fosse", "施工说明": "Notes", "一层": "Rez"}
    result, second, messages = _translate(
        tmp_path, first.read_text(encoding="utf-8"), again, name="again", display_mode="source_first",
    )
    assert any("上次写入的双语译文" in message for message in messages)
    texts = [_field(body, 1) for body in _entities(second, "TEXT")]
    assert texts == ["设备基坑", "Fosse"]
    assert _field(_entities(second, "ATTRIB")[0], 1) == "一层 / Rez"
    assert result.stats.bilingual_added == 1


# ── 译文兼容字体 ────────────────────────────────────────────


def test_compatible_font_adds_a_stock_style_and_points_translations_at_it(tmp_path):
    drawing = _drawing(
        *_text("20", "设备基坑", x=0),
        *_text("21", "共 3 处", x=0, y=50),
        (0, "MTEXT"), (5, "22"), (330, "1F"), (100, "AcDbEntity"), (8, "0"), (100, "AcDbMText"),
        (10, "0"), (20, "20"), (30, "0"), (40, "3"), (1, "{\\Ftssdeng.shx;\\fArial|b1;说明}"), (7, "Standard"),
    )
    translations = {"设备基坑": "Fosse d’équipement – œuvre", "说明": "Note"}
    result, output, _messages = _translate(tmp_path, drawing, translations, translation_font="compatible")
    styles = {_field(body, 2): body for body in _entities(output, "STYLE")}
    style = styles[TRANSLATION_STYLE]
    assert (_field(style, 3), _field(style, 4), _field(style, 1001)) == ("simplex.shx", "", None)
    assert _field(style, 5) not in {"3", "11", "12"}
    translated, untouched = _entities(output, "TEXT")
    # 弯引号、长破折号、œ 换成 SHX 字体一定有的写法；没翻的文字不换样式。
    assert _field(translated, 1) == "Fosse d'équipement - oeuvre"
    assert _field(translated, 7) == TRANSLATION_STYLE and _field(untouched, 7) == "Standard"
    mtext = _entities(output, "MTEXT")[0]
    assert _field(mtext, 7) == TRANSLATION_STYLE
    assert _field(mtext, 1) == "{\\fArial|b1;Note}"
    assert result.stats.restyled == 2
    # 报告里仍是模型给的原样。
    assert "œuvre" in (tmp_path / "plan_out.dxf.report.json").read_text(encoding="utf-8")


def test_chinese_target_uses_the_gb_fonts_and_original_font_option_changes_nothing(tmp_path):
    drawing = _drawing(*_text("20", "Equipment pit"))
    _result, output, _messages = _translate(
        tmp_path, drawing, {"Equipment pit": "设备基坑"}, target_lang="zh", translation_font="compatible",
    )
    style = {_field(body, 2): body for body in _entities(output, "STYLE")}[TRANSLATION_STYLE]
    assert (_field(style, 3), _field(style, 4)) == ("gbenor.shx", "gbcbig.shx")

    _result, kept, _messages = _translate(
        tmp_path, drawing, {"Equipment pit": "设备基坑"}, name="kept", target_lang="zh", translation_font="original",
    )
    assert TRANSLATION_STYLE not in kept.read_text(encoding="utf-8")


# ── DWG 转换缓存与逐张统计 ───────────────────────────────────


class CountingConverter:
    def __init__(self):
        self.calls = 0

    def dwg_to_dxf(self, source, destination):
        self.calls += 1
        destination.write_text(Path(source).read_text(encoding="utf-8"), encoding="utf-8")
        return destination

    def dxf_to_dwg(self, source, destination):
        destination.write_text(Path(source).read_text(encoding="utf-8"), encoding="utf-8")
        return destination


def test_inspecting_a_dwg_caches_the_conversion_for_the_task(tmp_path):
    source = tmp_path / "plan.dwg"
    source.write_text(_framed_drawing(), encoding="utf-8")
    cache = CadDxfCache(tmp_path / "cache")
    converter = CountingConverter()
    inspection = inspect_cad_file(source, converter=converter, cache=cache, options=CadPipelineOptions(target_lang="fr"))
    assert inspection.converted and converter.calls == 1
    assert inspection.summary.text_entity_count == 7 and not inspection.summary.counts_pending
    assert {item["name"] for item in inspection.blocks} == {"TK-A1", "LOGO", "DOOR"}
    again = inspect_cad_file(source, converter=converter, cache=cache, options=CadPipelineOptions(target_lang="fr"))
    assert not again.converted and converter.calls == 1

    messages = []
    CadTranslationPipeline(
        converter=converter,
        translator=lambda texts, glossary: {text: "FR" for text in texts},
        progress=lambda event: messages.append(event.message),
        dxf_cache=cache,
    ).translate_file(source, tmp_path / "out" / "plan_fr.dwg", options=CadPipelineOptions(target_lang="fr"))
    assert converter.calls == 2  # 只有读回校验那一次，扫描时转好的直接复用
    assert "使用扫描时已转换好的图纸，跳过格式转换" in messages


def test_cache_misses_after_the_drawing_changes_and_prunes_to_its_size_limit(tmp_path):
    source = tmp_path / "plan.dwg"
    source.write_text("one", encoding="utf-8")
    dxf = tmp_path / "plan.dxf"
    dxf.write_text("x" * 100, encoding="utf-8")
    cache = CadDxfCache(tmp_path / "cache", max_bytes=150)
    assert cache.store(source, dxf) is not None and cache.lookup(source) is not None
    source.write_text("two!", encoding="utf-8")
    assert cache.lookup(source) is None
    cache.store(source, dxf)
    assert len(list((tmp_path / "cache").glob("*.dxf"))) == 1


def test_scan_counts_a_dwg_from_the_cache_and_lists_others_as_pending(tmp_path):
    from core.cad_translation import scan_cad_paths

    counted = tmp_path / "counted.dwg"
    counted.write_text(_framed_drawing(), encoding="utf-8")
    pending = tmp_path / "pending.dwg"
    pending.write_text(_framed_drawing(), encoding="utf-8")
    cache = CadDxfCache(tmp_path / "cache")
    inspect_cad_file(counted, converter=CountingConverter(), cache=cache, options=CadPipelineOptions(target_lang="fr"))
    summaries = {
        item.filename: item
        for item in scan_cad_paths([tmp_path], converter_available=True, options=CadPipelineOptions(target_lang="fr"), cache=cache)
    }
    assert summaries["counted.dwg"].text_entity_count == 7 and not summaries["counted.dwg"].counts_pending
    assert summaries["counted.dwg"].format == "dwg"
    assert summaries["pending.dwg"].counts_pending
