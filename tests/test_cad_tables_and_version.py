"""CAD：AutoCAD 表格（ACAD_TABLE）的文字照翻并同步单元格原值；DWG 译文按原图或设置的版本保存。"""
from __future__ import annotations

from pathlib import Path

from core.cad_translation import (
    CadPipelineOptions,
    CadTranslationPipeline,
    _parse_dxf,
    dwg_output_version,
    extract_cad_text_units,
    resolve_output_version,
)
from settings import CadOutputSettings
from tests.test_cad_new_features import _drawing, _entities, _field, _text, _translate

TITLE = "{\\fSimSun|b0|i0|c134|p2;材料表}"


def _table_block(name="*T5", *, layer="0"):
    """AutoCAD 写出的表格显示块：每格一段多行文字。"""
    def cell(handle, text, y):
        return (
            (0, "MTEXT"), (5, handle), (330, "2F"), (100, "AcDbEntity"), (8, layer), (100, "AcDbMText"),
            (10, "1"), (20, str(y)), (30, "0"), (40, "2.5"), (41, "38"), (71, "5"), (1, text), (7, "Standard"),
        )
    return (
        (0, "BLOCK"), (5, "2E"), (330, "2F"), (100, "AcDbEntity"), (8, "0"), (100, "AcDbBlockBegin"),
        (2, name), (70, "1"), (10, "0"), (20, "0"), (30, "0"), (3, name), (1, ""),
        *cell("31", "\\pxqc;" + TITLE, -4),
        *cell("32", "墙体拆除图", -12),
        *cell("33", "3100X2100mm", -20),
        (0, "ENDBLK"), (5, "34"), (330, "2F"), (100, "AcDbEntity"), (8, "0"), (100, "AcDbBlockEnd"),
    )


def _table(name="*T5", *, layer="0"):
    """ACAD_TABLE 本体：单元格原值在组码 1 和 CELL_VALUE 的 302 里。"""
    def cell(text):
        return (
            (171, "1"), (172, "0"), (173, "0"), (174, "0"), (175, "1"), (176, "1"), (91, "262177"),
            (178, "0"), (145, "0.0"), (92, "0"), (1, text),
            (301, "CELL_VALUE"), (93, "6"), (90, "4"), (1, text), (94, "0"), (300, ""), (302, text),
            (304, "ACVALUE_END"),
        )
    return (
        (0, "ACAD_TABLE"), (5, "40"), (330, "1F"), (100, "AcDbEntity"), (8, layer),
        (100, "AcDbBlockReference"), (2, name), (10, "0"), (20, "0"), (30, "0"),
        (100, "AcDbTable"), (280, "0"), (342, "3A"), (343, "2F"), (11, "1"), (21, "0"), (31, "0"),
        (90, "22"), (91, "3"), (92, "1"), (93, "0"), (94, "0"), (95, "0"), (96, "0"),
        (141, "8"), (141, "8"), (141, "8"), (142, "40"),
        *cell(TITLE), *cell("墙体拆除图"), *cell("3100X2100mm"),
    )


TRANSLATIONS = {"材料表": "Liste des matériaux", "墙体拆除图": "Plan de démolition des murs"}


def _cells(path: Path) -> list[str]:
    table = _entities(path, "ACAD_TABLE")[0]
    in_table = False
    out = []
    for record in table:
        if record.code == 100:
            in_table = record.value == "AcDbTable"
        elif in_table and record.code in {1, 302}:
            out.append(record.value)
    return out


def test_table_text_is_extracted_even_when_block_text_is_off():
    records = _parse_dxf(_drawing(*_text("20", "天花图"), *_table(), blocks=_table_block()))
    for include_block_text in (True, False):
        texts = {unit.plain_text for unit in extract_cad_text_units(records, include_block_text=include_block_text)}
        assert {"材料表", "墙体拆除图", "天花图"} <= texts


def test_translated_table_updates_display_block_and_cell_values(tmp_path):
    drawing = _drawing(*_table(), blocks=_table_block())
    result, output, messages = _translate(tmp_path, drawing, TRANSLATIONS, translation_font="original")
    shown = [_field(body, 1) for body in _entities(output, "MTEXT")]
    assert "\\pxqc;{\\fSimSun|b0|i0|c134|p2;Liste des matériaux}" in shown
    assert "Plan de démolition des murs" in shown
    cells = _cells(output)
    # 格式代码留着，数字格不动。
    assert cells.count("{\\fSimSun|b0|i0|c134|p2;Liste des matériaux}") == 3
    assert cells.count("Plan de démolition des murs") == 3
    assert cells.count("3100X2100mm") == 3
    assert any("1 个表格的单元格内容已同步为译文" in message for message in messages)


def test_bilingual_table_cells_are_restored_before_translating_again(tmp_path):
    drawing = _drawing(*_table(), blocks=_table_block())
    _, first, _ = _translate(
        tmp_path, drawing, TRANSLATIONS, name="first", display_mode="source_first", translation_font="original",
    )
    expected = "墙体拆除图\\P{\\H0.8x;Plan de démolition des murs}"
    assert _cells(first).count(expected) == 3
    # 拿上次的双语译文图再翻一次：先恢复原文，不会叠出第二份译文。
    _, second, _ = _translate(
        tmp_path, first.read_text(encoding="utf-8"), TRANSLATIONS, name="second",
        display_mode="source_first", translation_font="original",
    )
    assert _cells(second) == _cells(first)


