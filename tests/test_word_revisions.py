"""Word：内容控件里的文字照常翻译；未接受的修订在扫描期数出来、开始前由用户选处理方式。

V10.5 之前，``w:sdt``（内容控件）和 ``w:ins`` / ``w:del``（未接受的修订）里的文字
python-docx 都看不见：它们既不进待译清单，只在任务跑起来之后报一句「存在无法读取的
内容」。这一版：

- 内容控件的段落当成普通正文段落，随正文一起翻，用户没有选择；
- 修订在扫描时数进 ``tracked_change_count``，开始翻译时弹窗让用户选 ``skip``
  （默认，保持原文）或 ``translate``（连修订一起翻，作者/日期/id 不变）。

这里造的是真 .docx：python-docx 建骨架，lxml 塞 ``w:sdt`` / ``w:ins`` / ``w:del``。
"""

from __future__ import annotations

# 本仓库的开发副本里 core/_embedded_key.py 只有 EMBEDDED_KEY_ID，缺发布期注入的
# EMBEDDED_PRIVATE_KEY_B64：api.app 在这份副本上根本导不进来，整份 API 测试会
# 收在 collection 阶段。导入时就用开发兜底密钥补上，接口契约才测得到。
import base64 as _base64
import tempfile
import unittest
import zipfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from docx import Document
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls, qn

from core.task_runner import DoneMsg
from core.word_coverage import build_word_coverage_plan, write_untranslated_docx
from core.word_document import (
    detect_hidden_word_content,
    extract_word_segments,
    extract_word_story_segments,
    scan_word_sources,
    write_bilingual_docx,
)
from core.word_revisions import (
    REVISION_MODE_SKIP,
    REVISION_MODE_TRANSLATE,
    RevisionWriteStats,
    count_tracked_changes,
    normalize_revision_mode,
)
from core.word_document import WordFileItem
from core.word_task_runner import WordTaskRunner
from settings import AppSettings
from tests import test_word_resume as word_helpers
from tests.app_data_isolation import IsolatedAppDataTestCase

try:  # pragma: no cover - 只有缺少发布期注入的副本才走这里
    import core._embedded_key as _embedded_key

    if not hasattr(_embedded_key, "EMBEDDED_PRIVATE_KEY_B64"):
        from cryptography.hazmat.primitives import serialization as _serialization
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

        _embedded_key.EMBEDDED_PRIVATE_KEY_B64 = _base64.b64encode(
            X25519PrivateKey.generate().private_bytes(
                encoding=_serialization.Encoding.Raw,
                format=_serialization.PrivateFormat.Raw,
                encryption_algorithm=_serialization.NoEncryption(),
            )
        ).decode()
except Exception:  # noqa: BLE001 - 拿不到就按原样跑，相关用例会自己跳过
    pass

from fastapi.testclient import TestClient

CONTROL_TEXT = "内容控件里的字"
INSERTED_TEXT = "插入的一句话"
DELETED_TEXT = "删掉的一句话"
BODY_TEXT = "正文原文"
TRANSLATIONS = {
    BODY_TEXT: "Body text",
    CONTROL_TEXT: "Text inside the control",
    INSERTED_TEXT: "An inserted sentence",
    DELETED_TEXT: "A deleted sentence",
}


def _append_xml(doc: Document, xml: str) -> None:
    """插到 sectPr 之前——Word 的正文块都在那里，body 尾部只能有 sectPr。"""
    doc.element.body.find(qn("w:sectPr")).addprevious(parse_xml(xml))


def _content_control_xml(text: str, *, toc: bool = False) -> str:
    marker = (
        "<w:sdtPr><w:docPartObj>"
        '<w:docPartGallery w:val="Table of Contents"/>'
        "</w:docPartObj></w:sdtPr>"
        if toc
        else "<w:sdtPr/>"
    )
    body = f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"
    if toc:
        body += (
            '<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
            '<w:r><w:instrText xml:space="preserve">TOC \\o "1-3" \\h \\u </w:instrText></w:r>'
            '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
            "<w:r><w:t>一、变更概况</w:t></w:r>"
            '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'
        )
    return f'<w:sdt {nsdecls("w")}>{marker}<w:sdtContent>{body}</w:sdtContent></w:sdt>'


def _revision_xml(*, inserted: str, deleted: str) -> str:
    return (
        f'<w:p {nsdecls("w")}><w:r><w:t xml:space="preserve">{BODY_TEXT} </w:t></w:r>'
        f'<w:ins w:id="11" w:author="张三" w:date="2026-01-02T03:04:05Z">'
        f"<w:r><w:t>{inserted}</w:t></w:r></w:ins>"
        f'<w:del w:id="12" w:author="张三" w:date="2026-01-02T03:04:05Z">'
        f"<w:r><w:delText>{deleted}</w:delText></w:r></w:del></w:p>"
    )


def build_document(
    path: Path,
    *,
    control: str | None = None,
    control_is_toc: bool = False,
    inserted: str | None = None,
    deleted: str | None = None,
    body: bool = True,
) -> None:
    doc = Document()
    if body:
        doc.add_paragraph(BODY_TEXT)
    if control:
        _append_xml(doc, _content_control_xml(control, toc=control_is_toc))
    if inserted or deleted:
        _append_xml(doc, _revision_xml(inserted=inserted or "", deleted=deleted or ""))
    doc.save(path)


