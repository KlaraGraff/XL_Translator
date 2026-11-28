"""Word「修订一起翻译」的三处遗留问题。

1. 表格里带修订的格子，续翻时把已经译过的内容又送去翻一遍：
   - 修改前、修改后两句译文差得多时，译文段整段都在 ``w:ins`` / ``w:del`` 里，
     python-docx 读出来是空段，补译计划把这一格认成「只有原文」；
   - 格子只译了一部分（上次中途停下、某段没译成）时，补译计划把整格（连同已经译好
     的段）重新送翻，而这一格的写回本来归修订那一路，送出去的译文根本用不上。
2. 双语版式往脚注 / 尾注里插译文段时，复制过去的段落标记修订带着原来的编号，
   同一个编号在文件里出现两次。
3. 目录段落（自动目录的条目）带着修订时被当成修订段落翻掉了；不带修订时它们本来
   就原样保留。

文档都是真 .docx：python-docx 建骨架，lxml 塞修订、域和脚注部件。
"""

from __future__ import annotations

import re
import tempfile
import zipfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.opc.packuri import PackURI
from docx.opc.part import Part
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls, qn

from core.task_runner import DoneMsg, LogMsg
from core.word_coverage import build_word_coverage_plan
from core.word_document import (
    WordFileItem,
    extract_word_segments,
    extract_word_story_segments,
    write_bilingual_docx,
)
from core.word_revisions import REVISION_MODE_SKIP, REVISION_MODE_TRANSLATE, paragraph_versions
from core.word_task_runner import WordTaskRunner
from settings import AppSettings
from tests import test_word_resume as word_helpers
from tests.app_data_isolation import IsolatedAppDataTestCase

W = nsdecls("w")
STAMP = 'w:author="甲" w:date="2026-01-02T03:04:05Z"'

PLAIN = "付款方式为银行转账"
NOTE = "验收标准第一条说明"
PERIOD_BEFORE = "工期为九十天，自开工之日起算。"
PERIOD_AFTER = "工期为一百天，自开工之日起算。"
WARRANTY_BEFORE = "质保期为一年，期满后退还保证金。"
WARRANTY_AFTER = "质保期为两年，期满后退还保证金。"
# 修改前、修改后两句译文故意写得差很多：逐词对比标不出来，译文段整段是一删一增，
# 段落里没有一个普通（不在修订里）的字。
TRANSLATIONS = {
    PLAIN: "Payment by bank transfer",
    NOTE: "Note on the first acceptance criterion",
    PERIOD_BEFORE: "Ninety days from commencement",
    PERIOD_AFTER: "The contract period runs one hundred days",
    WARRANTY_BEFORE: "One-year warranty, deposit returned afterwards",
    WARRANTY_AFTER: "Two years of warranty before the bond is released",
}


def _revision_paragraph(before: str, after: str, rid: int) -> str:
    """一段话：前缀相同，中间一处「删 → 增」，后缀相同。"""
    prefix = 0
    while prefix < min(len(before), len(after)) and before[prefix] == after[prefix]:
        prefix += 1
    suffix = 0
    while (
        suffix < min(len(before), len(after)) - prefix
        and before[-1 - suffix] == after[-1 - suffix]
    ):
        suffix += 1
    head, tail = before[:prefix], before[len(before) - suffix :]
    deleted, inserted = before[prefix : len(before) - suffix], after[prefix : len(after) - suffix]
    return (
        f'<w:p {W}><w:r><w:t xml:space="preserve">{head}</w:t></w:r>'
        f'<w:del w:id="{rid}" {STAMP}><w:r><w:delText>{deleted}</w:delText></w:r></w:del>'
        f'<w:ins w:id="{rid + 1}" {STAMP}><w:r><w:t>{inserted}</w:t></w:r></w:ins>'
        f'<w:r><w:t xml:space="preserve">{tail}</w:t></w:r></w:p>'
    )