def test_locked_table_is_left_alone(tmp_path):
    drawing = _drawing(*_text("20", "墙体拆除图"), *_table(layer="TK"), blocks=_table_block())
    _, output, _ = _translate(tmp_path, drawing, TRANSLATIONS, locked_layers=("TK",), translation_font="original")
    assert _cells(output).count("墙体拆除图") == 3
    assert _field(_entities(output, "TEXT")[0], 1) == "Plan de démolition des murs"


# ── 输出图纸版本 ──────────────────────────────────────────────


def _dwg(tmp_path, header: str) -> Path:
    path = tmp_path / f"{header}.dwg"
    path.write_bytes(header.encode("ascii") + b"\x00" * 20)
    return path


def test_output_version_follows_the_source_dwg(tmp_path):
    assert dwg_output_version(_dwg(tmp_path, "AC1018")) == "ACAD2004"
    assert dwg_output_version(_dwg(tmp_path, "AC1015")) == "ACAD2000"
    assert dwg_output_version(_dwg(tmp_path, "AC1014")) == "ACAD2000"  # R14 及更早存成 2000
    assert dwg_output_version(_dwg(tmp_path, "AC1032")) is None  # 2018 起就是默认
    assert dwg_output_version(_dwg(tmp_path, "garbag")) is None


def test_output_version_setting_overrides_the_source(tmp_path):
    old = _dwg(tmp_path, "AC1018")
    assert resolve_output_version(old, "source") == "ACAD2004"
    assert resolve_output_version(old, "2010") == "ACAD2010"
    assert resolve_output_version(old, "2018") is None
    assert resolve_output_version(old, "bogus") == "ACAD2004"
    assert CadOutputSettings().output_version == "source"
    assert CadOutputSettings(output_version="1999").output_version == "source"
    assert CadOutputSettings(output_version="2007").output_version == "2007"


class VersionConverter:
    """把任何 DWG 都「转」成同一份 DXF，记下写回时要的版本。"""

    def __init__(self, dxf: str):
        self.dxf = dxf
        self.versions: list[str | None] = []

    def dwg_to_dxf(self, source, destination):
        destination.write_text(self.dxf, encoding="utf-8")
        return destination

    def dxf_to_dwg(self, source, destination, version=None):
        self.versions.append(version)
        destination.write_text(Path(source).read_text(encoding="utf-8"), encoding="utf-8")
        return destination


def test_pipeline_writes_the_dwg_in_the_chosen_version(tmp_path):
    converter = VersionConverter(_drawing(*_text("20", "墙体拆除图")))
    source = _dwg(tmp_path, "AC1018")
    pipeline = CadTranslationPipeline(
        converter=converter,
        translator=lambda texts, glossary: {text: TRANSLATIONS[text] for text in texts if text in TRANSLATIONS},
    )
    pipeline.translate_file(source, tmp_path / "a" / "out.dwg", options=CadPipelineOptions(target_lang="fr"))
    pipeline.translate_file(
        source, tmp_path / "b" / "out.dwg", options=CadPipelineOptions(target_lang="fr", output_version="2010"),
    )
    pipeline.translate_file(
        source, tmp_path / "c" / "out.dwg", options=CadPipelineOptions(target_lang="fr", output_version="2018"),
    )
    assert converter.versions == ["ACAD2004", "ACAD2010", None]


def test_paragraph_codes_stay_out_of_the_text_and_survive_write_back(tmp_path):
    # 表格单元格和带缩进、编号的说明常以 \pxqc; \pi-3,l3; 开头：参数不能当成文字送去翻译，
    # 写回时也不能被译文盖掉。
    mtext = (
        (0, "MTEXT"), (5, "21"), (330, "1F"), (100, "AcDbEntity"), (8, "0"), (100, "AcDbMText"),
        (10, "0"), (20, "0"), (30, "0"), (40, "2.5"), (41, "60"), (71, "1"), (1, "\\pi-3,l3;注：说明"),
        (7, "Standard"),
    )
    records = _parse_dxf(_drawing(*mtext))
    assert [unit.plain_text for unit in extract_cad_text_units(records)] == ["注：说明"]
    _, output, _ = _translate(tmp_path, _drawing(*mtext), {"注：说明": "Note : description"}, translation_font="original")
    assert _field(_entities(output, "MTEXT")[0], 1) == "\\pi-3,l3;Note : description"


def test_task_option_reaches_the_pipeline():
    from api.surfaces.cad import cad_layout_options
    from api.task_contracts import TaskOptions

    assert cad_layout_options(TaskOptions(cad_output_version="2007"))["output_version"] == "2007"
    assert cad_layout_options(TaskOptions(cad_output_version="1999"))["output_version"] == "source"
    assert cad_layout_options(TaskOptions())["output_version"] == "source"
