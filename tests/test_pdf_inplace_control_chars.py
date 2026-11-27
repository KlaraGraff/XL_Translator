"""原位替换：原文里的控制字符不算字（2026-11-27，说明书第 7 页漏翻约 19 行）。

有的 PDF 把编号列表「1.」后面的制表位写成 \\x07。原来它被当成字留在原文里，模型照抄进译文，
写回时字体里找不到这个字，8 段编号列表全记成 font_missing_glyph、留着原文。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pytest

fitz = pytest.importorskip("fitz")


def _latin_font() -> bool:
    from core.pdf_vector.fonts import FontSet

    return bool(FontSet("fr").pick("Important : suivez les étapes."))


class ControlCharacterTests(unittest.TestCase):
    def test_norm_text_turns_control_characters_into_spaces(self):
        from core.pdf_vector.common import norm_text

        self.assertEqual(norm_text("1. \x07Important.\x00 Done\x7f"), "1. Important. Done")

    def test_a_line_with_only_a_control_character_is_not_a_row(self):
        from core.pdf_vector.extract import native_rows

        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((100, 300), "1. \x07Important. Do not put the reflector up.", fontsize=12, fontname="helv")
        page.insert_text((100, 340), " \x07", fontsize=12, fontname="helv")
        self.assertEqual([r["source_text"] for r in native_rows(page, 72)],
                         ["1. Important. Do not put the reflector up."])

    # 译成法文：构建机上没有中文字体，拉丁字体总有
    @unittest.skipUnless(_latin_font(), "no Latin font on this machine")
    def test_a_numbered_item_with_a_control_character_is_written(self):
        from core.pdf_vector import translate_page_inplace

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            doc = fitz.open()
            page = doc.new_page(width=595, height=842)
            page.insert_text((86, 200), "1. \x07Important. Do not put the reflector up. However, keep",
                             fontsize=12, fontname="helv")
            page.insert_text((86, 216), "it ready to install.", fontsize=12, fontname="helv")
            page.insert_text((86, 260), "2. \x07Power up the unit and you will see the first screen.",
                             fontsize=12, fontname="helv")
            doc.save(root / "source.pdf")
            seen: list[str] = []

            def translate(texts, prompt, **_extra):
                seen.extend(texts)
                # 模型照抄了控制字符：写回前也要把它当空白。
                return {t: t[:3] + "\x07Important : suivez les étapes." for t in texts}

            translate_page_inplace(root / "source.pdf", 0, root / "work", translate=translate,
                                   source_lang="en", target_lang="fr")
            report = json.loads((root / "work" / "writeback.json").read_text(encoding="utf-8"))

        self.assertTrue(seen)
        self.assertFalse(any("\x07" in t for t in seen), seen)
        self.assertEqual([s for s in report["skipped"] if s["reason"] == "font_missing_glyph"], [])
        self.assertGreaterEqual(len(report["written"]), 2, report)
        # 有的字体（云端测试用的）给 \x07 也配了字形，不会被跳过，但同样不该把它写进页面。
        self.assertFalse(any("\x07" in w["translation"] for w in report["written"]), report["written"])


if __name__ == "__main__":
    unittest.main()
