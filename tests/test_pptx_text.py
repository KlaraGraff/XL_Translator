"""PPT reading and writing (``core/pptx_text.py``) on the fixtures in ``tests/fixtures/pptx``.

``mixed.pptx`` (built by ``build_fixtures.py``) has a title with a line
break, bullets with a bold lead and a link, notes, a table, a page-number
field, a group, a picture, paragraph animations, an auto-numbered list, a
vertical text box, a shape inside ``mc:AlternateContent``, a comment and
fixed text on the master and a layout.
"""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from lxml import etree

from core.pptx_text import (
    REASON_VERTICAL,
    PptxError,
    Translation,
    extract_units,
    lang_tag,
    scan_file,
    write_file,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "pptx"
MIXED = FIXTURES / "mixed.pptx"
NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
}

FRENCH = {
    "2026 年度项目进展汇报\n第三季度": "Rapport d’avancement 2026\nTroisième trimestre",
    "注意：所有图纸以最新版本为准。": "Attention : seuls les plans à jour font foi.",
    "详细资料见项目网站。": "Détails sur le site du projet.",
    "第 3 页": "Page 3",
}
FRENCH_MARKED = {
    "注意：所有图纸以最新版本为准。": "<g1>Attention :</g1> seuls les plans à jour font foi.",
    "详细资料见项目网站。": "Détails sur le <g1>site du projet</g1>.",
    "第 3 页": "Page <g1>3</g1>",
}


def _french(unit) -> Translation:
    return Translation(FRENCH.get(unit.text, f"FR {unit.text}"), FRENCH_MARKED.get(unit.text, ""))


def _write(tmp_path: Path, *, bilingual: bool = False, source: Path = MIXED, marked: bool = True, **kwargs):
    units = extract_units(source)
    translations = {
        unit.id: _french(unit) if marked else Translation(_french(unit).text) for unit in units
    }
    out = tmp_path / ("bi.pptx" if bilingual else "only.pptx")
    return write_file(source, out, translations, bilingual=bilingual, target_lang="fr", **kwargs), out


def _xml(path: Path, part: str):
    with zipfile.ZipFile(path) as package:
        return etree.fromstring(package.read(part))


def _paragraphs(root) -> list:
    return list(root.iter(f"{{{NS['a']}}}p"))


def _text(paragraph) -> str:
    parts = []
    for child in paragraph:
        if child.tag == f"{{{NS['a']}}}br":
            parts.append("\n")
        elif child.tag in (f"{{{NS['a']}}}r", f"{{{NS['a']}}}fld"):
            parts.append("".join(t.text or "" for t in child.iter(f"{{{NS['a']}}}t")))
    return "".join(parts)


def _find(root, text: str):
    return next(paragraph for paragraph in _paragraphs(root) if _text(paragraph).strip() == text)


def _rewrite_part(source: Path, target: Path, part: str, edit) -> None:
    with zipfile.ZipFile(source) as before, zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as after:
        for info in before.infolist():
            data = before.read(info.filename)
            if info.filename == part:
                data = edit(data)
            after.writestr(info, data)


# ── reading ────────────────────────────────────────────────────────────────


def test_units_follow_the_slide_order_notes_then_masters() -> None:
    units = extract_units(MIXED)
    contexts = [unit.context for unit in units]
    assert contexts[0] == "第 1 页 · 标题"
    assert units[0].text == "2026 年度项目进展汇报\n第三季度"
    assert units[0].kind == "heading"
    # Notes come right after their slide, each paragraph its own unit.
    notes = [unit for unit in units if unit.kind == "notes"]
    assert [unit.context for unit in notes] == ["第 2 页备注 · 第 1 段", "第 2 页备注 · 第 2 段"]
    assert contexts.index(notes[0].context) == contexts.index("第 3 页 · 标题") - 2
    # Fixed text on the master and a layout, never the placeholders' prompts.
    assert units[-2].context == "母版 1 · 文本框 100" and units[-2].text == "内部资料 请勿外传"
    assert units[-1].context == "版式「Title and Content」 · 文本框 100"
    assert not any("单击" in unit.text or "Click to" in unit.text for unit in units)
    # Numbers alone ("1", "2" in the table) are not sent.
    assert "1" not in [unit.text for unit in units]
    assert len({unit.id for unit in units}) == len(units)


