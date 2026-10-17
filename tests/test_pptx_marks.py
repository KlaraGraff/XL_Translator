"""DrawingML inline marks (``core/drawingml_marks.py``): one ``a:p`` at a time.

The marks are Word's ``<gN>…</gN>`` (same parser and checks); these pin what
is DrawingML's own: runs split only by language are one format, a line break
closes a mark, fields are marks of their own, and the written runs reuse the
original ``a:rPr`` (no rebuild when the pieces line up).
"""

from __future__ import annotations

from lxml import etree

from core.drawingml_marks import look_key, paragraph_marks, read_pieces, write_paragraph

A = "http://schemas.openxmlformats.org/drawingml/2006/main"


def _p(body: str):
    return etree.fromstring(f'<a:p xmlns:a="{A}">{body}</a:p>')


def _r(text: str, **attrs: str) -> str:
    rendered = " ".join(f'{key}="{value}"' for key, value in attrs.items())
    return f"<a:r><a:rPr {rendered}/><a:t>{text}</a:t></a:r>"


def _marks(paragraph):
    return paragraph_marks(paragraph, read_pieces(paragraph))


def _runs(paragraph) -> list[tuple[str, dict]]:
    out = []
    for child in paragraph:
        name = etree.QName(child).localname
        if name == "r":
            out.append((child.findtext(f"{{{A}}}t"), dict(child.find(f"{{{A}}}rPr").attrib)))
        elif name in ("br", "fld"):
            out.append((name, {}))
    return out


def test_language_spelling_and_font_names_are_not_a_format() -> None:
    plain = _r("Windows ", lang="en-US", sz="1800") + _r("系统", lang="zh-CN", sz="1800", err="1")
    marks = _marks(_p(plain))
    assert marks.plain == "Windows 系统" and marks.marked == ""
    assert look_key(etree.fromstring(f'<a:rPr xmlns:a="{A}" b="0" u="none" lang="fr-FR"/>')) == ((), ())


def test_bold_lead_and_whitespace_between_bold_runs() -> None:
    paragraph = _p(_r("注意", b="1") + _r(" ", b="1") + _r("事项：", b="1") + _r("所有图纸以最新版本为准。"))
    marks = _marks(paragraph)
    assert marks.marked == "<g1>注意 事项：</g1>所有图纸以最新版本为准。"
    written = write_paragraph(marks, "Attention : plans à jour.", "<g1>Attention :</g1> plans à jour.")
    assert _runs(paragraph) == [("Attention :", {"b": "1"}), (" plans à jour.", {})]
    assert len(written) == 2


def test_a_line_break_closes_the_mark() -> None:
    paragraph = _p(_r("要点", b="1") + "<a:br><a:rPr/></a:br>" + _r("一", b="1") + _r("：后面的说明文字"))
    marks = _marks(paragraph)
    assert marks.marked == "<g1>要点</g1>\n<g2>一</g2>：后面的说明文字"
    write_paragraph(marks, "Point\nUn : la suite", "<g1>Point</g1>\n<g2>Un</g2> : la suite")
    assert _runs(paragraph) == [("Point", {"b": "1"}), ("br", {}), ("Un", {"b": "1"}), (" : la suite", {})]


def test_reordered_marks_rebuild_the_paragraph() -> None:
    paragraph = _p(_r("红", b="1") + _r("色和颜色搭配") + _r("蓝", i="1"))
    marks = _marks(paragraph)
    assert marks.marked == "<g1>红</g1>色和颜色搭配<g2>蓝</g2>"
    write_paragraph(marks, "bleu avec rouge", "<g2>bleu</g2> avec <g1>rouge</g1>")
    assert _runs(paragraph) == [("bleu", {"i": "1"}), (" avec ", {}), ("rouge", {"b": "1"})]


def test_wrong_marks_fall_back_to_the_main_format() -> None:
    paragraph = _p(_r("注意：", b="1") + _r("所有图纸以最新版本为准。"))
    marks = _marks(paragraph)
    write_paragraph(marks, "Attention : plans à jour.", "<g1>Attention : plans à jour.")
    assert _runs(paragraph) == [("Attention : plans à jour.", {})]


def test_line_count_must_match_for_marks() -> None:
    paragraph = _p(_r("甲", b="1") + "<a:br/>" + _r("乙丙丁"))
    marks = _marks(paragraph)
    assert marks.marked == "<g1>甲</g1>\n乙丙丁"
    write_paragraph(marks, "A B", "<g1>A</g1> B")
    assert _runs(paragraph) == [("A B", {})]


def test_fields_keep_their_element_and_id() -> None:
    field = '<a:fld id="{1}" type="slidenum"><a:rPr/><a:t>7</a:t></a:fld>'
    paragraph = _p(_r("第 ") + field + _r(" 页"))
    marks = _marks(paragraph)
    assert marks.marked == "第 <g1>7</g1> 页"
    write_paragraph(marks, "Page 7", "Page <g1>7</g1>")
    assert _runs(paragraph) == [("Page ", {}), ("fld", {})]
    assert paragraph.find(f"{{{A}}}fld").get("id") == "{1}"
    # A field whose text cannot be marked stays at the end it was near.
    empty = _p(_r("日期") + '<a:fld id="{2}" type="datetime1"><a:rPr/><a:t></a:t></a:fld>')
    marks = _marks(empty)
    assert marks.marked == ""
    write_paragraph(marks, "Date", "")
    assert _runs(empty) == [("Date", {}), ("fld", {})]


def test_sources_that_look_like_marks_or_have_too_many_formats_go_unmarked() -> None:
    assert _marks(_p(_r("用 &lt;g1&gt; 标记", b="1") + _r("说明文字更长一些"))).marked == ""
    many = "".join(_r(f"词{index}", b="1") + _r("，") for index in range(13))
    assert _marks(_p(many)).marked == ""


def test_surrounding_whitespace_is_kept() -> None:
    paragraph = _p(_r("  标题  "))
    marks = _marks(paragraph)
    assert marks.plain == "标题"
    write_paragraph(marks, "Titre", "")
    assert _runs(paragraph) == [("  Titre  ", {})]


def test_runs_with_the_same_look_take_their_own_text() -> None:
    paragraph = _p(_r("甲", lang="zh-CN") + "<a:br/>" + _r("乙", lang="zh-CN", altLang="en-US"))
    marks = _marks(paragraph)
    write_paragraph(marks, "A\nB", "")
    assert _runs(paragraph) == [("A", {"lang": "zh-CN"}), ("br", {}), ("B", {"lang": "zh-CN", "altLang": "en-US"})]


def test_new_runs_go_before_the_end_of_paragraph_properties() -> None:
    paragraph = _p('<a:pPr algn="ctr"/>' + _r("红", b="1") + _r("绿") + '<a:endParaRPr lang="zh-CN"/>')
    marks = _marks(paragraph)
    write_paragraph(marks, "vert", "")
    names = [etree.QName(child).localname for child in paragraph]
    assert names == ["pPr", "r", "endParaRPr"]
