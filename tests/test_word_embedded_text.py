"""Word 文本框和脚注/尾注的翻译（core/word_embedded_text.py）。"""

from __future__ import annotations

import re
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.opc.packuri import PackURI
from docx.opc.part import Part
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls, qn

from core.word_coverage import build_word_coverage_plan, write_untranslated_docx
from core.word_document import (
    detect_hidden_word_content,
    extract_word_segments,
    extract_word_story_segments,
    write_bilingual_docx,
)

MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
WPS = "http://schemas.microsoft.com/office/word/2010/wordprocessingShape"
WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
V = "urn:schemas-microsoft-com:vml"

BOX_TEXTS = ["图例说明", "红色表示待施工区域"]
TRANSLATIONS = {
    "正文原文": "Texte du corps",
    "图例说明": "Légende",
    "红色表示待施工区域": "Le rouge indique les zones où les travaux restent à réaliser",
    "脚注正文": "Texte de la note de bas de page",
    "尾注正文": "Texte de la note de fin",
}


def _box_xml(texts: list[str], ppr: str = "") -> str:
    paragraphs = "".join(
        f'<w:p>{ppr}<w:r><w:rPr><w:sz w:val="22"/></w:rPr><w:t>{text}</w:t></w:r></w:p>' for text in texts
    )
    return f"<w:txbxContent>{paragraphs}</w:txbxContent>"


def _textbox_paragraph(texts: list[str], *, cx: int, cy: int, autofit: bool = False, ppr: str = "") -> str:
    box = _box_xml(texts, ppr)
    fit = "<a:spAutoFit/>" if autofit else ""
    return (
        f'<w:p {nsdecls("w")} xmlns:mc="{MC}" xmlns:wps="{WPS}" xmlns:wp="{WP}" xmlns:a="{A}" xmlns:v="{V}">'
        "<w:r><w:t>正文原文</w:t></w:r>"
        "<w:r><mc:AlternateContent>"
        '<mc:Choice Requires="wps"><w:drawing>'
        '<wp:anchor distT="0" distB="0" distL="0" distR="0" simplePos="0" relativeHeight="1" '
        'behindDoc="0" locked="0" layoutInCell="1" allowOverlap="1">'
        '<wp:simplePos x="0" y="0"/>'
        '<wp:positionH relativeFrom="column"><wp:posOffset>0</wp:posOffset></wp:positionH>'
        '<wp:positionV relativeFrom="paragraph"><wp:posOffset>0</wp:posOffset></wp:positionV>'
        f'<wp:extent cx="{cx}" cy="{cy}"/><wp:wrapNone/><wp:docPr id="1" name="文本框 1"/>'
        '<a:graphic><a:graphicData uri="http://schemas.microsoft.com/office/word/2010/wordprocessingShape">'
        '<wps:wsp><wps:cNvSpPr txBox="1"/><wps:spPr><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></wps:spPr>'
        f"<wps:txbx>{box}</wps:txbx>"
        f'<wps:bodyPr wrap="square">{fit}</wps:bodyPr>'
        "</wps:wsp></a:graphicData></a:graphic></wp:anchor></w:drawing></mc:Choice>"
        f'<mc:Fallback><w:pict><v:shape style="width:144pt;height:60pt"><v:textbox>{box}</v:textbox></v:shape></w:pict></mc:Fallback>'
        "</mc:AlternateContent></w:r></w:p>"
    )