def test_slides_are_read_in_presentation_order_not_file_order(tmp_path: Path) -> None:
    swapped = tmp_path / "swapped.pptx"

    def swap(data: bytes) -> bytes:
        root = etree.fromstring(data)
        listing = root.find("p:sldIdLst", NS)
        items = list(listing)
        listing.remove(items[0])
        listing.append(items[0])
        return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)

    _rewrite_part(MIXED, swapped, "ppt/presentation.xml", swap)
    units = extract_units(swapped)
    assert units[0].context == "第 1 页 · 标题" and units[0].text == "项目概况"
    assert any(unit.context == "第 7 页 · 标题" and unit.text.startswith("2026") for unit in units)


def test_notes_can_be_left_out() -> None:
    assert not any(unit.kind == "notes" for unit in extract_units(MIXED, translate_notes=False))
    assert scan_file(MIXED, translate_notes=False).unit_count == len(extract_units(MIXED)) - 2


def test_inline_formats_fields_tables_and_groups() -> None:
    units = {unit.text: unit for unit in extract_units(MIXED)}
    assert units["注意：所有图纸以最新版本为准。"].marked == "<g1>注意：</g1>所有图纸以最新版本为准。"
    assert units["详细资料见项目网站。"].marked == "详细资料见<g1>项目网站</g1>。"
    assert units["本项目位于卡萨布兰卡，总建筑面积 12 万平方米。"].marked == ""
    page = units["第 3 页"]
    assert page.marked == "第 <g1>3</g1> 页" and page.protected == ["3"]
    assert units["钢结构安装"].context == "第 3 页 · Table 2 · 第 3 行第 2 列"
    assert units["钢结构安装"].kind == "cell"
    assert units["现场布置"].context == "第 4 页 · TextBox 3"  # inside a group
    assert units["兼容内容里的文字"].context == "第 7 页 · TextBox 2"  # mc:Choice, read once


def test_scan_counts_what_is_not_translated() -> None:
    result = scan_file(MIXED)
    assert result.unit_count == len(extract_units(MIXED))
    assert result.warnings == ["1 张图片中的文字未翻译", "1 条批注未翻译"]


def test_unreadable_files_get_one_sentence(tmp_path: Path) -> None:
    legacy = tmp_path / "old.pptx"
    legacy.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 512)
    with pytest.raises(PptxError, match="已加密，或实际是旧版 .ppt 格式"):
        extract_units(legacy)
    broken = tmp_path / "broken.pptx"
    broken.write_bytes(b"not a zip")
    with pytest.raises(PptxError, match="已损坏或不是 PowerPoint 文件"):
        scan_file(broken)


def test_paragraphs_with_unknown_content_are_left_alone(tmp_path: Path) -> None:
    patched = tmp_path / "math.pptx"
    math = b'<a14:m xmlns:a14="http://schemas.microsoft.com/office/drawing/2010/main"/>'
    _rewrite_part(
        MIXED,
        patched,
        "ppt/slides/slide4.xml",
        lambda data: data.replace("<a:t>关键路径</a:t></a:r>".encode(), "<a:t>关键路径</a:t></a:r>".encode() + math),
    )
    assert "关键路径" not in [unit.text for unit in extract_units(patched)]


# ── writing: translation only ─────────────────────────────────────────────


def test_unchanged_translations_leave_every_part_as_it_was(tmp_path: Path) -> None:
    units = extract_units(MIXED)
    out = tmp_path / "same.pptx"
    write_file(MIXED, out, {unit.id: Translation(unit.text) for unit in units}, target_lang="fr")
    with zipfile.ZipFile(MIXED) as before, zipfile.ZipFile(out) as after:
        assert [(info.filename, info.CRC) for info in before.infolist()] == [
            (info.filename, info.CRC) for info in after.infolist()
        ]


