"""Word 段内局部格式：标记生成、译文标记校验、按标记写回、出错退回整段写法。

docx 都用 python-docx 真实构造（含 w:hyperlink），断言落在输出文件的 run 上。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from core.language_preflight import TranslationLanguageResult
from core.word_document import extract_word_segments, write_bilingual_docx
from core.word_inline_marks import (
    MARK_PROMPT_RULE,
    InlineMarkEngine,
    marks_match,
    paragraph_marks,
    parse_marked,
    place_marks,
    strip_marks,
)
from engines.base_engine import engine_supports_chat


def _bold_label_paragraph(doc, label="注意：", body="施工前必须完成技术交底。"):
    paragraph = doc.add_paragraph()
    paragraph.add_run(label).bold = True
    paragraph.add_run(body)
    return paragraph


def _add_hyperlink(paragraph, text: str, url: str):
    r_id = paragraph.part.relate_to(url, RT.HYPERLINK, is_external=True)
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), r_id)
    run = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.text = text
    run.append(t)
    link.append(run)
    paragraph._p.append(link)


def _runs(paragraph):
    """[(文字, 加粗, 斜体, 是否在链接里)]"""
    out = []
    for node in paragraph._p.iter(qn("w:r")):
        text = "".join(t.text or "" for t in node.iter(qn("w:t")))
        if not text:
            continue
        rpr = node.find(qn("w:rPr"))
        bold = rpr is not None and rpr.find(qn("w:b")) is not None
        italic = rpr is not None and rpr.find(qn("w:i")) is not None
        out.append((text, bold, italic, node.getparent().tag == qn("w:hyperlink")))
    return out


class ParagraphMarksTests(unittest.TestCase):
    def test_bold_label_is_marked_and_base_is_the_longer_run(self) -> None:
        marks = paragraph_marks(_bold_label_paragraph(Document()))
        self.assertEqual(marks.marked_text, "<g1>注意：</g1>施工前必须完成技术交底。")
        self.assertEqual(list(marks.groups), [1])
        self.assertIsNone(marks.base_rpr.find(qn("w:b")) if marks.base_rpr is not None else None)

    def test_uniform_paragraph_needs_no_marks(self) -> None:
        doc = Document()
        paragraph = doc.add_paragraph()
        paragraph.add_run("本工程位于")
        paragraph.add_run("卡萨布兰卡。")  # 格式相同、只是拆成两个 run
        marks = paragraph_marks(paragraph)
        self.assertEqual(marks.groups, {})
        self.assertEqual(marks.marked_text, "本工程位于卡萨布兰卡。")

    def test_font_name_and_language_differences_do_not_count(self) -> None:
        doc = Document()
        paragraph = doc.add_paragraph()
        paragraph.add_run("采用")
        latin = paragraph.add_run("C30")
        latin.font.name = "Times New Roman"
        paragraph.add_run("混凝土。")
        self.assertEqual(paragraph_marks(paragraph).groups, {})

    def test_middle_word_italic_and_hyperlink(self) -> None:
        doc = Document()
        paragraph = doc.add_paragraph()
        paragraph.add_run("详见")
        paragraph.add_run("技术规范").italic = True
        paragraph.add_run("和")
        _add_hyperlink(paragraph, "官网", "https://example.com")
        paragraph.add_run("。")
        marks = paragraph_marks(paragraph)
        self.assertEqual(marks.marked_text, "详见<g1>技术规范</g1>和<g2>官网</g2>。")
        self.assertIsNotNone(marks.groups[2].hyperlink)

    def test_whitespace_between_two_bold_words_joins_the_group(self) -> None:
        doc = Document()
        paragraph = doc.add_paragraph()
        paragraph.add_run("Very").bold = True
        paragraph.add_run(" ")
        paragraph.add_run("important").bold = True
        paragraph.add_run(" notice for all workers on site.")
        self.assertEqual(paragraph_marks(paragraph).marked_text, "<g1>Very important</g1> notice for all workers on site.")

    def test_complex_paragraphs_are_not_marked(self) -> None:
        doc = Document()
        tabbed = _bold_label_paragraph(doc)
        tabbed.runs[1].add_tab()
        literal = _bold_label_paragraph(doc, body="文中出现 <g1> 字样。")
        broken = _bold_label_paragraph(doc)
        broken.runs[1].add_break()
        broken.add_run("下一行")
        for paragraph in (tabbed, literal, broken):
            self.assertIsNone(paragraph_marks(paragraph))


class ParseMarkedTests(unittest.TestCase):
    def test_reordered_tags_are_accepted(self) -> None:
        pieces = parse_marked("Voir le <g2>site</g2> et les <g1>CCTP</g1>.", [1, 2])
        self.assertEqual(pieces, [("Voir le ", None), ("site", 2), (" et les ", None), ("CCTP", 1), (".", None)])

    def test_broken_tags_are_rejected(self) -> None:
        for text in (
            "Attention : travaux.",  # 标记丢了
            "<g1>Attention</g1> : <g1>travaux</g1>.",  # 重复
            "<g1>Attention : <g2>x</g2></g1>",  # 嵌套
            "</g1>Attention<g1>",  # 先闭后开
            "<g1>Attention",  # 没闭合
            "<g3>Attention</g3>",  # 多出来的组号
        ):
            with self.subTest(text=text):
                self.assertIsNone(parse_marked(text, [1] if "g2" not in text else [1]))

    def test_tolerates_spaces_inside_tags(self) -> None:
        self.assertEqual(parse_marked("< g1 >Attention :</ g1 > texte", [1]), [("Attention :", 1), (" texte", None)])
        self.assertEqual(strip_marks("< g1 >A</ g1 >"), "A")

    def test_cell_lines_must_pair_up(self) -> None:
        self.assertTrue(marks_match("<g1>注：</g1>甲\n乙", "<g1>Note :</g1> A\nB"))
        self.assertFalse(marks_match("<g1>注：</g1>甲\n乙", "<g1>Note :</g1> A B"))
        self.assertFalse(marks_match("<g1>注：</g1>甲\n乙", "Note : A\n<g1>B</g1>"))


class _FakeEngine:
    engine_name = "fake"

    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
        self.calls.append((list(texts), system_prompt))
        return {text: self.reply(text) for text in texts}

    def translate_batch_with_sources(self, texts, target_lang, system_prompt, source_lang="auto"):
        self.calls.append((list(texts), system_prompt))
        return [TranslationLanguageResult(source_text=t, translation=self.reply(t), source_lang="zh") for t in texts]

    def chat(self, system, user):
        return "ok"


class InlineMarkEngineTests(unittest.TestCase):
    MARKED = {"注意：施工前必须完成技术交底。": "<g1>注意：</g1>施工前必须完成技术交底。"}

    def test_marked_text_goes_out_and_plain_comes_back(self) -> None:
        inner = _FakeEngine(lambda t: "<g1>Attention :</g1> briefing obligatoire." if "<g1>" in t else "Autre.")
        engine = InlineMarkEngine(inner, self.MARKED)
        result = engine.translate_batch(["注意：施工前必须完成技术交底。", "其他。"], "fr", "SYS", source_lang="zh")
        self.assertEqual(result, {"注意：施工前必须完成技术交底。": "Attention : briefing obligatoire.", "其他。": "Autre."})
        sent, prompt = inner.calls[0]
        self.assertEqual(sent[0], "<g1>注意：</g1>施工前必须完成技术交底。")
        self.assertIn(MARK_PROMPT_RULE, prompt)
        self.assertEqual(
            engine.marked_translations["注意：施工前必须完成技术交底。"],
            ("<g1>注意：</g1>施工前必须完成技术交底。", "<g1>Attention :</g1> briefing obligatoire."),
        )

    def test_lost_tags_fall_back_and_forget_earlier_result(self) -> None:
        replies = iter(["<g1>Attention :</g1> x.", "Attention : y."])
        inner = _FakeEngine(lambda t: next(replies))
        engine = InlineMarkEngine(inner, self.MARKED)
        engine.translate_batch(["注意：施工前必须完成技术交底。"], "fr", "SYS")
        result = engine.translate_batch(["注意：施工前必须完成技术交底。"], "fr", "SYS")
        self.assertEqual(result["注意：施工前必须完成技术交底。"], "Attention : y.")
        self.assertNotIn("注意：施工前必须完成技术交底。", engine.marked_translations)

    def test_batches_without_marks_pass_through_untouched(self) -> None:
        inner = _FakeEngine(lambda t: "x")
        InlineMarkEngine(inner, self.MARKED).translate_batch(["其他。"], "fr", "SYS")
        self.assertEqual(inner.calls[0], (["其他。"], "SYS"))

    def test_source_language_protocol_and_capabilities(self) -> None:
        inner = _FakeEngine(lambda t: "<g1>Attention :</g1> x." if "<g1>" in t else "y")
        engine = InlineMarkEngine(inner, self.MARKED)
        results = engine.translate_batch_with_sources(["注意：施工前必须完成技术交底。"], "fr", "SYS")
        self.assertEqual(results[0].source_text, "注意：施工前必须完成技术交底。")
        self.assertEqual(results[0].translation, "Attention : x.")
        self.assertEqual(engine.engine_name, "fake")
        self.assertTrue(engine_supports_chat(engine) is engine_supports_chat(inner))


class _ChatEngine(_FakeEngine):
    def __init__(self, answers):
        super().__init__(lambda t: t)
        self.answers = answers
        self.prompts = []

    def chat(self, system, user):
        self.prompts.append((system, user))
        return self.answers


class PlaceMarksTests(unittest.TestCase):
    ITEMS = [
        ("注意：施工前完成交底。", "<g1>注意：</g1>施工前完成交底。", "Attention : briefing avant travaux."),
        ("严禁堆放。", "<g1>严禁</g1>堆放。", "Stockage interdit."),
        ("注：单位毫米。", "<g1>注：</g1>单位毫米。", "Note : unités en mm."),
    ]

    def test_only_answers_that_keep_the_final_text_are_used(self) -> None:
        engine = _ChatEngine(
            '["<g1>Attention :</g1> briefing avant travaux.", '
            '"Stockage <g1>strictement</g1> interdit.", '  # 改了译文
            '"Note : unités en mm."]'  # 没插标记
        )
        placed = place_marks(engine, self.ITEMS)
        self.assertEqual(placed, {"注意：施工前完成交底。": ("<g1>注意：</g1>施工前完成交底。", "<g1>Attention :</g1> briefing avant travaux.")})
        self.assertIn("原文", engine.prompts[0][1])

    def test_bad_reply_and_engines_without_chat_place_nothing(self) -> None:
        self.assertEqual(place_marks(_ChatEngine("not json"), self.ITEMS), {})

        class NoChat:
            engine_name = "x"

        self.assertEqual(place_marks(NoChat(), self.ITEMS), {})


class WriteBackTests(unittest.TestCase):
    SOURCE = "注意：施工前必须完成技术交底。"
    MARKED_SOURCE = "<g1>注意：</g1>施工前必须完成技术交底。"
    MARKED_FR = "<g1>Attention :</g1> le briefing technique est obligatoire avant les travaux."
    PLAIN_FR = "Attention : le briefing technique est obligatoire avant les travaux."

    def _write(self, doc, translations, marked, *, translation_only: bool):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / "src.docx"
        doc.save(source)
        out = write_bilingual_docx(
            source_path=source,
            output_dir=root / "out",
            translations=translations,
            target_lang="fr",
            source_lang="zh",
            output_translation_only=translation_only,
            marked_translations=marked,
        )
        return Document(str(out)), source

    def test_extraction_carries_marked_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "src.docx"
            doc = Document()
            _bold_label_paragraph(doc)
            doc.add_paragraph("普通段落。")
            doc.save(source)
            segments = {s.source: s.marked_source for s in extract_word_segments(source, target_lang="fr")}
        self.assertEqual(segments, {self.SOURCE: self.MARKED_SOURCE, "普通段落。": ""})

    def test_translation_only_keeps_bold_label(self) -> None:
        doc = Document()
        _bold_label_paragraph(doc)
        out, _ = self._write(
            doc, {self.SOURCE: self.PLAIN_FR}, {self.SOURCE: (self.MARKED_SOURCE, self.MARKED_FR)}, translation_only=True
        )
        self.assertEqual(
            _runs(out.paragraphs[0]),
            [("Attention :", True, False, False), (" le briefing technique est obligatoire avant les travaux.", False, False, False)],
        )
        self.assertEqual(out.paragraphs[0].text, self.PLAIN_FR)

    def test_bilingual_keeps_bold_label_in_the_translation_line(self) -> None:
        doc = Document()
        _bold_label_paragraph(doc)
        out, _ = self._write(
            doc, {self.SOURCE: self.PLAIN_FR}, {self.SOURCE: (self.MARKED_SOURCE, self.MARKED_FR)}, translation_only=False
        )
        self.assertEqual(out.paragraphs[0].text, self.SOURCE)
        self.assertEqual([r[:2] for r in _runs(out.paragraphs[1])],
                         [("Attention :", True), (" le briefing technique est obligatoire avant les travaux.", False)])

    def test_hyperlink_survives_and_points_to_the_same_target(self) -> None:
        doc = Document()
        paragraph = doc.add_paragraph()
        paragraph.add_run("详见")
        _add_hyperlink(paragraph, "官网", "https://example.com")
        paragraph.add_run("。")
        source, marked = "详见官网。", "详见<g1>官网</g1>。"
        out, _ = self._write(
            doc, {source: "Voir le site officiel."}, {source: (marked, "Voir le <g1>site officiel</g1>.")},
            translation_only=True,
        )
        self.assertEqual(_runs(out.paragraphs[0])[1], ("site officiel", False, False, True))
        link = out.paragraphs[0]._p.find(qn("w:hyperlink"))
        self.assertEqual(out.part.rels[link.get(qn("r:id"))].target_ref, "https://example.com")

    def test_falls_back_when_final_text_was_changed_later(self) -> None:
        """复查或修数字改过译文：标记版本对不上最终译文，按整段写法写，格式取主体那一截。"""
        doc = Document()
        _bold_label_paragraph(doc)
        changed = "Attention : briefing obligatoire (version corrigée)."
        out, _ = self._write(
            doc, {self.SOURCE: changed}, {self.SOURCE: (self.MARKED_SOURCE, self.MARKED_FR)}, translation_only=True
        )
        self.assertEqual(_runs(out.paragraphs[0]), [(changed, False, False, False)])

    def test_falls_back_when_marked_source_differs(self) -> None:
        doc = Document()
        paragraph = doc.add_paragraph()
        paragraph.add_run("注意：施工前").bold = True  # 同样的文字，加粗范围不同
        paragraph.add_run("必须完成技术交底。")
        out, _ = self._write(
            doc, {self.SOURCE: self.PLAIN_FR}, {self.SOURCE: (self.MARKED_SOURCE, self.MARKED_FR)}, translation_only=True
        )
        self.assertEqual(len(_runs(out.paragraphs[0])), 1)

    def test_table_cell_paragraphs_keep_their_own_marks(self) -> None:
        doc = Document()
        cell = doc.add_table(rows=1, cols=1).cell(0, 0)
        first = cell.paragraphs[0]
        first.add_run("注：").bold = True
        first.add_run("尺寸以毫米计。")
        cell.add_paragraph("标高以米计。")
        source = "注：尺寸以毫米计。\n标高以米计。"
        marked_source = "<g1>注：</g1>尺寸以毫米计。\n标高以米计。"
        marked_fr = "<g1>Note :</g1> dimensions en millimètres.\nCotes de niveau en mètres."
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "c.docx"
            doc.save(path)
            segments = {s.source: s.marked_source for s in extract_word_segments(path, target_lang="fr")}
        self.assertEqual(segments[source], marked_source)
        out, _ = self._write(doc, {source: strip_marks(marked_fr)}, {source: (marked_source, marked_fr)}, translation_only=True)
        out_cell = out.tables[0].cell(0, 0)
        self.assertEqual([r[:2] for r in _runs(out_cell.paragraphs[0])], [("Note :", True), (" dimensions en millimètres.", False)])
        self.assertEqual(out_cell.paragraphs[1].text, "Cotes de niveau en mètres.")


if __name__ == "__main__":
    unittest.main()
