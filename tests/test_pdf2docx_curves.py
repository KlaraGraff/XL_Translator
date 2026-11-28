"""PDF 转 Word：圆、椭圆按图片保留，不被 pdf2docx 当成长方形丢掉。

讲义第 48 页图 13（维恩图）：灰底方框里一个白色实心圆、一个空心圆，转出来只剩灰底和 A、B
两个字母；第 57 页箭头图里单独的点也丢了。pdf2docx 用贝塞尔曲线的控制点量面积，圆被算成
外接正方形的九成，当成了长方形。这里用同样画法的小样例，走一键安装组件的转换入口
（core.pdf_component._convert_in_process）真转一次 Word。
"""
from __future__ import annotations

import math
import tempfile
import unittest
import zipfile
from pathlib import Path

try:
    import pymupdf
except ImportError:  # pragma: no cover - PyMuPDF is in requirements-test.txt
    pymupdf = None

try:
    import pdf2docx
except ImportError:  # pragma: no cover - pdf2docx is in requirements-test.txt
    pdf2docx = None

from core.pdf2docx_curves import fills_its_box, outline

_PIC = "{http://schemas.openxmlformats.org/drawingml/2006/picture}pic"
_ANCHOR = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}anchor"
_WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_EMU = 12700

GRAY = (0.663, 0.663, 0.663)
VENN = (255.6, 372.6, 339.6, 420.6)  # 第 48 页图 13 的灰底方框
DOT = (231.838, 213.875, 235.438, 217.475)  # 第 57 页箭头图里单独的小黑点（原位置）
#: 讲义（TikZ）画圆用的控制点：半径 16.492 磅的圆，控制点离端点 9.109 磅。
#: PyMuPDF 自己的 draw_circle 控制点稍远一点，控制点围出来的面积不到九成，复现不了。
KAPPA = 9.109 / 16.492


def _circle(shape, cx: float, cy: float, r: float) -> None:
    k = KAPPA * r
    for curve in (((cx + r, cy), (cx + r, cy - k), (cx + k, cy - r), (cx, cy - r)),
                  ((cx, cy - r), (cx - k, cy - r), (cx - r, cy - k), (cx - r, cy)),
                  ((cx - r, cy), (cx - r, cy + k), (cx - k, cy + r), (cx, cy + r)),
                  ((cx, cy + r), (cx + k, cy + r), (cx + r, cy + k), (cx + r, cy))):
        shape.draw_bezier(*curve)


def _make_pdf(path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=595.276, height=841.89)
    page.insert_text((57, 80), "In Figure 13, the shaded area represents the complement of A.",
                     fontname="helv", fontsize=10)
    # 图 13：灰底方框（带边线），一个白色实心圆（A）压在上面，一个空心圆（B）
    shape = page.new_shape()
    shape.draw_rect(pymupdf.Rect(*VENN))
    shape.finish(color=(0, 0, 0), fill=GRAY, width=0.8)
    _circle(shape, 288.638, 396.602, 16.492)
    shape.finish(color=(0, 0, 0), fill=(1, 1, 1), width=0.8, closePath=True)
    _circle(shape, 307.638, 396.602, 16.492)
    shape.finish(color=(0, 0, 0), width=0.8, closePath=True)
    shape.commit()
    page.insert_text((275, 400), "A", fontname="helv", fontsize=8)
    page.insert_text((315, 400), "B", fontname="helv", fontsize=8)
    page.insert_text((215, 445), "Figure 13: The complement of A.", fontname="helv", fontsize=10)
    # 圆角的「Note」框：底色、四个角是曲线，但几乎就是长方形，应当照旧转成带底色的表格
    shape = page.new_shape()
    shape.draw_rect(pymupdf.Rect(56.7, 480, 538.6, 540), radius=0.05)
    shape.finish(fill=(0.95, 0.95, 1.0), width=0)
    shape.commit()
    page.insert_text((72, 515), "Note: a rounded box keeps its text and its colour.", fontname="helv", fontsize=10)
    # 单独一个小黑点，旁边一个标签
    shape = page.new_shape()
    _circle(shape, (DOT[0] + DOT[2]) / 2, (DOT[1] + DOT[3]) / 2, (DOT[2] - DOT[0]) / 2)
    shape.finish(fill=(0, 0, 0), width=0, closePath=True)
    shape.commit()
    page.insert_text((243, 219), "4", fontname="helv", fontsize=10)
    page.insert_text((57, 700), "End of page.", fontname="helv", fontsize=10)
    doc.save(path)


def _document(docx: Path):
    from lxml import etree

    with zipfile.ZipFile(docx) as archive:
        return etree.fromstring(archive.read("word/document.xml"))


def _picture_boxes(root) -> list[tuple[float, float, float, float]]:
    """转出来的 Word 里每张浮动图片在页面上的位置（磅）。"""
    boxes = []
    for anchor in root.iter(_ANCHOR):
        x = int(anchor.find(f"{_WP}positionH/{_WP}posOffset").text) / _EMU
        y = int(anchor.find(f"{_WP}positionV/{_WP}posOffset").text) / _EMU
        extent = anchor.find(f"{_WP}extent")
        boxes.append((x, y, x + int(extent.get("cx")) / _EMU, y + int(extent.get("cy")) / _EMU))
    return boxes