def test_translation_only_keeps_formats_links_and_fields(tmp_path: Path) -> None:
    result, out = _write(tmp_path)
    assert [unit.text for unit in extract_units(out)] == [_french(unit).text for unit in extract_units(MIXED)]
    slide = _xml(out, "ppt/slides/slide2.xml")
    lead = _find(slide, "Attention : seuls les plans à jour font foi.")
    runs = lead.findall("a:r", NS)
    assert [run.findtext("a:t", namespaces=NS) for run in runs] == ["Attention :", " seuls les plans à jour font foi."]
    assert runs[0].find("a:rPr", NS).get("b") == "1" and runs[1].find("a:rPr", NS).get("b") is None
    assert {run.find("a:rPr", NS).get("lang") for run in runs} == {"fr-FR"}
    assert lead.find("a:pPr", NS).get("latinLnBrk") == "0"
    link = _find(slide, "Détails sur le site du projet.")
    linked = [run for run in link.findall("a:r", NS) if run.find("a:rPr/a:hlinkClick", NS) is not None]
    assert [run.findtext("a:t", namespaces=NS) for run in linked] == ["site du projet"]
    page = _find(_xml(out, "ppt/slides/slide3.xml"), "Page 3")
    field = page.find("a:fld", NS)
    assert field.get("id") == "{B6F15528-21DE-4FAA-801E-634DDDAF4B2B}" and field.get("type") == "slidenum"
    title = _find(_xml(out, "ppt/slides/slide1.xml"), "Rapport d’avancement 2026\nTroisième trimestre")
    assert len(title.findall("a:br", NS)) == 1
    assert result.review[0].reason == REASON_VERTICAL


def test_a_single_run_only_changes_its_text(tmp_path: Path) -> None:
    _result, out = _write(tmp_path)
    part = "ppt/slideMasters/slideMaster1.xml"
    before = _find(_xml(MIXED, part), "内部资料 请勿外传")
    after = _find(_xml(out, part), "FR 内部资料 请勿外传")
    old_rpr, new_rpr = before.find("a:r/a:rPr", NS), after.find("a:r/a:rPr", NS)
    assert dict(old_rpr.attrib) == {"lang": "zh-CN", "altLang": "en-US", "sz": "1000"}
    assert dict(new_rpr.attrib) == {"lang": "fr-FR", "altLang": "en-US", "sz": "1000"}
    assert list(new_rpr.attrib) == list(old_rpr.attrib)  # same attribute order: only the value changed
    assert len(after.findall("a:r", NS)) == 1
    assert after.find("a:endParaRPr", NS).get("lang") == "fr-FR"


def test_lost_marks_write_the_paragraph_in_its_main_format(tmp_path: Path) -> None:
    _result, out = _write(tmp_path, marked=False)
    lead = _find(_xml(out, "ppt/slides/slide2.xml"), "Attention : seuls les plans à jour font foi.")
    runs = lead.findall("a:r", NS)
    assert len(runs) == 1 and runs[0].find("a:rPr", NS).get("b") is None
    page = _find(_xml(out, "ppt/slides/slide3.xml"), "Page 3")
    # The field goes where its text is found in the translation.
    assert [child.tag.split("}")[1] for child in page if child.tag != f"{{{NS['a']}}}pPr"] == ["r", "fld"]


def test_alternate_content_fallback_follows_the_choice(tmp_path: Path) -> None:
    _result, out = _write(tmp_path)
    slide = _xml(out, "ppt/slides/slide7.xml")
    choice = slide.find(".//mc:Choice", NS)
    fallback = slide.find(".//mc:Fallback", NS)
    assert [_text(p) for p in _paragraphs(choice)] == [_text(p) for p in _paragraphs(fallback)] == ["FR 兼容内容里的文字"]