def _notes_part(doc, *, name: str, tag: str, ref_tag: str, text: str, rel_type: str, content_type: str) -> None:
    xml = (
        f'<w:{tag}s {nsdecls("w")}>'
        f'<w:{tag} w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:{tag}>'
        f'<w:{tag} w:type="continuationSeparator" w:id="0"><w:p><w:r><w:continuationSeparator/></w:r></w:p></w:{tag}>'
        f'<w:{tag} w:id="1"><w:p><w:pPr><w:pStyle w:val="FootnoteText"/></w:pPr>'
        f'<w:r><w:rPr><w:vertAlign w:val="superscript"/></w:rPr><w:{ref_tag}/></w:r>'
        f'<w:r><w:t xml:space="preserve"> {text}</w:t></w:r></w:p></w:{tag}>'
        f"</w:{tag}s>"
    ).encode()
    part = Part(PackURI(f"/word/{name}.xml"), content_type, xml, doc.part.package)
    doc.part.relate_to(part, rel_type)


def build_document(
    path: Path, *, cx: int = 2286000, cy: int = 914400, autofit: bool = False, ppr: str = ""
) -> None:
    """``ppr``：文本框里每段的 ``<w:pPr>``，用来给段落设行距。"""
    doc = Document()
    end = doc.element.body.find(qn("w:sectPr"))
    end.addprevious(parse_xml(_textbox_paragraph(BOX_TEXTS, cx=cx, cy=cy, autofit=autofit, ppr=ppr)))
    end.addprevious(
        parse_xml(
            f'<w:p {nsdecls("w")}><w:r><w:t>这一段带脚注</w:t></w:r>'
            '<w:r><w:footnoteReference w:id="1"/></w:r><w:r><w:endnoteReference w:id="1"/></w:r></w:p>'
        )
    )
    _notes_part(
        doc,
        name="footnotes",
        tag="footnote",
        ref_tag="footnoteRef",
        text="脚注正文",
        rel_type=RT.FOOTNOTES,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml",
    )
    _notes_part(
        doc,
        name="endnotes",
        tag="endnote",
        ref_tag="endnoteRef",
        text="尾注正文",
        rel_type=RT.ENDNOTES,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.endnotes+xml",
    )
    doc.save(path)


def _xml(path: Path, part: str) -> str:
    with zipfile.ZipFile(path) as archive:
        return archive.read(part).decode("utf-8")


def _box_texts(xml: str) -> list[list[str]]:
    """每个 w:txbxContent 里各段的文字（正本在前、VML 备份在后）。"""
    boxes = re.findall(r"<w:txbxContent>(.*?)</w:txbxContent>", xml, re.S)
    return [
        ["".join(re.findall(r"<w:t(?:\s[^>]*)?>([^<]*)</w:t>", p)) for p in re.findall(r"<w:p[ >].*?</w:p>", box, re.S)]
        for box in boxes
    ]


def _note_texts(xml: str, tag: str) -> list[str]:
    note = re.search(rf'<w:{tag} w:id="1">(.*?)</w:{tag}>', xml, re.S).group(1)
    return ["".join(re.findall(r"<w:t(?:\s[^>]*)?>([^<]*)</w:t>", p)).strip() for p in re.findall(r"<w:p[ >].*?</w:p>", note, re.S)]


class WordStoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.source = self.root / "source.docx"
        build_document(self.source)

    def _write(self, source: Path | None = None, *, only: bool = False, issues: list | None = None, name: str = "out") -> Path:
        return write_bilingual_docx(
            source_path=source or self.source,
            output_dir=self.root / name,
            translations=dict(TRANSLATIONS),
            target_lang="fr",
            output_translation_only=only,
            issue_callback=issues.append if issues is not None else None,
        )

    def test_segments_include_textbox_and_note_paragraphs_once(self) -> None:
        segments = extract_word_segments(self.source, target_lang="fr")
        by_kind: dict[str, list[str]] = {}
        for segment in segments:
            by_kind.setdefault(segment.kind, []).append(segment.source)
        # VML 备份里的同一个文本框不重复列出；分隔线条目不算。
        self.assertEqual(by_kind["textbox"], BOX_TEXTS)
        self.assertEqual(by_kind["footnote"], ["脚注正文"])
        self.assertEqual(by_kind["endnote"], ["尾注正文"])
        locations = {segment.source: segment.location for segment in segments}
        self.assertEqual(locations["红色表示待施工区域"], "textbox[0].paragraph[1]")
        self.assertEqual(locations["脚注正文"], "footnote[1].paragraph[0]")

    def test_bilingual_adds_translation_after_each_paragraph_and_syncs_fallback(self) -> None:
        output = self._write()
        boxes = _box_texts(_xml(output, "word/document.xml"))
        expected = ["图例说明", "Légende", "红色表示待施工区域", TRANSLATIONS["红色表示待施工区域"]]
        self.assertEqual(boxes, [expected, expected])
        self.assertEqual(_note_texts(_xml(output, "word/footnotes.xml"), "footnote"), ["脚注正文", TRANSLATIONS["脚注正文"]])
        self.assertEqual(_note_texts(_xml(output, "word/endnotes.xml"), "endnote"), ["尾注正文", TRANSLATIONS["尾注正文"]])
        # 文件仍能被 python-docx 打开，正文照常翻译。
        self.assertIn("Texte du corps", [p.text for p in Document(output).paragraphs])

    def test_translation_only_replaces_text_and_keeps_note_number(self) -> None:
        output = self._write(only=True)
        boxes = _box_texts(_xml(output, "word/document.xml"))
        expected = ["Légende", TRANSLATIONS["红色表示待施工区域"]]
        self.assertEqual(boxes, [expected, expected])
        footnotes = _xml(output, "word/footnotes.xml")
        self.assertEqual(_note_texts(footnotes, "footnote"), [TRANSLATIONS["脚注正文"]])
        note = re.search(r'<w:footnote w:id="1">(.*?)</w:footnote>', footnotes, re.S).group(1)
        # 编号标记还在第一个上标 run 里，译文写进了后面那个普通 run。
        runs = re.findall(r"<w:r>.*?</w:r>", note, re.S)
        self.assertIn("<w:footnoteRef/>", runs[0])
        self.assertNotIn("<w:t", runs[0])
        self.assertIn(TRANSLATIONS["脚注正文"], runs[1])
        self.assertNotIn("superscript", runs[1])

    def test_small_box_shrinks_font_and_reports_when_still_too_small(self) -> None:
        issues: list[dict] = []
        roomy = self._write(name="roomy", issues=issues)
        sizes = set(re.findall(r'<w:sz w:val="(\d+)"/>', _xml(roomy, "word/document.xml")))
        # 2.5 英寸 × 1 英寸：四段放得下（译文会折行），缩一点。
        self.assertTrue(sizes and all(12 <= int(size) < 22 for size in sizes), sizes)
        self.assertEqual(issues, [])

        tiny = self.root / "tiny.docx"
        build_document(tiny, cx=914400, cy=228600)  # 1 英寸 × 0.25 英寸
        output = self._write(tiny, name="tiny", issues=issues)
        sizes = set(re.findall(r'<w:sz w:val="(\d+)"/>', _xml(output, "word/document.xml")))
        self.assertIn("13", sizes)  # 22 × 0.6
        self.assertEqual([issue["kind"] for issue in issues], ["textbox_overflow"])
        self.assertEqual(issues[0]["location"], "textbox[0].paragraph[0]")

    def test_big_or_autofit_box_keeps_font_size(self) -> None:
        for name, kwargs in (("big", {"cx": 5486400, "cy": 2743200}), ("autofit", {"cy": 228600, "autofit": True})):
            source = self.root / f"{name}.docx"
            build_document(source, **kwargs)
            output = self._write(source, name=name)
            sizes = set(re.findall(r'<w:sz w:val="(\d+)"/>', _xml(output, "word/document.xml")))
            self.assertEqual(sizes, {"22"}, name)

    def test_line_spacing_counts_when_judging_room(self) -> None:
        sizes = {}
        for name, ppr in (("single", ""), ("wide", '<w:pPr><w:spacing w:line="360" w:lineRule="auto"/></w:pPr>')):
            source = self.root / f"{name}.docx"
            build_document(source, cx=180 * 12700, cy=100 * 12700, ppr=ppr)
            output = self._write(source, name=name)
            sizes[name] = set(re.findall(r'<w:sz w:val="(\d+)"/>', _xml(output, "word/document.xml")))
        # 同样大的框：单倍行距放得下，1.5 倍行距要缩字。
        self.assertEqual(sizes["single"], {"22"})
        self.assertTrue(all(int(size) < 22 for size in sizes["wide"]), sizes["wide"])

    def test_exact_line_spacing_shrinks_with_the_font(self) -> None:
        source = self.root / "exact.docx"
        build_document(source, cx=180 * 12700, cy=80 * 12700, ppr='<w:pPr><w:spacing w:line="400" w:lineRule="exact"/></w:pPr>')
        output = self._write(source, name="exact")
        xml = _xml(output, "word/document.xml")
        size = {int(value) for value in re.findall(r'<w:sz w:val="(\d+)"/>', xml)}
        line = {int(value) for value in re.findall(r'w:line="(\d+)"', xml)}
        self.assertEqual(len(size), 1)
        self.assertEqual(len(line), 1)
        # 字缩到几成，固定行距也缩到几成；规则仍是固定值。
        self.assertAlmostEqual(line.pop() / 400, size.pop() / 22, delta=0.05)
        self.assertNotIn('w:lineRule="auto"', xml)

    def test_untranslated_only_fills_in_what_is_missing_without_repeating(self) -> None:
        first = self._write()
        # 已经译过的文档再补译一遍：什么也不加。
        self.assertEqual(extract_word_story_segments(first, target_lang="fr"), [])
        again = write_untranslated_docx(
            source_path=first,
            output_dir=self.root / "again",
            plan=build_word_coverage_plan(first, target_lang="fr"),
            translations=dict(TRANSLATIONS),
            target_lang="fr",
        )
        self.assertEqual(_box_texts(_xml(again, "word/document.xml"))[0], _box_texts(_xml(first, "word/document.xml"))[0])
        self.assertEqual(_note_texts(_xml(again, "word/footnotes.xml"), "footnote"), ["脚注正文", TRANSLATIONS["脚注正文"]])

        # 原文还没翻的文档：补译把文本框和脚注都补上。
        pending = extract_word_story_segments(self.source, target_lang="fr")
        self.assertEqual([segment.source for segment in pending], [*BOX_TEXTS, "脚注正文", "尾注正文"])
        filled = write_untranslated_docx(
            source_path=self.source,
            output_dir=self.root / "filled",
            plan=build_word_coverage_plan(self.source, target_lang="fr"),
            translations=dict(TRANSLATIONS),
            target_lang="fr",
        )
        self.assertIn("Légende", _box_texts(_xml(filled, "word/document.xml"))[0])
        self.assertEqual(_note_texts(_xml(filled, "word/endnotes.xml"), "endnote"), ["尾注正文", TRANSLATIONS["尾注正文"]])

    def test_textboxes_and_notes_no_longer_reported_as_untranslated(self) -> None:
        report = detect_hidden_word_content(self.source)
        self.assertEqual(report.textbox_count, 1)
        self.assertEqual(report.footnote_count, 2)
        self.assertFalse(report.found)
        self.assertEqual(report.describe(), "")

    def test_document_without_notes_or_boxes_is_untouched(self) -> None:
        plain = self.root / "plain.docx"
        doc = Document()
        doc.add_paragraph("正文原文")
        doc.save(plain)
        output = self._write(plain, name="plain")
        with zipfile.ZipFile(output) as archive:
            names = set(archive.namelist())
        self.assertNotIn("word/footnotes.xml", names)
        self.assertEqual([p.text for p in Document(output).paragraphs], ["正文原文", "Texte du corps"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
