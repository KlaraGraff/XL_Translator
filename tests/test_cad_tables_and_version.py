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
from tests.test_cad_new_features import _drawing, _dxf, _entities, _field, _text, _translate

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


def _with_content(drawing: str, *, link: str = "owner") -> str:
    """AutoCAD 2008 起的新式表格：单元格内容另存在 OBJECTS 段的 TABLECONTENT 里。

    ``link="owner"``：内容对象以表格为所属；``"pointer"``：表格用 340 指向它。
    """
    def cell(text):
        return (
            (90, "1"), (300, "CELL"), (91, "0"), (300, "CONTENT"), (90, "1"),
            (300, "VALUE"), (93, "6"), (90, "4"), (1, text), (94, "0"), (300, ""), (302, text), (304, "ACVALUE_END"),
            (309, "CONTENTEND"),
        )
    content = _dxf(
        (0, "SECTION"), (2, "OBJECTS"),
        (0, "DICTIONARY"), (5, "C"), (330, "0"), (100, "AcDbDictionary"), (3, "ACAD_GROUP"), (350, "D"),
        (0, "TABLECONTENT"), (5, "60"), (330, "40" if link == "owner" else "C"),
        (100, "AcDbLinkedData"), (1, ""), (300, ""),
        (100, "AcDbLinkedTableData"), (90, "1"), (90, "3"),
        *cell(TITLE), *cell("墙体拆除图"), *cell("3100X2100mm"),
        (100, "AcDbFormattedTableData"), (100, "AcDbTableContent"),
        (0, "ENDSEC"),
    )
    if link == "pointer":
        drawing = drawing.replace("100\nAcDbTable\n280\n0\n", "100\nAcDbTable\n280\n0\n340\n60\n", 1)
    return drawing.replace("0\nEOF\n", content + "0\nEOF\n")


def _content_cells(path: Path) -> list[str]:
    return [
        record.value for record in _entities(path, "TABLECONTENT")[0] if record.code in {1, 302} and record.value
    ]


def test_new_style_table_content_is_translated_too(tmp_path):
    for link in ("owner", "pointer"):
        drawing = _with_content(_drawing(*_table(), blocks=_table_block()), link=link)
        _, output, messages = _translate(tmp_path, drawing, TRANSLATIONS, name=link, translation_font="original")
        cells = _content_cells(output)
        assert cells.count("Plan de démolition des murs") == 2, (link, cells)
        assert cells.count("{\\fSimSun|b0|i0|c134|p2;Liste des matériaux}") == 2
        assert "墙体拆除图" not in cells and cells.count("3100X2100mm") == 2
        assert any("1 个表格的单元格内容已同步为译文" in message for message in messages)


def test_bilingual_rerun_restores_cells_by_value_even_after_the_records_moved(tmp_path):
    drawing = _with_content(_drawing(*_table(), blocks=_table_block()))
    _, first, _ = _translate(tmp_path, drawing, TRANSLATIONS, name="first", display_mode="source_first", translation_font="original")
    expected = "墙体拆除图\\P{\\H0.8x;Plan de démolition des murs}"
    assert _content_cells(first).count(expected) == 2
    # 存成 DWG 再转回来时，转换工具会重排表格里的组码：标记按位置记就会找错格子。
    moved = first.read_text(encoding="utf-8").replace(
        "100\nAcDbTable\n280\n0\n", "100\nAcDbTable\n280\n0\n94\n0\n95\n0\n", 1,
    )
    _, second, _ = _translate(tmp_path, moved, TRANSLATIONS, name="second", display_mode="source_first", translation_font="original")
    assert _cells(second) == _cells(first)
    assert _content_cells(second) == _content_cells(first)


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


def test_fit_scales_default_by_target_language_when_not_sent():
    """界面总会按目标语言传比例；没传时（直接调接口、旧任务记录）后端也按目标语言取默认值。"""
    from api.surfaces.cad import cad_layout_options
    from api.task_contracts import TaskOptions

    def scales(options, lang):
        layout = cad_layout_options(options, lang)
        return layout["fit_default_scale"], layout["fit_min_scale"]

    assert scales(TaskOptions(), "zh") == (1.0, 0.4)
    assert scales(TaskOptions(), "zh-TW") == (1.0, 0.4)
    assert scales(TaskOptions(), "fr") == (0.7, 0.4)
    assert scales(TaskOptions(), "") == (0.7, 0.4)
    # 传了的照用；只传一项时另一项取该语言的默认值。
    assert scales(TaskOptions(cad_fit_default_scale=0.8, cad_fit_min_scale=0.5), "zh") == (0.8, 0.5)
    assert scales(TaskOptions(cad_fit_min_scale=0.6), "en") == (0.7, 0.6)
    assert scales(TaskOptions(cad_fit_default_scale=0.9), "zh") == (0.9, 0.4)


def test_underlined_latin_text_is_not_swallowed_by_the_format_code(tmp_path):
    # 带下划线的标题：译成法文后 \L 紧跟单词，读回时整段被当成格式代码删掉，
    # 校验数出来少一条，「仅译文」整张图被拦下不出图。
    from core.cad_translation import _mtext_runs, _plain_text

    assert _plain_text("\\LPlan de démolition\\l") == "Plan de démolition"
    assert _plain_text("{\\OAbc\\o; et \\KXyz\\k}") == "Abc; et Xyz"
    assert [run.text for run in _mtext_runs("{\\LPlan}\\P\\OCoupe")] == ["Plan", "Coupe"]
    mtext = (
        (0, "MTEXT"), (5, "21"), (330, "1F"), (100, "AcDbEntity"), (8, "0"), (100, "AcDbMText"),
        (10, "0"), (20, "0"), (30, "0"), (40, "2.5"), (41, "60"), (71, "1"), (1, "{\\L墙体拆除图}"), (7, "Standard"),
    )
    result, output, _ = _translate(tmp_path, _drawing(*mtext), TRANSLATIONS, translation_font="original")
    assert _field(_entities(output, "MTEXT")[0], 1) == "{\\LPlan de démolition des murs}"
    records = _parse_dxf(output.read_text(encoding="utf-8"))
    assert [unit.plain_text for unit in extract_cad_text_units(records)] == ["Plan de démolition des murs"]


def test_text_split_by_an_inline_font_change_is_translated_as_one_phrase(tmp_path):
    # 「平面图」中间夹着换字体的代码：以前拆成「平面」「图」分别翻译，拼回成「Vue en planDessin」。
    from core.cad_translation import _mtext_runs

    assert [run.text for run in _mtext_runs("平面{\\fSimSun|b0|i0|c134|p2;图}")] == ["平面图"]
    assert [run.text for run in _mtext_runs("{\\fA;平面}{\\fB;图}\\P说明")] == ["平面图", "说明"]
    assert [run.text for run in _mtext_runs("Plan {\\Ldetail}")] == ["Plan detail"]
    mtext = (
        (0, "MTEXT"), (5, "21"), (330, "1F"), (100, "AcDbEntity"), (8, "0"), (100, "AcDbMText"),
        (10, "0"), (20, "0"), (30, "0"), (40, "2.5"), (41, "60"), (71, "1"),
        (1, "{\\fSimSun|b0;平面}{\\fSimHei|b0;图}\\P说明"), (7, "Standard"),
    )
    _, output, _ = _translate(
        tmp_path, _drawing(*mtext), {"平面图": "Vue en plan", "说明": "Note", "平面": "X", "图": "Y"},
        translation_font="original",
    )
    assert _field(_entities(output, "MTEXT")[0], 1) == "{\\fSimSun|b0;Vue en plan}{\\fSimHei|b0;}\\PNote"