def test_what_the_fit_changes_on_the_choice_reaches_the_fallback(tmp_path: Path) -> None:
    def shrink(_read_part, part_name, _root, touched):
        for body in touched:
            words = "".join(t.text or "" for t in body.iter(f"{{{NS['a']}}}t"))
            if part_name != "ppt/slides/slide7.xml" or not words.startswith("FR 兼容"):
                continue
            body_pr = body.find("a:bodyPr", NS)
            for child in list(body_pr):
                body_pr.remove(child)
            etree.SubElement(body_pr, f"{{{NS['a']}}}normAutofit", fontScale="62500", lnSpcReduction="20000")
            for rpr in body.iter(f"{{{NS['a']}}}rPr"):
                rpr.set("sz", "1100")
            ext = body.getparent().find("p:spPr/a:xfrm/a:ext", NS)
            ext.set("cy", str(int(ext.get("cy")) + 254000))
        return []

    _result, out = _write(tmp_path, fit=shrink)
    slide = _xml(out, "ppt/slides/slide7.xml")
    choice, fallback = (slide.find(f".//mc:{name}/p:sp", NS) for name in ("Choice", "Fallback"))
    for shape in (choice, fallback):
        assert shape.find("p:txBody/a:bodyPr/a:normAutofit", NS).attrib == {"fontScale": "62500", "lnSpcReduction": "20000"}
        assert {rpr.get("sz") for rpr in shape.iter(f"{{{NS['a']}}}rPr")} == {"1100"}
    sizes = [shape.find("p:spPr/a:xfrm/a:ext", NS).attrib for shape in (choice, fallback)]
    assert sizes[0] == sizes[1]


def test_cjk_targets_keep_line_breaking_and_get_their_language(tmp_path: Path) -> None:
    units = extract_units(MIXED)
    out = tmp_path / "zh.pptx"
    result = write_file(MIXED, out, {unit.id: Translation(f"中{unit.text}") for unit in units}, target_lang="zh")
    paragraph = _find(_xml(out, "ppt/slides/slide4.xml"), "中关键路径")
    assert paragraph.find("a:r/a:rPr", NS).get("lang") == "zh-CN"
    assert paragraph.find("a:pPr", NS) is None or paragraph.find("a:pPr", NS).get("latinLnBrk") is None
    assert result.review == []  # vertical boxes only need a look for Western text


def test_language_tags() -> None:
    assert lang_tag("fr") == "fr-FR" and lang_tag("en") == "en-US" and lang_tag("zh") == "zh-CN"
    assert lang_tag("en-GB") == "en-GB" and lang_tag("en_GB") == "en-GB"
    assert lang_tag("custom:克里奥尔语") == "" and lang_tag("") == ""


# ── writing: bilingual ────────────────────────────────────────────────────


def test_bilingual_puts_the_translation_under_each_source_paragraph(tmp_path: Path) -> None:
    _result, out = _write(tmp_path, bilingual=True)
    body = _xml(out, "ppt/slides/slide2.xml").findall(".//p:sp", NS)[1].find("p:txBody", NS)
    texts = [_text(p) for p in body.findall("a:p", NS)]
    assert texts[:4] == [
        "本项目位于卡萨布兰卡，总建筑面积 12 万平方米。",
        "FR 本项目位于卡萨布兰卡，总建筑面积 12 万平方米。",
        "注意：所有图纸以最新版本为准。",
        "Attention : seuls les plans à jour font foi.",
    ]
    source, translation = body.findall("a:p", NS)[2:4]
    assert source.find("a:pPr", NS) is None or source.find("a:pPr/a:buNone", NS) is None
    ppr = translation.find("a:pPr", NS)
    # No second bullet; the text lines up with the source text, not the bullet.
    assert ppr.find("a:buNone", NS) is not None and ppr.get("indent") == "0"
    assert source.find("a:r/a:rPr", NS).get("lang") is None  # the source paragraph is untouched
    assert len(extract_units(out)) == 2 * len(extract_units(MIXED))


def test_bilingual_keeps_numbering_and_paragraph_animations(tmp_path: Path) -> None:
    _result, out = _write(tmp_path, bilingual=True)
    ranges = [
        (int(item.get("st")), int(item.get("end")))
        for item in _xml(out, "ppt/slides/slide5.xml").iter(f"{{{NS['p']}}}pRg")
    ]
    assert ranges == [(0, 1), (2, 3), (4, 5)]
    paragraphs = _xml(out, "ppt/slides/slide6.xml").findall(".//a:p", NS)
    numbers = [p.find("a:pPr/a:buAutoNum", NS) for p in paragraphs]
    assert [item.get("startAt") if item is not None else None for item in numbers] == ["1", None, "2", None, "3", None]
    assert all(p.find("a:pPr/a:buNone", NS) is not None for p in paragraphs[1::2])


