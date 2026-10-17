"""SmartArt text in PPT files (``core/pptx_smartart.py``) on slide 4 of ``graphics.pptx``.

The diagram parts come from a file saved by PowerPoint (see
``tests/fixtures/pptx/build_graphics.py``): three filled circles, the second
with a bold first half, the third with two paragraphs, plus an empty one.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

from lxml import etree

from core.pptx_smartart import REASON_DRAWING_NOT_SYNCED, REASON_TEXT_TOO_LONG
from core.pptx_text import Translation, extract_units, scan_file, write_file

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "pptx"
GRAPHICS = FIXTURES / "graphics.pptx"
NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "dgm": "http://schemas.openxmlformats.org/drawingml/2006/diagram",
    "dsp": "http://schemas.microsoft.com/office/drawing/2008/diagram",
}
FRENCH = {
    "需求调研": ("Étude des besoins", ""),
    "方案设计": ("Conception détaillée", "Conception <g1>détaillée</g1>"),
    "施工交付": ("Exécution", ""),
    "竣工验收": ("Réception", ""),
}


def _units(source: Path = GRAPHICS):
    return [unit for unit in extract_units(source) if "/diagrams/" in unit.id]


def _write(tmp_path: Path, *, bilingual: bool = False, source: Path = GRAPHICS, skip: str = ""):
    translations = {}
    for unit in extract_units(source):
        if unit.text == skip:
            continue
        text, marked = FRENCH.get(unit.text, (f"FR {unit.text}", ""))
        translations[unit.id] = Translation(text, marked)
    out = tmp_path / ("bilingual.pptx" if bilingual else "translated.pptx")
    return write_file(source, out, translations, bilingual=bilingual, target_lang="fr"), out


def _root(path: Path, name: str):
    with zipfile.ZipFile(path) as package:
        return etree.fromstring(package.read(f"ppt/diagrams/{name}"))


def _texts(root, path: str) -> list[list[str]]:
    found = []
    for body in root.iterfind(path, NS):
        paragraphs = ["".join(t.text or "" for t in p.iterfind(".//a:t", NS)) for p in body.findall("a:p", NS)]
        if any(paragraphs):
            found.append(paragraphs)
    return found


def test_units_are_the_node_paragraphs_with_marks() -> None:
    units = _units()
    assert [(unit.text, unit.context, unit.marked) for unit in units] == [
        ("需求调研", "第 4 页 · SmartArt 流程 · 第 1 项", ""),
        ("方案设计", "第 4 页 · SmartArt 流程 · 第 2 项", "方案<g1>设计</g1>"),
        ("施工交付", "第 4 页 · SmartArt 流程 · 第 3 项 · 第 1 段", ""),
        ("竣工验收", "第 4 页 · SmartArt 流程 · 第 3 项 · 第 2 段", ""),
    ]
    assert scan_file(GRAPHICS).warnings == []


def test_translation_only_writes_content_and_drawing(tmp_path: Path) -> None:
    result, out = _write(tmp_path)
    assert result.review == []
    expected = [["Étude des besoins"], ["Conception détaillée"], ["Exécution", "Réception"]]
    assert _texts(_root(out, "data1.xml"), ".//dgm:pt/dgm:t") == expected
    assert _texts(_root(out, "drawing1.xml"), ".//dsp:sp/dsp:txBody") == expected
    # 带标记的译文：内容和显示副本都是「Conception」加粗（同原文主体）、「détaillée」常规（同原文的后半截）。
    for name, path in (("data1.xml", ".//dgm:pt/dgm:t"), ("drawing1.xml", ".//dsp:sp/dsp:txBody")):
        runs = [
            (run.findtext("a:t", namespaces=NS), run.find("a:rPr", NS).get("b"))
            for body in _root(out, name).iterfind(path, NS)
            for run in body.iterfind(".//a:r", NS)
            if "Conception" in (run.findtext("a:t", namespaces=NS) or "") or "détaillée" in (run.findtext("a:t", namespaces=NS) or "")
        ]
        assert runs == [("Conception ", "1"), ("détaillée", "0")], name
    langs = {rpr.get("lang") for rpr in _root(out, "data1.xml").iterfind(".//dgm:pt/dgm:t//a:r/a:rPr", NS)}
    assert langs == {"fr-FR"}


def test_shapes_keep_their_place_and_nodes_their_links(tmp_path: Path) -> None:
    _result, out = _write(tmp_path)
    for name in ("data1.xml", "drawing1.xml"):
        before, after = _root(GRAPHICS, name), _root(out, name)
        for tag in ("a:xfrm", "dsp:txXfrm", "dgm:cxnLst"):
            assert [etree.tostring(item) for item in before.iterfind(f".//{tag}", NS)] == [
                etree.tostring(item) for item in after.iterfind(f".//{tag}", NS)
            ]
        assert [item.get("modelId") for item in before.iter()] == [item.get("modelId") for item in after.iter()]


def test_drawing_text_shrinks_together_when_it_does_not_fit(tmp_path: Path) -> None:
    _result, out = _write(tmp_path)
    sizes_before = {
        int(rpr.get("sz")) for rpr in _root(GRAPHICS, "drawing1.xml").iterfind(".//dsp:txBody//a:r/a:rPr", NS)
    }
    sizes_after = {int(rpr.get("sz")) for rpr in _root(out, "drawing1.xml").iterfind(".//dsp:txBody//a:r/a:rPr", NS)}
    assert len(sizes_before) == len(sizes_after) == 1
    assert 0.6 * min(sizes_before) <= min(sizes_after) < min(sizes_before)
    # 内容部件不缩（PowerPoint 按内容重排时自己定字号）。
    assert [rpr.get("sz") for rpr in _root(out, "data1.xml").iterfind(".//a:rPr", NS)] == [
        rpr.get("sz") for rpr in _root(GRAPHICS, "data1.xml").iterfind(".//a:rPr", NS)
    ]


def test_bilingual_adds_the_translation_after_each_paragraph(tmp_path: Path) -> None:
    result, out = _write(tmp_path, bilingual=True)
    # 每个圆里多了一段译文，字要缩得很小：列入复核。
    assert [(item.context, item.reason) for item in result.review] == [("第 4 页 · SmartArt 流程", REASON_TEXT_TOO_LONG)]
    expected = [
        ["需求调研", "Étude des besoins"],
        ["方案设计", "Conception détaillée"],
        ["施工交付", "Exécution", "竣工验收", "Réception"],
    ]
    assert _texts(_root(out, "data1.xml"), ".//dgm:pt/dgm:t") == expected
    assert _texts(_root(out, "drawing1.xml"), ".//dsp:sp/dsp:txBody") == expected


def test_untranslated_node_is_not_a_mismatch(tmp_path: Path) -> None:
    result, out = _write(tmp_path, skip="竣工验收")
    assert result.review == []
    assert _texts(_root(out, "drawing1.xml"), ".//dsp:sp/dsp:txBody")[2] == ["Exécution", "竣工验收"]


def test_drawing_text_that_matches_no_node_is_listed(tmp_path: Path) -> None:
    source = tmp_path / "stale.pptx"
    with zipfile.ZipFile(GRAPHICS) as package, zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as target:
        for info in package.infolist():
            data = package.read(info.filename)
            if info.filename == "ppt/diagrams/drawing1.xml":
                data = re.sub("需求调研".encode(), "旧的文字".encode(), data)
            target.writestr(info, data)
    result, out = _write(tmp_path, source=source)
    assert [(item.context, item.reason) for item in result.review] == [
        ("第 4 页 · SmartArt 流程", REASON_DRAWING_NOT_SYNCED)
    ]
    assert _texts(_root(out, "drawing1.xml"), ".//dsp:sp/dsp:txBody")[0] == ["旧的文字"]
    assert _texts(_root(out, "data1.xml"), ".//dgm:pt/dgm:t")[0] == ["Étude des besoins"]
