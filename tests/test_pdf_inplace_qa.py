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