def test_bilingual_animation_ranges_cover_untranslated_paragraphs_too(tmp_path: Path) -> None:
    units = extract_units(MIXED)
    translations = {unit.id: Translation(f"FR {unit.text}" if unit.text == "启动幕墙深化设计" else unit.text) for unit in units}
    out = tmp_path / "one.pptx"
    write_file(MIXED, out, translations, bilingual=True, target_lang="fr")
    ranges = [
        (int(item.get("st")), int(item.get("end")))
        for item in _xml(out, "ppt/slides/slide5.xml").iter(f"{{{NS['p']}}}pRg")
    ]
    assert ranges == [(0, 0), (1, 2), (3, 3)]


# ── fitting and the check after saving ────────────────────────────────────


def test_the_fit_hook_sees_each_written_part_and_its_issues_go_to_review(tmp_path: Path) -> None:
    calls = []

    def fit(read_part, part_name, root, touched):
        calls.append((part_name, [etree.QName(item).localname for item in touched]))
        assert read_part("ppt/slideLayouts/slideLayout2.xml")
        if part_name == "ppt/slides/slide3.xml":
            frame = next(item for item in touched if etree.QName(item).localname == "graphicFrame")
            return [SimpleNamespace(part_name=part_name, target=frame, reason="缩到 60% 仍放不下")]
        return []

    result, _out = _write(tmp_path, fit=fit)
    parts = [name for name, _touched in calls]
    assert parts[:2] == ["ppt/slides/slide1.xml", "ppt/slides/slide2.xml"]
    assert not any("notesSlide" in name for name in parts)
    assert ("ppt/slides/slide3.xml", ["txBody", "graphicFrame", "txBody"]) in calls
    assert [(item.context, item.reason) for item in result.review if item.reason.startswith("缩到")] == [
        ("第 3 页 · Table 2", "缩到 60% 仍放不下")
    ]


def test_fixed_layout_reaches_the_fit_only_when_on(tmp_path: Path) -> None:
    seen = []

    def fit(read_part, part_name, root, touched, **kwargs):
        seen.append(kwargs)
        return []

    _write(tmp_path, fit=fit)
    assert seen and all(kwargs == {} for kwargs in seen)
    seen.clear()
    _write(tmp_path, fit=fit, fixed_layout=True)
    assert seen and all(kwargs == {"fixed_layout": True} for kwargs in seen)


def test_a_box_the_fit_grows_passes_the_check(tmp_path: Path) -> None:
    def grow(_read_part, _part_name, _root, touched):
        for body in touched:
            shape = body.getparent()
            ext = shape.find("p:spPr/a:xfrm/a:ext", NS) if shape is not None else None
            if ext is not None:
                ext.set("cy", str(int(ext.get("cy")) + 100))
        return []

    _result, out = _write(tmp_path, fit=grow)
    assert out.is_file()


def test_a_damaged_result_is_not_kept(tmp_path: Path) -> None:
    def move_something_else(_read_part, part_name, root, touched):
        if part_name == "ppt/slides/slide4.xml":
            picture = root.find(".//p:pic", NS)
            picture.find("p:spPr/a:xfrm/a:off", NS).set("x", "0")
        return []

    with pytest.raises(PptxError, match="未通过检查.*未改动的形状位置或大小变了"):
        _write(tmp_path, fit=move_something_else)
    assert not (tmp_path / "only.pptx").exists()

    def leave_a_mark(_read_part, _part_name, root, _touched):
        root.find(".//a:t", NS).text = "<g1>oops</g1>"
        return []

    with pytest.raises(PptxError, match="残留了格式标记"):
        _write(tmp_path, fit=leave_a_mark)
    assert not (tmp_path / "only.pptx").exists()


def test_the_source_file_is_never_touched(tmp_path: Path) -> None:
    source = tmp_path / "source.pptx"
    shutil.copyfile(MIXED, source)
    before = source.read_bytes()
    _write(tmp_path, source=source, bilingual=True)
    assert source.read_bytes() == before