def _build_table_document(path: Path) -> None:
    """一张 1×3 的表：只有修订的格、普通段 + 修订段的格、普通格。"""
    doc = Document()
    doc.add_paragraph("以上条款自签字之日起生效。")
    table = doc.add_table(rows=1, cols=3)
    only_revision = table.cell(0, 0)._tc
    only_revision.remove(only_revision.find(qn("w:p")))
    only_revision.append(parse_xml(_revision_paragraph(PERIOD_BEFORE, PERIOD_AFTER, 101)))
    mixed = table.cell(0, 1)
    mixed.paragraphs[0].add_run(NOTE)
    mixed._tc.append(parse_xml(_revision_paragraph(WARRANTY_BEFORE, WARRANTY_AFTER, 201)))
    table.cell(0, 2).paragraphs[0].add_run(PLAIN)
    doc.save(path)


def _cell_versions(path: Path) -> list[list[tuple[str, str]]]:
    """每一格各段（修改前, 修改后）的文字。"""
    table = Document(str(path)).tables[0]
    return [
        [paragraph_versions(paragraph) for paragraph in tc.iterchildren(qn("w:p"))]
        for tc in table._tbl.iter(qn("w:tc"))
    ]


TRANSLATED_CELLS = [
    [
        (PERIOD_BEFORE, PERIOD_AFTER),
        (TRANSLATIONS[PERIOD_BEFORE], TRANSLATIONS[PERIOD_AFTER]),
    ],
    [
        (NOTE, NOTE),
        (TRANSLATIONS[NOTE], TRANSLATIONS[NOTE]),
        (WARRANTY_BEFORE, WARRANTY_AFTER),
        (TRANSLATIONS[WARRANTY_BEFORE], TRANSLATIONS[WARRANTY_AFTER]),
    ],
    [(PLAIN, PLAIN), (TRANSLATIONS[PLAIN], TRANSLATIONS[PLAIN])],
]


class _RecordingEngine:
    engine_name = "record/mock"

    def __init__(self) -> None:
        self.sent: list[str] = []

    def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):  # noqa: ARG002
        self.sent.extend(texts)
        return {text: TRANSLATIONS.get(text, f"Translation of item {len(self.sent)}") for text in texts}