def _document_xml(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        return archive.read("word/document.xml").decode("utf-8")


def _control_texts(path: Path) -> list[str]:
    """每个内容控件里各段的文字（按文档顺序）。"""
    import re

    xml = _document_xml(path)
    controls = re.findall(r"<w:sdt>.*?</w:sdt>", xml, re.S)
    return [
        [
            "".join(re.findall(r"<w:t(?:\s[^>]*)?>([^<]*)</w:t>", paragraph))
            for paragraph in re.findall(r"<w:p[ >].*?</w:p>", control, re.S)
        ]
        for control in controls
    ]


# 测试文档里那一段修改前、修改后的整句（_revision_xml：正文原文 + 插入 + 删除）。
ORIGINAL = f"{BODY_TEXT} {DELETED_TEXT}"
REVISED = f"{BODY_TEXT} {INSERTED_TEXT}"
TRANSLATIONS[ORIGINAL] = "Body text with a deleted sentence"
TRANSLATIONS[REVISED] = "Body text with an inserted sentence"


def _revision_paragraphs(path: Path) -> list:
    """含修订标记的段落（w:p），按文档顺序。"""
    body = Document(str(path)).element.body
    return [
        paragraph
        for paragraph in body.iter(qn("w:p"))
        if next(paragraph.iter(qn("w:ins"), qn("w:del")), None) is not None
    ]


def _view(paragraph, *, accept: bool) -> str:
    """在 Word 里接受（或拒绝）全部修订后这一段的文字。"""
    dropped = qn("w:del") if accept else qn("w:ins")
    pieces = []
    for node in paragraph.iter(qn("w:t"), qn("w:delText")):
        parent = node.getparent()
        while parent is not None and parent is not paragraph:
            if parent.tag == dropped:
                break
            parent = parent.getparent()
        else:
            pieces.append(node.text or "")
    return "".join(pieces).strip()


def _accepted(paragraph) -> str:
    return _view(paragraph, accept=True)


def _rejected(paragraph) -> str:
    return _view(paragraph, accept=False)


def _paragraph_texts(path: Path) -> list[str]:
    """全文每一段接受修订后的文字（含内容控件、表格里的段落）。"""
    return [
        _accepted(paragraph) for paragraph in Document(str(path)).element.body.iter(qn("w:p"))
    ]


def _paragraph_xml(paragraph) -> bytes:
    from lxml import etree

    return etree.tostring(paragraph)


class ContentControlTests(unittest.TestCase):
    """A. 内容控件里的文字永远翻译，不由用户选择。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_control_paragraphs_are_extracted_like_body_paragraphs(self) -> None:
        source = self.root / "control.docx"
        build_document(source, control=CONTROL_TEXT)

        segments = extract_word_segments(source, target_lang="en")

        by_source = {segment.source: segment for segment in segments}
        self.assertIn(CONTROL_TEXT, by_source)
        self.assertEqual(by_source[CONTROL_TEXT].kind, "content_control")
        self.assertEqual(
            by_source[CONTROL_TEXT].location, "content_control[0].paragraph[0]"
        )

    def test_control_text_is_translated_in_bilingual_output(self) -> None:
        source = self.root / "control.docx"
        build_document(source, control=CONTROL_TEXT)

        output = write_bilingual_docx(
            source_path=source,
            output_dir=self.root / "out",
            translations=TRANSLATIONS,
            target_lang="en",
        )

        self.assertEqual(_control_texts(output), [[CONTROL_TEXT, TRANSLATIONS[CONTROL_TEXT]]])
        # 控件本身还在：用户仍然在同一个表单格子里，内容控件没有被拆掉。
        self.assertIn("<w:sdt>", _document_xml(output))
        Document(str(output))  # 输出还能被 Word 打开

    def test_control_text_replaces_in_translation_only_mode(self) -> None:
        source = self.root / "control.docx"
        build_document(source, control=CONTROL_TEXT)

        output = write_bilingual_docx(
            source_path=source,
            output_dir=self.root / "only",
            translations=TRANSLATIONS,
            target_lang="en",
            output_translation_only=True,
        )

        self.assertEqual(_control_texts(output), [[TRANSLATIONS[CONTROL_TEXT]]])

    def test_toc_control_is_still_left_untouched(self) -> None:
        source = self.root / "toc.docx"
        build_document(source, control="目录", control_is_toc=True)

        segments = extract_word_segments(source, target_lang="en")
        self.assertNotIn("目录", [segment.source for segment in segments])

        output = write_bilingual_docx(
            source_path=source,
            output_dir=self.root / "toc-out",
            translations={"目录": "Contents", BODY_TEXT: TRANSLATIONS[BODY_TEXT]},
            target_lang="en",
        )
        self.assertIn("<w:instrText", _document_xml(output))
        # 目录控件里的每段文字都没动过（gallery 属性值里也有 "Contents"，不能按整份 XML 判）。
        self.assertEqual(
            [text.strip() for text in _control_texts(output)[0]],
            ["目录", "一、变更概况"],
        )

    def test_control_text_does_not_trigger_the_hidden_content_warning(self) -> None:
        source = self.root / "control.docx"
        build_document(source, control=CONTROL_TEXT)

        report = detect_hidden_word_content(source)

        self.assertEqual(report.content_control_count, 1)
        self.assertFalse(report.found)
        self.assertEqual(report.describe(), "")

    def test_untreated_only_fills_in_control_paragraphs(self) -> None:
        source = self.root / "control.docx"
        build_document(source, control=CONTROL_TEXT)

        plan = build_word_coverage_plan(source, target_lang="en")
        self.assertIn(
            ("content_control", CONTROL_TEXT),
            [(unit.kind, unit.source_text) for unit in plan.source_units],
        )

        output = write_untranslated_docx(
            source_path=source,
            output_dir=self.root / "fill",
            plan=plan,
            translations=TRANSLATIONS,
            target_lang="en",
        )
        self.assertEqual(_control_texts(output)[0][-1], TRANSLATIONS[CONTROL_TEXT])

    def test_control_paragraphs_are_not_part_of_story_segments(self) -> None:
        """文本框/脚注那一路不能把控件段落再抽一遍，否则同一句会被翻两次。"""
        source = self.root / "control.docx"
        build_document(source, control=CONTROL_TEXT)

        self.assertEqual(extract_word_story_segments(source, target_lang="en"), [])


class TrackedChangeScanTests(unittest.TestCase):
    """B. 扫描期就把「有几处未接受的修订」数出来。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_scan_counts_insertions_and_deletions(self) -> None:
        source = self.root / "revisions.docx"
        build_document(source, inserted=INSERTED_TEXT, deleted=DELETED_TEXT)

        result = scan_word_sources(self.root)

        item = next(item for item in result.items if item.path == source)
        self.assertEqual(item.tracked_change_count, 2)
        self.assertEqual(result.summary["tracked_change_count"], 2)
        self.assertEqual(result.summary["tracked_change_file_count"], 1)

    def test_file_without_revisions_reports_zero(self) -> None:
        source = self.root / "plain.docx"
        build_document(source, body=True)

        result = scan_word_sources(self.root)

        item = next(item for item in result.items if item.path == source)
        self.assertEqual(item.tracked_change_count, 0)
        self.assertEqual(result.summary["tracked_change_file_count"], 0)

    def test_paragraph_mark_insertions_do_not_count(self) -> None:
        """``w:pPr/w:rPr/w:ins`` 只标记段落标记本身，没有文字，不算一处修订。"""
        source = self.root / "mark.docx"
        doc = Document()
        doc.add_paragraph("Ordinary paragraph.")
        doc.paragraphs[0]._p.get_or_add_pPr().append(
            parse_xml(
                f'<w:rPr {nsdecls("w")}>'
                '<w:ins w:id="7" w:author="tester" w:date="2026-01-01T00:00:00Z"/>'
                "</w:rPr>"
            )
        )
        doc.save(source)

        result = scan_word_sources(self.root)
        self.assertEqual(result.items[0].tracked_change_count, 0)

    def test_revisions_inside_a_toc_control_do_not_count(self) -> None:
        source = self.root / "toc.docx"
        doc = Document()
        doc.add_paragraph(BODY_TEXT)
        _append_xml(
            doc,
            f'<w:sdt {nsdecls("w")}><w:sdtPr><w:docPartObj>'
            '<w:docPartGallery w:val="Table of Contents"/>'
            "</w:docPartObj></w:sdtPr><w:sdtContent>"
            "<w:p><w:r><w:t>目录</w:t></w:r></w:p>"
            f'<w:p><w:ins w:id="21" w:author="tester" w:date="2026-01-01T00:00:00Z">'
            "<w:r><w:t>目录条目</w:t></w:r></w:ins></w:p>"
            "</w:sdtContent></w:sdt>",
        )
        doc.save(source)

        result = scan_word_sources(self.root)
        self.assertEqual(result.items[0].tracked_change_count, 0)
        self.assertEqual(count_tracked_changes(Document(str(source)).element.body), 1)


class RevisionModeTests(unittest.TestCase):
    """C/D. skip 保原文、translate 换文字但保留修订标记。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _write(self, source: Path, *, mode: str, name: str) -> Path:
        return write_bilingual_docx(
            source_path=source,
            output_dir=self.root / name,
            translations=TRANSLATIONS,
            target_lang="en",
            revision_mode=mode,
            output_translation_only=True,
        )

    def test_default_mode_is_skip(self) -> None:
        self.assertEqual(normalize_revision_mode(None), REVISION_MODE_SKIP)
        self.assertEqual(normalize_revision_mode("bogus"), REVISION_MODE_SKIP)
        self.assertEqual(normalize_revision_mode("TRANSLATE"), REVISION_MODE_TRANSLATE)
        stats = RevisionWriteStats()
        self.assertEqual(stats.describe(), "跳过了 0 处修订（保持原文）")

    def test_skip_leaves_revision_text_untouched(self) -> None:
        source = self.root / "revisions.docx"
        build_document(source, inserted=INSERTED_TEXT, deleted=DELETED_TEXT)

        output = self._write(source, mode="skip", name="skip")
        xml = _document_xml(output)

        self.assertIn(INSERTED_TEXT, xml)
        self.assertIn(DELETED_TEXT, xml)
        self.assertNotIn(TRANSLATIONS[INSERTED_TEXT], xml)
        self.assertNotIn(TRANSLATIONS[DELETED_TEXT], xml)
        self.assertIn(TRANSLATIONS[BODY_TEXT], xml)

    def test_translate_in_translation_only_output_keeps_marks_in_place(self) -> None:
        source = self.root / "revisions.docx"
        build_document(source, inserted=INSERTED_TEXT, deleted=DELETED_TEXT)

        seen: list[RevisionWriteStats] = []
        output = write_bilingual_docx(
            source_path=source,
            output_dir=self.root / "translate",
            translations=TRANSLATIONS,
            target_lang="en",
            revision_mode="translate",
            output_translation_only=True,
            revision_callback=seen.append,
        )
        xml = _document_xml(output)

        # 原文（含修订里的原文）一个字都不剩，修改前 / 修改后都换成了整句译文。
        for text in (BODY_TEXT, INSERTED_TEXT, DELETED_TEXT):
            self.assertNotIn(text, xml)
        paragraph = _revision_paragraphs(output)[0]
        self.assertEqual(_accepted(paragraph), TRANSLATIONS[REVISED])
        self.assertEqual(_rejected(paragraph), TRANSLATIONS[ORIGINAL])
        # 修订标记沿用原来的作者和日期；删除的写在 w:delText 里，不能被写成正文 w:t。
        self.assertIn('w:author="张三"', xml)
        self.assertIn('w:date="2026-01-02T03:04:05Z"', xml)
        self.assertRegex(xml, r"<w:del [^>]*>.*?<w:delText[^>]*>[^<]*deleted")
        self.assertEqual(seen[0].mode, "translate")
        self.assertEqual(seen[0].translated_count, 2)
        self.assertEqual(seen[0].describe(), "翻译了 2 处修订（保留修订标记）")
        Document(str(output))

    def test_translate_in_bilingual_output_leaves_the_original_paragraph_alone(self) -> None:
        source = self.root / "revisions.docx"
        build_document(source, inserted=INSERTED_TEXT, deleted=DELETED_TEXT)
        original = _paragraph_xml(_revision_paragraphs(source)[0])

        output = write_bilingual_docx(
            source_path=source,
            output_dir=self.root / "bilingual",
            translations=TRANSLATIONS,
            target_lang="en",
            revision_mode="translate",
        )

        source_paragraph, translation = _revision_paragraphs(output)
        # 原文段落连同修订标记原样不动（maple 2026-11-01 定的：修订的译文放进译文段）。
        self.assertEqual(_paragraph_xml(source_paragraph), original)
        self.assertEqual(_accepted(translation), TRANSLATIONS[REVISED])
        self.assertEqual(_rejected(translation), TRANSLATIONS[ORIGINAL])
        for wrapper in translation.iter(qn("w:ins"), qn("w:del")):
            self.assertEqual(wrapper.get(qn("w:author")), "张三")
            self.assertEqual(wrapper.get(qn("w:date")), "2026-01-02T03:04:05Z")
            # 新标记有自己的编号，不和原文段里的 11 / 12 撞。
            self.assertNotIn(wrapper.get(qn("w:id")), {"11", "12"})
        # 修订以外那截「正文原文」不再单独译一遍、另起一段（"Body text" 只来自开头那段正文）。
        self.assertEqual(_paragraph_texts(output).count(TRANSLATIONS[BODY_TEXT]), 1)
        Document(str(output))

    def test_translate_marks_only_the_words_that_differ(self) -> None:
        source = self.root / "amount.docx"
        doc = Document()
        _append_xml(
            doc,
            f'<w:p {nsdecls("w")}><w:r><w:t>合同金额为</w:t></w:r>'
            '<w:del w:id="5" w:author="李四" w:date="2026-01-02T00:00:00Z">'
            "<w:r><w:delText>100</w:delText></w:r></w:del>"
            '<w:ins w:id="6" w:author="李四" w:date="2026-01-02T00:00:00Z">'
            "<w:r><w:t>120</w:t></w:r></w:ins><w:r><w:t>万元。</w:t></w:r></w:p>",
        )
        doc.save(source)
        translations = {
            "合同金额为100万元。": "The contract amount is RMB 1 million.",
            "合同金额为120万元。": "The contract amount is RMB 1.2 million.",
        }
        self.assertEqual(
            sorted(segment.source for segment in extract_word_segments(
                source, target_lang="en", revision_mode="translate"
            )),
            sorted(translations),
        )

        output = write_bilingual_docx(
            source_path=source,
            output_dir=self.root / "amount",
            translations=translations,
            target_lang="en",
            revision_mode="translate",
        )

        translation = _revision_paragraphs(output)[1]
        self.assertEqual(
            [node.text for node in translation.iter(qn("w:t"))],
            ["The contract amount is RMB 1", ".2", " million."],
        )
        self.assertEqual(_accepted(translation), translations["合同金额为120万元。"])
        self.assertEqual(_rejected(translation), translations["合同金额为100万元。"])

    def test_translate_mode_extracts_whole_sentences_only_in_translate_mode(self) -> None:
        source = self.root / "revisions.docx"
        build_document(source, inserted=INSERTED_TEXT, deleted=DELETED_TEXT)

        skip = extract_word_segments(source, target_lang="en")
        translated = extract_word_segments(source, target_lang="en", revision_mode="translate")

        # skip：照旧只送修订以外的文字。
        self.assertEqual([segment.source for segment in skip], [BODY_TEXT])
        # translate：修改前、修改后各一句整句，修订以外那截不再单独送。
        self.assertEqual(
            [(segment.source, segment.kind) for segment in translated],
            [(BODY_TEXT, "paragraph"), (ORIGINAL, "tracked_change"), (REVISED, "tracked_change")],
        )

    def test_both_control_and_revisions_together(self) -> None:
        source = self.root / "both.docx"
        build_document(source, control=CONTROL_TEXT, inserted=INSERTED_TEXT, deleted=DELETED_TEXT)

        output = self._write(source, mode="translate", name="both")

        self.assertEqual(
            _control_texts(output), [[TRANSLATIONS[CONTROL_TEXT]]]
        )
        paragraph = _revision_paragraphs(output)[0]
        self.assertEqual(_accepted(paragraph), TRANSLATIONS[REVISED])
        Document(str(output))

    def test_revision_inside_a_content_control_goes_to_the_translation_paragraph(self) -> None:
        source = self.root / "control-revision.docx"
        doc = Document()
        _append_xml(
            doc,
            f'<w:sdt {nsdecls("w")}><w:sdtPr/><w:sdtContent>'
            + _revision_xml(inserted=INSERTED_TEXT, deleted=DELETED_TEXT).replace(
                f' {nsdecls("w")}', ""
            )
            + "</w:sdtContent></w:sdt>",
        )
        doc.save(source)

        output = write_bilingual_docx(
            source_path=source,
            output_dir=self.root / "control-revision",
            translations=TRANSLATIONS,
            target_lang="en",
            revision_mode="translate",
        )

        control = next(Document(str(output)).element.body.iter(qn("w:sdt")))
        paragraphs = list(control.iter(qn("w:p")))
        self.assertEqual(len(paragraphs), 2)
        self.assertEqual(_rejected(paragraphs[0]), ORIGINAL)
        self.assertEqual(_accepted(paragraphs[1]), TRANSLATIONS[REVISED])
        self.assertNotIn(TRANSLATIONS[BODY_TEXT], _paragraph_texts(output))

    def test_revision_in_a_table_cell_is_translated_per_paragraph(self) -> None:
        source = self.root / "cell.docx"
        doc = Document()
        cell = doc.add_table(rows=1, cols=1).cell(0, 0)
        cell.paragraphs[0].add_run("单元格第一行")
        cell._tc.append(parse_xml(_revision_xml(inserted=INSERTED_TEXT, deleted=DELETED_TEXT)))
        doc.save(source)
        translations = {**TRANSLATIONS, "单元格第一行": "First line of the cell"}

        output = write_bilingual_docx(
            source_path=source,
            output_dir=self.root / "cell",
            translations=translations,
            target_lang="en",
            revision_mode="translate",
        )

        tc = next(Document(str(output)).element.body.iter(qn("w:tc")))
        texts = [_accepted(paragraph) for paragraph in tc.iterchildren(qn("w:p"))]
        self.assertEqual(
            texts,
            ["单元格第一行", "First line of the cell", REVISED, TRANSLATIONS[REVISED]],
        )

    def test_translate_mode_also_works_for_fill_in_tasks(self) -> None:
        source = self.root / "revisions.docx"
        build_document(source, inserted=INSERTED_TEXT, deleted=DELETED_TEXT)
        plan = build_word_coverage_plan(source, target_lang="en")
        stories = extract_word_story_segments(source, target_lang="en", revision_mode="translate")
        self.assertEqual(
            sorted(segment.source for segment in stories), sorted([ORIGINAL, REVISED])
        )

        output = write_untranslated_docx(
            source_path=source,
            output_dir=self.root / "fill",
            plan=plan,
            translations=TRANSLATIONS,
            target_lang="en",
            revision_mode="translate",
        )

        source_paragraph, translation = _revision_paragraphs(output)
        self.assertEqual(_rejected(source_paragraph), ORIGINAL)
        self.assertEqual(_accepted(translation), TRANSLATIONS[REVISED])
        self.assertEqual(_rejected(translation), TRANSLATIONS[ORIGINAL])
        # 再补译一遍：译文已经在了，不再接一段。
        self.assertEqual(
            extract_word_story_segments(output, target_lang="en", revision_mode="translate"), []
        )
        again = write_untranslated_docx(
            source_path=output,
            output_dir=self.root / "fill-again",
            plan=build_word_coverage_plan(output, target_lang="en"),
            translations=TRANSLATIONS,
            target_lang="en",
            revision_mode="translate",
        )
        self.assertEqual(len(_revision_paragraphs(again)), 2)

    def test_bilingual_translate_output_is_not_reported_as_untranslated(self) -> None:
        source = self.root / "revisions.docx"
        build_document(source, control=CONTROL_TEXT, inserted=INSERTED_TEXT, deleted=DELETED_TEXT)

        output = write_bilingual_docx(
            source_path=source,
            output_dir=self.root / "check",
            translations=TRANSLATIONS,
            target_lang="en",
            revision_mode="translate",
        )

        self.assertEqual(build_word_coverage_plan(output, target_lang="en").source_units, [])


def _cover_page_xml(text: str) -> str:
    """Word「插入 → 封面」生成的那种封面：一个 docPartGallery 为 Cover Pages 的块级控件。"""
    return (
        f'<w:sdt {nsdecls("w")}><w:sdtPr><w:docPartObj>'
        '<w:docPartGallery w:val="Cover Pages"/><w:docPartUnique/>'
        f"</w:docPartObj></w:sdtPr><w:sdtContent><w:p><w:r><w:t>{text}</w:t></w:r></w:p>"
        "</w:sdtContent></w:sdt>"
    )


class ReviewFindingTests(unittest.TestCase):
    """独立审查查出的几处：成对判定、封面保护、控件里的文本框。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_bilingual_control_output_is_covered_not_untranslated(self) -> None:
        source = self.root / "control.docx"
        build_document(source, control=CONTROL_TEXT)
        output = write_bilingual_docx(
            source_path=source,
            output_dir=self.root / "out",
            translations=TRANSLATIONS,
            target_lang="en",
        )

        plan = build_word_coverage_plan(output, target_lang="en")

        self.assertEqual(plan.source_units, [])
        control_units = [unit for unit in plan.units if unit.kind == "content_control"]
        self.assertEqual(
            [(unit.status, unit.target_text) for unit in control_units],
            [("covered", TRANSLATIONS[CONTROL_TEXT])],
        )
        # 补译再跑：控件里的段落已经译过，不再送翻、也不再接一段。
        self.assertNotIn(CONTROL_TEXT, plan.source_texts)
        again = write_untranslated_docx(
            source_path=output,
            output_dir=self.root / "again",
            plan=plan,
            translations=TRANSLATIONS,
            target_lang="en",
        )
        self.assertEqual(_control_texts(again), [[CONTROL_TEXT, TRANSLATIONS[CONTROL_TEXT]]])

    def _cover_document(self) -> Path:
        source = self.root / "cover.docx"
        doc = Document()
        _append_xml(doc, _cover_page_xml("封面标题"))
        doc.add_heading("第一章 总则", level=1)
        doc.add_paragraph(BODY_TEXT)
        _append_xml(doc, _content_control_xml(CONTROL_TEXT))
        doc.save(source)
        return source

    def test_cover_page_control_is_protected_with_front_matter(self) -> None:
        source = self._cover_document()
        translations = {**TRANSLATIONS, "封面标题": "Cover title", "第一章 总则": "Chapter 1"}

        protected = [
            segment.source
            for segment in extract_word_segments(source, target_lang="en", protect_front_matter=True)
        ]
        output = write_bilingual_docx(
            source_path=source,
            output_dir=self.root / "protected",
            translations=translations,
            target_lang="en",
            protect_front_matter=True,
        )
        plan = build_word_coverage_plan(output, target_lang="en", protect_front_matter=True)

        self.assertNotIn("封面标题", protected)
        self.assertIn(CONTROL_TEXT, protected)
        # 封面控件原样不动，正文里的控件照常翻。
        self.assertEqual(
            _control_texts(output), [["封面标题"], [CONTROL_TEXT, TRANSLATIONS[CONTROL_TEXT]]]
        )
        self.assertEqual(plan.source_units, [])

    def test_cover_page_control_is_translated_without_protection(self) -> None:
        source = self._cover_document()

        sources = [segment.source for segment in extract_word_segments(source, target_lang="en")]

        self.assertIn("封面标题", sources)

    def test_textbox_inside_a_control_is_translated_once(self) -> None:
        from tests.test_word_embedded_text import _box_texts, _textbox_paragraph

        source = self.root / "control-textbox.docx"
        doc = Document()
        paragraph = _textbox_paragraph(["图例说明"], cx=2286000, cy=914400)
        _append_xml(
            doc,
            f'<w:sdt {nsdecls("w")}><w:sdtPr/><w:sdtContent>{paragraph}</w:sdtContent></w:sdt>',
        )
        doc.save(source)
        translations = {"正文原文": "Body text", "图例说明": "Legend"}

        output = write_bilingual_docx(
            source_path=source,
            output_dir=self.root / "out",
            translations=translations,
            target_lang="en",
        )
        xml = _document_xml(output)

        # 文本框（正本和 VML 备份）各自只多出一段译文；控件那一趟不再往框里或框外再写一份。
        self.assertEqual(_box_texts(xml), [["图例说明", "Legend"], ["图例说明", "Legend"]])
        self.assertEqual(xml.count("Legend"), 2)

    def test_textbox_revision_is_counted_once(self) -> None:
        from tests.test_word_embedded_text import _textbox_paragraph

        source = self.root / "textbox-revision.docx"
        doc = Document()
        paragraph = parse_xml(_textbox_paragraph(["图例说明"], cx=2286000, cy=914400))
        for box in paragraph.iter(qn("w:txbxContent")):
            box.find(qn("w:p")).append(
                parse_xml(
                    f'<w:ins {nsdecls("w")} w:id="3" w:author="张三">'
                    f"<w:r><w:t>{INSERTED_TEXT}</w:t></w:r></w:ins>"
                )
            )
        doc.element.body.find(qn("w:sectPr")).addprevious(paragraph)
        doc.save(source)

        self.assertEqual(count_tracked_changes(Document(str(source)).element.body), 1)

    def test_ordinary_document_is_untouched_by_the_new_passes(self) -> None:
        """没有内容控件和修订的普通文档：translate 模式和 skip 模式写出来逐字节一样。"""
        source = self.root / "plain.docx"
        doc = Document()
        doc.add_heading("第一章 总则", level=1)
        doc.add_paragraph(BODY_TEXT)
        doc.add_table(rows=1, cols=1).cell(0, 0).text = "表格里的字"
        doc.save(source)
        translations = {**TRANSLATIONS, "第一章 总则": "Chapter 1", "表格里的字": "Text in a table"}

        outputs = [
            _document_xml(
                write_bilingual_docx(
                    source_path=source,
                    output_dir=self.root / mode,
                    translations=translations,
                    target_lang="en",
                    revision_mode=mode,
                )
            )
            for mode in ("skip", "translate")
        ]

        self.assertEqual(outputs[0], outputs[1])
        self.assertIn("Text in a table", outputs[0])


_DRAWING = (
    '<w:r><w:drawing><wp:inline xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing">'
    '<wp:extent cx="1" cy="1"/><wp:docPr id="9" name="图片 1"/></wp:inline></w:drawing></w:r>'
)


class SecondReviewTests(unittest.TestCase):
    """第二轮审查：修订里的图片、多人叠加的修订、编号撞号、绑定控件。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _source(self, xml: str) -> Path:
        source = self.root / "source.docx"
        doc = Document()
        _append_xml(doc, xml)
        doc.save(source)
        return source

    def test_image_inside_an_insertion_survives_translation_only_output(self) -> None:
        source = self._source(
            f'<w:p {nsdecls("w")}><w:r><w:t xml:space="preserve">{BODY_TEXT} </w:t></w:r>'
            f'<w:ins w:id="11" w:author="张三"><w:r><w:t>{INSERTED_TEXT}</w:t></w:r>{_DRAWING}</w:ins></w:p>'
        )
        output = write_bilingual_docx(
            source_path=source,
            output_dir=self.root / "out",
            translations={**TRANSLATIONS, REVISED: "Body text with an inserted sentence"},
            target_lang="en",
            revision_mode="translate",
            output_translation_only=True,
        )

        paragraph = _revision_paragraphs(output)[0]
        drawings = list(paragraph.iter(qn("w:drawing")))
        self.assertEqual(len(drawings), 1)
        # 图片还在原来那层插入修订里：拒绝全部时它照旧消失。
        self.assertEqual(drawings[0].getparent().getparent().tag, qn("w:ins"))
        self.assertNotIn(INSERTED_TEXT, _document_xml(output))

    def test_deleted_insertion_is_in_neither_version(self) -> None:
        from core.word_revisions import paragraph_versions

        source = self._source(
            f'<w:p {nsdecls("w")}><w:r><w:t>甲方应在</w:t></w:r>'
            '<w:ins w:id="1" w:author="甲"><w:del w:id="2" w:author="乙">'
            "<w:r><w:delText>三十日内</w:delText></w:r></w:del></w:ins>"
            "<w:r><w:t>付款。</w:t></w:r></w:p>"
        )
        paragraph = _revision_paragraphs(source)[0]

        self.assertEqual(paragraph_versions(paragraph), ("甲方应在付款。", "甲方应在付款。"))

    def test_revision_ids_stay_unique_for_an_inserted_paragraph(self) -> None:
        source = self._source(
            f'<w:p {nsdecls("w")}><w:pPr><w:rPr><w:ins w:id="7" w:author="张三"/></w:rPr></w:pPr>'
            f'<w:ins w:id="8" w:author="张三"><w:r><w:t>{INSERTED_TEXT}</w:t></w:r></w:ins></w:p>'
        )
        output = write_bilingual_docx(
            source_path=source,
            output_dir=self.root / "out",
            translations=TRANSLATIONS,
            target_lang="en",
            revision_mode="translate",
        )

        body = Document(str(output)).element.body
        ids = [
            element.get(qn("w:id"))
            for element in body.iter(qn("w:ins"), qn("w:del"))
        ]
        self.assertEqual(len(ids), len(set(ids)), ids)
        self.assertIn(TRANSLATIONS[INSERTED_TEXT], _paragraph_texts(output))

    def test_bound_and_placeholder_controls_are_left_alone(self) -> None:
        source = self.root / "bound.docx"
        doc = Document()
        _append_xml(
            doc,
            f'<w:sdt {nsdecls("w")}><w:sdtPr><w:dataBinding w:xpath="/ns0:coreProperties[1]/ns1:title[1]" '
            'w:storeItemID="{6C3C8BC8-F283-45AE-878A-BAB7291924A1}"/></w:sdtPr>'
            "<w:sdtContent><w:p><w:r><w:t>封面标题</w:t></w:r></w:p></w:sdtContent></w:sdt>",
        )
        _append_xml(
            doc,
            f'<w:sdt {nsdecls("w")}><w:sdtPr><w:showingPlcHdr/></w:sdtPr>'
            "<w:sdtContent><w:p><w:r><w:t>单击此处输入文字。</w:t></w:r></w:p></w:sdtContent></w:sdt>",
        )
        _append_xml(doc, _content_control_xml(CONTROL_TEXT))
        doc.save(source)

        sources = [segment.source for segment in extract_word_segments(source, target_lang="en")]

        self.assertNotIn("封面标题", sources)
        self.assertNotIn("单击此处输入文字。", sources)
        self.assertIn(CONTROL_TEXT, sources)


class RunnerRevisionRecordTests(IsolatedAppDataTestCase):
    """D + 状态⑤：任务记录里写明这份文件对修订是怎么处理的。"""

    def _run(self, source: Path, root: Path, *, revision_mode: str) -> DoneMsg:
        runner = WordTaskRunner(
            [WordFileItem(path=source, name=source.name, size_kb=1.0)],
            AppSettings(source_lang="zh", target_lang="en"),
            source_root=root,
            revision_mode=revision_mode,
        )
        with ExitStack() as stack:
            word_helpers.WordResumeTests._runner_patches(self, stack, root=root)
            runner._run()
        return next(
            message for message in runner._queue.queue if isinstance(message, DoneMsg)
        )

    def test_full_run_with_a_control_reports_no_untranslated_paragraph(self) -> None:
        """成品体检不打桩：双语输出里控件的原文段后面跟着译文，不能报「未翻译」。"""
        from core.word_quality_report import _append_post_write_coverage_issues

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "control.docx"
            build_document(source, control=CONTROL_TEXT)
            runner = WordTaskRunner(
                [WordFileItem(path=source, name=source.name, size_kb=1.0)],
                AppSettings(source_lang="zh", target_lang="en"),
                source_root=root,
            )
            with ExitStack() as stack:
                word_helpers.WordResumeTests._runner_patches(self, stack, root=root)
                stack.enter_context(
                    patch(
                        "core.word_task_runner._append_post_write_coverage_issues",
                        side_effect=_append_post_write_coverage_issues,
                    )
                )
                runner._run()
            done = next(
                message for message in runner._queue.queue if isinstance(message, DoneMsg)
            )

            result = done.file_results[0]
            self.assertTrue(result["success"], result)
            self.assertEqual(_control_texts(Path(result["output"])), [[CONTROL_TEXT, "TM translation"]])
            self.assertEqual(
                [issue for issue in result["issues"] if "content_control" in str(issue.get("location"))],
                [],
            )

    def test_skip_is_recorded_on_the_file_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "revisions.docx"
            build_document(source, inserted=INSERTED_TEXT, deleted=DELETED_TEXT)

            done = self._run(source, root, revision_mode="skip")

            result = done.file_results[0]
            self.assertTrue(result["success"], result)
            self.assertEqual(result["revisions"]["mode"], "skip")
            self.assertEqual(result["revisions"]["count"], 2)
            self.assertEqual(result["revisions"]["label"], "跳过了 2 处修订（保持原文）")

    def test_translate_is_recorded_on_the_file_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "revisions.docx"
            build_document(source, inserted=INSERTED_TEXT, deleted=DELETED_TEXT)

            done = self._run(source, root, revision_mode="translate")

            result = done.file_results[0]
            self.assertTrue(result["success"], result)
            self.assertEqual(result["revisions"]["mode"], "translate")
            self.assertEqual(result["revisions"]["count"], 2)
            self.assertEqual(
                result["revisions"]["label"], "翻译了 2 处修订（保留修订标记）"
            )
            # 这里走的是记忆库替身（每句都给 "TM translation"）：验证的是原文段落和修订
            # 标记原样留着、译文段写了出来，具体译法由别的用例覆盖。
            # 替身给修改前、修改后同一句译文，两句一样，译文段里也就没有要标的差别。
            source_paragraph = _revision_paragraphs(Path(result["output"]))[0]
            self.assertEqual(_rejected(source_paragraph), ORIGINAL)
            self.assertEqual(_accepted(source_paragraph.getnext()), "TM translation")


if __name__ == "__main__":
    unittest.main(verbosity=2)


def _app_factory():
    """按需导入 FastAPI 应用。

    仓库里的开发版 ``core/_embedded_key.py`` 只有 ``EMBEDDED_KEY_ID``，发布期才会由
    scripts/inject_embedded_key.py 补上 ``EMBEDDED_PRIVATE_KEY_B64``；缺这个常量时
    ``api.app`` 根本导不进来，整个 API 层都测不了。那种环境里跳过，而不是让这一组
    契约测试在开发副本上永远红着。
    """
    try:
        from api.app import create_app
    except ImportError as exc:  # pragma: no cover - 只发生在缺少发布期注入的开发副本
        raise unittest.SkipTest(f"api.app 在当前副本里导入不了：{exc}")
    return create_app


class RevisionModeApiTests(IsolatedAppDataTestCase):
    """API 契约：扫描结果带 tracked_change_count，启动载荷带可选的 revision_mode。"""

    def test_scan_reports_tracked_change_count_per_file_and_in_summary(self) -> None:
        create_app = _app_factory()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with_revisions = root / "revisions.docx"
            plain = root / "plain.docx"
            build_document(with_revisions, inserted=INSERTED_TEXT, deleted=DELETED_TEXT)
            build_document(plain)

            with TestClient(create_app()) as client:
                response = client.post(
                    "/api/sources/scan", json={"surface": "word", "path": str(root)}
                )

            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()
            by_name = {item["name"]: item for item in payload["items"]}
            self.assertEqual(by_name["revisions"]["tracked_change_count"], 2)
            self.assertEqual(by_name["plain"]["tracked_change_count"], 0)
            self.assertEqual(payload["summary"]["tracked_change_file_count"], 1)
            self.assertEqual(payload["summary"]["tracked_change_count"], 2)

    def test_start_payload_carries_revision_mode_and_defaults_to_skip(self) -> None:
        from api.task_manager import TaskOptions
        from api.schemas import TaskStartRequest

        # 请求字段 → TaskOptions 的那一步是纯数据搬运（api/routes/tasks.py 的
        # _task_options 按同名字段取值），这里直接钉住两端的契约，不启动真任务。
        explicit = TaskStartRequest(
            surface="word", source_path="/tmp/x.docx", revision_mode="translate"
        )
        self.assertEqual(explicit.revision_mode, "translate")
        self.assertEqual(TaskOptions(revision_mode=explicit.revision_mode).revision_mode, "translate")

        default = TaskStartRequest(surface="word", source_path="/tmp/x.docx")
        self.assertEqual(default.revision_mode, "skip")
        self.assertNotIn("revision_mode", default.model_fields_set)
        self.assertEqual(TaskOptions().revision_mode, "skip")

    def test_surface_forwards_revision_mode_to_the_runner(self) -> None:
        from api.surfaces import get_surface
        from api.surfaces.base import RunnerRequest
        from api.task_contracts import TaskOptions

        WordSurface = type(get_surface("word"))
        from settings import AppSettings

        captured: dict[str, object] = {}

        class _Recorder:
            def __init__(self, *args, **kwargs):  # noqa: ARG002 - 只记 keyword
                captured.update(kwargs)

        with patch("api.surfaces.word.WordTaskRunner", _Recorder):
            surface = WordSurface()
            surface.build_runner(
                RunnerRequest(
                    files=[],
                    settings=AppSettings(),
                    source_root=Path("/tmp"),
                    source_lang="zh",
                    key_overrides={},
                    api_schedulers={},
                    options=TaskOptions(revision_mode="translate"),
                )
            )

        self.assertEqual(captured["revision_mode"], "translate")

    def test_unknown_revision_mode_is_rejected(self) -> None:
        """取值只认 skip / translate：拼错的值不该被悄悄当成默认值。"""
        from api.schemas import TaskStartRequest
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            TaskStartRequest(
                surface="word", source_path="/tmp/x.docx", revision_mode="delete"
            )
