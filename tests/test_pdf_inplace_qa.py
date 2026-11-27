"""原位替换的自检：写译文的地方以外，只要页面看起来没变就不算删坏了（2026-11-26）。

设备手册的接线图页：几十个画成线条的端子字母块压在接线图图片下面，写译文后从页面
数据里没了，但页面一个点都没变。原来按「路径少了」整页退回英文。
"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

import pytest

fitz = pytest.importorskip("fitz")


def _png() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (40, 120, 200)).save(buf, "PNG")
    return buf.getvalue()


class RemovedPathTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def _page(self, name: str, *, hidden: bool, visible: bool) -> Path:
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.draw_line((40, 60), (500, 60), color=(0, 0, 0), width=1)
        if hidden:   # 端子字母块，画在接线图图片下面
            page.draw_rect(fitz.Rect(220, 420, 226, 430), color=None, fill=(0, 0, 0))
        if visible:  # 图片外面一条看得见的线
            page.draw_line((40, 700), (300, 700), color=(0.6, 0.6, 0.6), width=0.5)
        page.insert_image(fitz.Rect(100, 350, 500, 600), stream=_png())
        path = self.root / f"{name}.pdf"
        doc.save(path)
        return path

    def _check(self, output: Path) -> dict:
        from core.pdf_vector import qa

        source = self._page("source", hidden=True, visible=True)
        manifest, report = self.root / "manifest.json", self.root / "writeback.json"
        manifest.write_text(json.dumps({"rows": []}), encoding="utf-8")
        report.write_text(json.dumps({"written": [], "skipped": [], "metrics": {"candidates": 0, "fit_modes": {}}}), encoding="utf-8")
        return qa.run(source, output, manifest, report, self.root / "qa.json", self.root / "ledger.csv")

    def test_a_path_hidden_under_an_image_does_not_fail_the_page(self):
        res = self._check(self._page("out", hidden=False, visible=True))
        self.assertEqual(res["vector_paths"]["removed"], 1)
        self.assertEqual(res["vector_paths"]["removed_outside_written_rects"], 0)
        self.assertEqual(res["vector_paths"]["removed_without_visible_change"], 1)
        self.assertEqual(res["status"], "pass")

    def test_a_visible_line_that_disappears_still_fails_the_page(self):
        res = self._check(self._page("out", hidden=True, visible=False))
        self.assertEqual(res["vector_paths"]["removed_outside_written_rects"], 1)
        self.assertEqual(res["status"], "review")


if __name__ == "__main__":
    unittest.main()


class ImageListTests(unittest.TestCase):
    """手册封底：软蒙版里画的投影图在 MuPDF 整理内容时被合并，图片清单 16 条变 10 条，
    页面一点没变，原来报「图片有变化」整页退回英文。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def _page(self, name: str, *, hidden: bool, visible: bool) -> Path:
        from PIL import Image

        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        if hidden:  # 被上面那张不透明的图整个盖住
            page.insert_image(fitz.Rect(300, 300, 360, 330), stream=_png())
        buf = io.BytesIO()
        Image.new("RGB", (40, 30), (200, 60, 40)).save(buf, "PNG")
        page.insert_image(fitz.Rect(290, 290, 370, 340), stream=buf.getvalue())
        if visible:
            page.insert_image(fitz.Rect(100, 600, 200, 680), stream=_png())
        path = self.root / f"{name}.pdf"
        doc.save(path)
        return path

    def _check(self, output: Path) -> dict:
        from core.pdf_vector import qa

        source = self._page("source", hidden=True, visible=True)
        manifest, report = self.root / "manifest.json", self.root / "writeback.json"
        manifest.write_text(json.dumps({"rows": []}), encoding="utf-8")
        report.write_text(json.dumps({"written": [], "skipped": [], "metrics": {"candidates": 0, "fit_modes": {}}}),
                          encoding="utf-8")
        return qa.run(source, output, manifest, report, self.root / "qa.json", self.root / "ledger.csv")

    def test_an_image_draw_that_never_showed_does_not_fail_the_page(self):
        res = self._check(self._page("out", hidden=False, visible=True))
        self.assertEqual((res["images"]["source"], res["images"]["output"]), (3, 2))
        self.assertTrue(res["images"]["identical"])
        self.assertEqual(res["status"], "pass")

    def test_a_visible_image_that_disappears_still_fails_the_page(self):
        res = self._check(self._page("out", hidden=True, visible=False))
        self.assertFalse(res["images"]["identical"])
        self.assertEqual(res["status"], "review")


class ReviewedNumberTests(unittest.TestCase):
    """说明书封面（2026-11-27 Mac 复查）：「01.01.23 Rev. 11」译成「2023年1月1日 修订版次 11」，
    数字规则没过、模型复查判意思一致后写进页面，自检又按同一条规则整页退回英文。复查的结论
    算数：自检不再因为这一行整页退回。"""

    def test_a_number_change_the_review_accepted_does_not_fail_the_page(self):
        from core.pdf_vector import translate_page_inplace
        from core.pdf_vector.fonts import find_cjk_font

        if not find_cjk_font():
            self.skipTest("no CJK font on this machine: in place cannot write Chinese")

        class Results(dict):
            review_accepted: dict = {}

        def translate(texts, prompt, extra_check=None, **_extra):
            out = Results({t: "用户手册 2023年1月1日 修订版次 11" if "Rev" in t else "说明" for t in texts})
            out.review_accepted = {t: "日期写法不同，意思一致" for t in texts
                                   if extra_check and extra_check(t, out[t])}
            return out

        translate.takes_extra_check = True
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            doc = fitz.open()
            page = doc.new_page(width=595, height=842)
            page.insert_text((60, 200), "User manual 01.01.23 Rev. 11", fontsize=14, fontname="helv")
            page.insert_text((60, 240), "Installation and commissioning guide", fontsize=12, fontname="helv")
            doc.save(root / "source.pdf")
            res = translate_page_inplace(root / "source.pdf", 0, root / "work", translate=translate,
                                         source_lang="en", target_lang="zh")
            self.assertTrue(res.ok, res.error)
            self.assertIn("2023年1月1日", fitz.open(res.output_pdf)[0].get_text())