class RevisionCellResumeTests(IsolatedAppDataTestCase):
    """问题 1：续翻时，带修订的表格格子里已经译过的内容不能再送翻。"""

    def _baseline(self, root: Path, translations: dict[str, str]) -> tuple[Path, Path]:
        source = root / "contract.docx"
        _build_table_document(source)
        resume_dir = root / "previous"
        written = write_bilingual_docx(
            source_path=source,
            output_dir=resume_dir,
            translations={**translations, "以上条款自签字之日起生效。": "Effective upon signature"},
            target_lang="en",
            revision_mode=REVISION_MODE_TRANSLATE,
        )
        self.assertEqual(written.name, word_helpers.WordResumeTests._bilingual_name(source.name, "en"))
        return source, written

    def _resume(self, root: Path, source: Path) -> tuple[WordTaskRunner, _RecordingEngine, DoneMsg]:
        engine = _RecordingEngine()
        runner = WordTaskRunner(
            [WordFileItem(path=source, name=source.name, size_kb=1.0, relative_path=source.name)],
            AppSettings(source_lang="zh", target_lang="en"),
            source_root=root,
            untranslated_only=True,
            resume_output_dir=str(root / "previous"),
            revision_mode=REVISION_MODE_TRANSLATE,
        )
        with ExitStack() as stack:
            word_helpers.WordResumeTests._runner_patches(self, stack, root=root)
            # 这一趟记忆库里什么都没有：送出去的每一句都会落到模型上。
            stack.enter_context(patch("core.word_task_runner.tm_manager.lookup_batch", return_value={}))
            stack.enter_context(patch("core.word_task_runner.build_engine", return_value=engine))
            runner._run()
        done = next(message for message in runner._queue.queue if isinstance(message, DoneMsg))
        self.assertTrue(done.file_results[0]["success"], done.file_results)
        return runner, engine, done

    @staticmethod
    def _logs(runner: WordTaskRunner) -> str:
        return "\n".join(
            message.message for message in runner._queue.queue if isinstance(message, LogMsg)
        )

    def test_translated_revision_cell_reads_as_covered(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _source, baseline = self._baseline(Path(temporary), TRANSLATIONS)
            self.assertEqual(_cell_versions(baseline), TRANSLATED_CELLS)
            # 以前：只有修订的那一格，译文段整段都在修订里，读出来是空段，
            # 这一格被认成「只有原文」——续翻提示里数得到它，续翻时又送去翻一遍。
            plan = build_word_coverage_plan(baseline, target_lang="en")
            self.assertEqual(plan.source_units, [])

    def test_resuming_a_finished_file_sends_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _baseline = self._baseline(root, TRANSLATIONS)

            runner, engine, done = self._resume(root, source)

            self.assertEqual(engine.sent, [])
            self.assertIn("在上次翻译结果的基础上补译", self._logs(runner))
            self.assertEqual(_cell_versions(Path(done.file_results[0]["output"])), TRANSLATED_CELLS)

    def test_resuming_a_half_translated_cell_sends_only_the_missing_paragraph(self) -> None:
        """上次这一格里普通段译好了、修订段没译成：续翻只补修订段，不重翻普通段。"""
        missing = {WARRANTY_BEFORE, WARRANTY_AFTER}
        partial = {key: value for key, value in TRANSLATIONS.items() if key not in missing}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, baseline = self._baseline(root, partial)
            self.assertEqual(
                _cell_versions(baseline)[1],
                [(NOTE, NOTE), (TRANSLATIONS[NOTE], TRANSLATIONS[NOTE]), (WARRANTY_BEFORE, WARRANTY_AFTER)],
            )

            runner, engine, done = self._resume(root, source)

            # 以前：底稿核对认不出这一格，整份放弃底稿重翻；就算用上了底稿，整格
            # 「验收标准…\n质保期为…」也会再送一次（这一格的写回归修订那一路，那句译文用不上）。
            self.assertIn("在上次翻译结果的基础上补译", self._logs(runner))
            self.assertEqual(sorted(engine.sent), sorted(missing))
            self.assertEqual(_cell_versions(Path(done.file_results[0]["output"])), TRANSLATED_CELLS)

    def test_skip_mode_resume_still_fills_revision_cells_the_ordinary_way(self) -> None:
        """「跳过修订」时带修订的格子归常规写回：补译计划照旧把它列进待补。"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "contract.docx"
            _build_table_document(source)
            plan = build_word_coverage_plan(source, target_lang="en", revision_mode=REVISION_MODE_SKIP)
            self.assertIn(
                "工期为天，自开工之日起算。",
                [unit.source_text for unit in plan.source_units],
            )
            owned = build_word_coverage_plan(source, target_lang="en", revision_mode=REVISION_MODE_TRANSLATE)
            self.assertNotIn(
                "工期为天，自开工之日起算。",
                [unit.source_text for unit in owned.source_units],
            )
            self.assertIn(PLAIN, [unit.source_text for unit in owned.source_units])


def _package_revision_ids(path: Path) -> list[tuple[str, str]]:
    """整个文件里所有修订标记的 (部件, 编号)。"""
    tags = "ins|del|moveFrom|moveTo|pPrChange|rPrChange|sectPrChange|tblPrChange|trPrChange|tcPrChange"
    found: list[tuple[str, str]] = []
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if name.startswith("word/") and name.endswith(".xml"):
                xml = archive.read(name).decode("utf-8")
                found.extend((name, value) for value in re.findall(rf'<w:(?:{tags})\b[^>]*w:id="(-?\d+)"', xml))
    return found


def _add_notes_part(doc, *, tag: str, name: str, rel_type: str, content_type: str, text: str, rid: int) -> None:
    xml = (
        f'<w:{tag}s {W}>'
        f'<w:{tag} w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:{tag}>'
        f'<w:{tag} w:type="continuationSeparator" w:id="0"><w:p><w:r><w:continuationSeparator/></w:r></w:p></w:{tag}>'
        f'<w:{tag} w:id="1"><w:p><w:pPr><w:rPr><w:ins w:id="{rid}" {STAMP}/></w:rPr></w:pPr>'
        f'<w:r><w:rPr><w:vertAlign w:val="superscript"/></w:rPr><w:{tag}Ref/></w:r>'
        f'<w:r><w:t xml:space="preserve"> {text}</w:t></w:r></w:p></w:{tag}>'
        f"</w:{tag}s>"
    ).encode()
    part = Part(PackURI(f"/word/{name}.xml"), content_type, xml, doc.part.package)
    doc.part.relate_to(part, rel_type)


class PackageRevisionIdTests(IsolatedAppDataTestCase):
    """问题 2：插进脚注、尾注的译文段复制了段落标记修订，编号不能和原段重复。"""

    def test_note_paragraph_marks_get_fresh_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "notes.docx"
            doc = Document()
            doc.element.body.find(qn("w:sectPr")).addprevious(
                parse_xml(
                    f'<w:p {W}><w:r><w:t>这一段带脚注和尾注</w:t></w:r>'
                    '<w:r><w:footnoteReference w:id="1"/></w:r><w:r><w:endnoteReference w:id="1"/></w:r></w:p>'
                )
            )
            _add_notes_part(
                doc,
                tag="footnote",
                name="footnotes",
                rel_type=RT.FOOTNOTES,
                content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml",
                text="脚注里新加的一句",
                rid=21,
            )
            _add_notes_part(
                doc,
                tag="endnote",
                name="endnotes",
                rel_type=RT.ENDNOTES,
                content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.endnotes+xml",
                text="尾注里新加的一句",
                rid=22,
            )
            doc.save(source)
            translations = {
                "这一段带脚注和尾注": "This paragraph has notes",
                "脚注里新加的一句": "A sentence added to the footnote",
                "尾注里新加的一句": "A sentence added to the endnote",
            }
            for mode in (REVISION_MODE_SKIP, REVISION_MODE_TRANSLATE):
                with self.subTest(mode=mode):
                    output = write_bilingual_docx(
                        source_path=source,
                        output_dir=root / mode,
                        translations=translations,
                        target_lang="en",
                        revision_mode=mode,
                    )
                    found = _package_revision_ids(output)
                    values = [value for _part, value in found]
                    # 原段一个、复制到译文段一个，各部件里都有。
                    self.assertEqual(len(found), 4, found)
                    self.assertEqual(len(set(values)), 4, f"修订编号重复：{found}")
                    # 原段的编号不动，换号的是插进去的译文段。
                    self.assertIn(("word/footnotes.xml", "21"), found)
                    self.assertIn(("word/endnotes.xml", "22"), found)


def _toc_entry(old: str, new: str, page: str, index: int, *, style: str | None, first: bool, last: bool) -> str:
    """自动目录里的一条：Word「更新目录」时开着修订，条目文字记成一删一增。"""
    begin = (
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        '<w:r><w:instrText xml:space="preserve"> TOC \\o "1-3" \\h \\z \\u </w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        if first
        else ""
    )
    end = '<w:r><w:fldChar w:fldCharType="end"/></w:r>' if last else ""
    style_xml = f'<w:pStyle w:val="{style}"/>' if style else ""
    return (
        f'<w:p {W}><w:pPr>{style_xml}<w:tabs><w:tab w:val="right" w:leader="dot" w:pos="8290"/></w:tabs></w:pPr>'
        f'{begin}<w:hyperlink w:anchor="_Toc{index}" w:history="1">'
        f'<w:del w:id="{900 + 2 * index}" {STAMP}><w:r><w:delText>{old}</w:delText></w:r></w:del>'
        f'<w:ins w:id="{901 + 2 * index}" {STAMP}><w:r><w:t>{new}</w:t></w:r></w:ins>'
        '<w:r><w:tab/></w:r><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        f'<w:r><w:instrText xml:space="preserve"> PAGEREF _Toc{index} \\h </w:instrText></w:r>'
        f'<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>{page}</w:t></w:r>'
        f'<w:r><w:fldChar w:fldCharType="end"/></w:r></w:hyperlink>{end}</w:p>'
    )


TOC_ENTRIES = [("第一章 概况", "第一章 工程概况", "1"), ("第二章 金额", "第二章 合同金额", "3")]


def _build_toc_document(path: Path, *, style: str | None, in_table: bool = False) -> None:
    doc = Document()
    entries = [
        _toc_entry(old, new, page, index, style=style, first=index == 0, last=index == len(TOC_ENTRIES) - 1)
        for index, (old, new, page) in enumerate(TOC_ENTRIES)
    ]
    if in_table:
        cell = doc.add_table(rows=1, cols=1).cell(0, 0)._tc
        cell.remove(cell.find(qn("w:p")))
        for entry in entries:
            cell.append(parse_xml(entry))
        cell.append(parse_xml(_revision_paragraph(PERIOD_BEFORE, PERIOD_AFTER, 101)))
    else:
        end = doc.element.body.find(qn("w:sectPr"))
        for entry in entries:
            end.addprevious(parse_xml(entry))
    doc.add_paragraph("第一章 工程概况", style="Heading 1")
    doc.add_paragraph("本工程位于城市中心。")
    doc.save(path)


def _toc_paragraph_xml(path: Path) -> list[bytes]:
    from lxml import etree

    body = Document(str(path)).element.body
    return [
        etree.tostring(paragraph)
        for paragraph in body.iter(qn("w:p"))
        if paragraph.find(f"{qn('w:hyperlink')}") is not None
    ]


class TocWithRevisionsTests(IsolatedAppDataTestCase):
    """问题 3：带修订的目录条目照样原样保留，和不带修订时一样。"""

    def test_toc_entries_with_revisions_are_left_alone(self) -> None:
        body_translations = {
            "第一章 工程概况": "Chapter 1 Project overview",
            "本工程位于城市中心。": "The project is in the city centre.",
            PERIOD_BEFORE: TRANSLATIONS[PERIOD_BEFORE],
            PERIOD_AFTER: TRANSLATIONS[PERIOD_AFTER],
        }
        cases = [("TOC1", False), (None, False), ("TOC1", True)]
        for style, in_table in cases:
            with self.subTest(style=style, in_table=in_table), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "toc.docx"
                _build_toc_document(source, style=style, in_table=in_table)
                original = _toc_paragraph_xml(source)

                segments = extract_word_segments(source, target_lang="en", revision_mode=REVISION_MODE_TRANSLATE)
                sources = [segment.source for segment in segments]
                for old, new, page in TOC_ENTRIES:
                    # 以前：目录条目修改前、修改后各当一句整句送翻（「第一章 概况\t1」）。
                    self.assertNotIn(f"{old}\t{page}", sources)
                    self.assertNotIn(f"{new}\t{page}", sources)
                self.assertIn("本工程位于城市中心。", sources)
                if in_table:
                    self.assertIn(PERIOD_AFTER, sources)

                for only in (False, True):
                    output = write_bilingual_docx(
                        source_path=source,
                        output_dir=root / f"out-{only}",
                        translations=body_translations,
                        target_lang="en",
                        revision_mode=REVISION_MODE_TRANSLATE,
                        output_translation_only=only,
                    )
                    self.assertEqual(_toc_paragraph_xml(output), original, f"only={only}")
                    texts = [paragraph_versions(p)[1] for p in Document(str(output)).element.body.iter(qn("w:p"))]
                    self.assertIn("The project is in the city centre.", texts)
                    if in_table:
                        self.assertIn(TRANSLATIONS[PERIOD_AFTER], texts)

                # 续翻：目录条目也不算「待补」。
                story = extract_word_story_segments(
                    source, target_lang="en", revision_mode=REVISION_MODE_TRANSLATE
                )
                self.assertFalse(any("概况" in segment.source or "金额" in segment.source for segment in story))
