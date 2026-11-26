"""原位替换的删字检查：框线穿过一行字时，框线不算这行字删不掉的残留（2026-11-26）。

设备手册第 8 页屏幕示意框：框的上边正好穿过「Manual        AQ      0%」这行字的范围，
删字后框线还在，被算成「原文删不掉」，这几个按键名没换，同一个框里半译半不译。
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

    return bool(FontSet("fr").pick("Manuel AQ 0%"))


# 译成法文：构建机上没有中文字体，拉丁字体总有
@unittest.skipUnless(_latin_font(), "no Latin font on this machine")
class FrameEdgeTests(unittest.TestCase):
    def _run(self, build) -> dict:
        from core.pdf_vector import translate_page_inplace

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            doc = fitz.open()
            page = doc.new_page(width=595, height=842)
            build(page)
            doc.save(root / "source.pdf")
            translate_page_inplace(root / "source.pdf", 0, root / "work",
                                   translate=lambda texts, prompt, **_: {t: "Manuel AQ 0%" for t in texts},
                                   source_lang="en", target_lang="fr")
            return json.loads((root / "work" / "writeback.json").read_text(encoding="utf-8"))

    def test_a_frame_edge_through_the_label_does_not_block_it(self):
        def build(page):
            page.draw_rect(fitz.Rect(359.6, 212, 457.6, 229.6), color=(0.14, 0.12, 0.13), width=0.99)
            page.insert_text((363.7, 217.5), "Manual        AQ      0%", fontsize=10.07, fontname="helv")

        rep = self._run(build)
        self.assertEqual([s["reason"] for s in rep["skipped"]], [])
        self.assertEqual(len(rep["written"]), 1)

    def test_text_that_is_also_in_a_picture_is_still_not_removable(self):
        def build(page):
            pic = fitz.open()
            pp = pic.new_page(width=200, height=30)
            pp.insert_text((4, 20), "Manual alignment mode", fontsize=14, fontname="helv")
            page.insert_image(fitz.Rect(100, 300, 300, 330), pixmap=pp.get_pixmap(dpi=300))
            page.insert_text((104, 320), "Manual alignment mode", fontsize=14, fontname="helv")

        rep = self._run(build)
        self.assertEqual([s["reason"] for s in rep["skipped"]], ["source_not_removable"])


if __name__ == "__main__":
    unittest.main()