def _covers(box, target, slack: float = 2.0) -> bool:
    return (box[0] - slack <= target[0] and box[1] - slack <= target[1]
            and box[2] + slack >= target[2] and box[3] + slack >= target[3])


def _text(root) -> str:
    return "".join(node.text or "" for node in root.iter(f"{_W}t"))


class OutlineTests(unittest.TestCase):
    """量面积用曲线本身：圆不到九成，圆角框还在九成以上。"""

    class _Line:
        def __init__(self, *points):
            self.points = points

    class _Curve(_Line):
        pass

    def _circle(self, r: float = 10.0) -> list:
        k = KAPPA * r
        return [self._Curve((r, 0), (r, k), (k, r), (0, r)), self._Curve((0, r), (-k, r), (-r, k), (-r, 0)),
                self._Curve((-r, 0), (-r, -k), (-k, -r), (0, -r)), self._Curve((0, -r), (k, -r), (r, -k), (r, 0))]

    def test_circle_is_not_a_rectangle(self) -> None:
        points = outline(self._circle(), self._Curve)
        self.assertFalse(fills_its_box(points, 0.9))
        # 控制点围出来的面积和外接正方形差不到万分之三：九成上下，pdf2docx 原来的量法看舍入落在哪边
        corners = [point for segment in self._circle() for point in segment.points]
        xs, ys = [x for x, _ in corners], [y for _, y in corners]
        area = abs(sum(xa * yb - xb * ya for (xa, ya), (xb, yb) in zip(corners, corners[1:]))) / 2
        self.assertAlmostEqual(area / ((max(xs) - min(xs)) * (max(ys) - min(ys))), 0.9, delta=3e-4)

    def test_rounded_box_and_lines_stay_rectangles(self) -> None:
        r, k, w, h = 3.0, 0.5523 * 3.0, 200.0, 40.0
        rounded = [self._Line((r, 0), (w - r, 0)), self._Curve((w - r, 0), (w - r + k, 0), (w, r - k), (w, r)),
                   self._Line((w, r), (w, h - r)), self._Curve((w, h - r), (w, h - r + k), (w - r + k, h), (w - r, h)),
                   self._Line((w - r, h), (r, h)), self._Curve((r, h), (r - k, h), (0, h - r + k), (0, h - r)),
                   self._Line((0, h - r), (0, r)), self._Curve((0, r), (0, r - k), (r - k, 0), (r, 0))]
        self.assertTrue(fills_its_box(outline(rounded, self._Curve), 0.9))
        self.assertTrue(fills_its_box(outline([self._Line((0, 5), (90, 5))], self._Curve), 0.9))
        self.assertFalse(fills_its_box(outline([self._Line((0, 0), (90, 40))], self._Curve), 0.9))

    def test_quarter_circle_area(self) -> None:
        points = outline(self._circle(1.0), self._Curve)
        area = abs(sum(xa * yb - xb * ya for (xa, ya), (xb, yb) in zip(points, points[1:]))) / 2
        self.assertAlmostEqual(area, math.pi, delta=0.05)


@unittest.skipIf(pymupdf is None or pdf2docx is None, "PyMuPDF / pdf2docx not installed")
class ConvertKeepsCurvesTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        self.pdf = self.folder / "venn.pdf"
        _make_pdf(self.pdf)

    def test_stock_pdf2docx_drops_the_venn_circles(self) -> None:
        """问题本身：pdf2docx 原样转，图 13 的圆和小黑点都没有了。"""
        from pdf2docx import Converter

        docx = self.folder / "stock.docx"
        converter = Converter(str(self.pdf))
        try:
            converter.convert(str(docx))
        finally:
            converter.close()
        # 一张图片都没有：圆当成长方形，小黑点也当成长方形
        self.assertEqual(list(_document(docx).iter(_PIC)), [])

    def test_component_conversion_keeps_circles_as_pictures_and_labels_as_text(self) -> None:
        from core.pdf_component import _convert_in_process

        docx = self.folder / "fixed.docx"
        _convert_in_process(Path(pdf2docx.__file__).resolve().parents[1], self.pdf, docx)
        root = _document(docx)
        # 两张图片：维恩图（浮在原位置，盖住整个灰底方框）和小黑点（跟着它那一行）。
        # 圆角框没有截成图片，照旧是带底色的表格，字在里面。
        self.assertEqual(len(list(root.iter(_PIC))), 2)
        boxes = _picture_boxes(root)
        self.assertEqual(len(boxes), 1)
        self.assertTrue(_covers(boxes[0], VENN), boxes)
        text = _text(root)
        for label in ("A", "B", "Figure 13", "rounded box keeps its text", "4"):
            self.assertIn(label, text)

    def test_pdf2docx_is_left_as_it_was_after_converting(self) -> None:
        from pdf2docx.shape import Path as path_module

        from core.pdf2docx_curves import curves_measured_on_the_curve

        before = path_module.Segments.__dict__["is_iso_oriented"]
        with curves_measured_on_the_curve():
            self.assertIsNot(path_module.Segments.__dict__["is_iso_oriented"], before)
        self.assertIs(path_module.Segments.__dict__["is_iso_oriented"], before)


if __name__ == "__main__":
    unittest.main()
