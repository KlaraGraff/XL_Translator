"""PDF 翻译方式（Beta 分支）：设置、文件分类、每页走哪条路线。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import fitz

from core.pdf_image_translation import is_oversized_for_threshold
from core.pdf_routes import (
    KIND_BLANK,
    KIND_DRAWING,
    KIND_MIXED,
    KIND_SCAN,
    KIND_TEXT,
    classify_document,
    file_uses_word,
    page_route,
)
from core.pdf_word_route import normalize_fonts_xml
from settings import AppSettings, PdfSettings

A4 = (595.276, 841.89)
A1 = (1683.78, 2383.94)
SENTENCE = "The applicant must present a valid passport and proof of travel insurance before departure."


def _route(pdf: PdfSettings, *, a3=False, big=False, kind=KIND_TEXT) -> str:
    return page_route(pdf, oversized_a3=a3, oversized=big, page_kind=kind)


class SettingsTests(unittest.TestCase):
    def test_defaults_keep_the_original_behaviour(self) -> None:
        pdf = PdfSettings()
        self.assertEqual(pdf.translation_route, "image")
        self.assertTrue(pdf.skip_oversized_pages)
        self.assertEqual(_route(pdf, a3=True, big=True, kind=KIND_DRAWING), "skip")
        self.assertEqual(_route(pdf, kind=KIND_TEXT), "image")

    def test_old_settings_file_still_loads(self) -> None:
        # 主线版本存的设置没有这几个字段；分支早期版本存过的旧字段要被忽略
        pdf = PdfSettings.model_validate({"skip_oversized_pages": False, "oversized_page_mode": "inplace"})
        self.assertEqual(pdf.translation_route, "image")
        self.assertFalse(pdf.skip_oversized_pages)

    def test_saved_through_the_settings_api(self) -> None:
        from api.routes.settings import _deep_merge

        base = AppSettings().model_dump(mode="json")
        for route in ("image", "inplace", "word", "auto"):
            merged = _deep_merge(base, {"pdf": {"translation_route": route, "auto_text_route": "inplace"}})
            pdf = AppSettings.model_validate(merged).pdf
            self.assertEqual(pdf.translation_route, route)
            self.assertEqual(pdf.auto_text_route, "inplace")


class PageRouteTests(unittest.TestCase):
    def test_image_route_honours_the_skip_switch(self) -> None:
        pdf = PdfSettings(skip_oversized_pages=False)
        self.assertEqual(_route(pdf, a3=True, big=True, kind=KIND_DRAWING), "image")

    def test_inplace_route(self) -> None:
        pdf = PdfSettings(translation_route="inplace")
        self.assertEqual(_route(pdf, big=True, kind=KIND_DRAWING), "inplace")
        self.assertEqual(_route(pdf, kind=KIND_TEXT), "inplace")
        self.assertEqual(_route(pdf, kind=KIND_DRAWING), "inplace")
        self.assertEqual(_route(pdf, kind=KIND_SCAN), "image")     # nothing to replace

    def test_word_route_pages_fall_back_to_the_original_behaviour(self) -> None:
        pdf = PdfSettings(translation_route="word")
        self.assertEqual(_route(pdf, a3=True, big=True, kind=KIND_DRAWING), "skip")
        self.assertEqual(_route(pdf, kind=KIND_SCAN), "image")

    def test_auto_route(self) -> None:
        pdf = PdfSettings(translation_route="auto")
        self.assertEqual(_route(pdf, big=True, kind=KIND_DRAWING), "inplace")
        self.assertEqual(_route(pdf, kind=KIND_DRAWING), "inplace")  # small drawing page
        self.assertEqual(_route(pdf, kind=KIND_SCAN), "image")
        self.assertEqual(_route(pdf, kind=KIND_TEXT), "image")       # text page of a mixed file
        self.assertEqual(_route(PdfSettings(translation_route="auto", auto_text_route="inplace"), kind=KIND_TEXT),
                         "inplace")
        self.assertEqual(_route(PdfSettings(translation_route="auto", auto_drawing_route="skip"), big=True,
                                kind=KIND_DRAWING), "skip")

    def test_word_is_only_for_whole_text_documents(self) -> None:
        from core.pdf_routes import DocumentKind

        text, mixed = DocumentKind(KIND_TEXT), DocumentKind(KIND_MIXED)
        self.assertTrue(file_uses_word(PdfSettings(translation_route="word"), text))
        self.assertFalse(file_uses_word(PdfSettings(translation_route="word"), mixed))
        self.assertTrue(file_uses_word(PdfSettings(translation_route="auto"), text))
        self.assertFalse(file_uses_word(PdfSettings(translation_route="auto", auto_text_route="inplace"), text))
        self.assertFalse(file_uses_word(PdfSettings(translation_route="image"), text))
        self.assertFalse(file_uses_word(PdfSettings(translation_route="inplace"), text))


def _text_page(doc) -> None:
    page = doc.new_page(width=A4[0], height=A4[1])
    y = 80
    for _ in range(12):
        page.insert_text((50, y), SENTENCE[:90], fontsize=9)
        y += 16


def _drawing_page(doc, size=A4) -> None:
    page = doc.new_page(width=size[0], height=size[1])
    shape = page.new_shape()
    for i in range(700):
        shape.draw_line((20 + i % 50 * 10, 100 + i // 50 * 20), (30 + i % 50 * 10, 110 + i // 50 * 20))
        shape.draw_circle((40 + i % 40 * 12, 500 + i // 40 * 12), 4)
    shape.finish(color=(0, 0, 0), width=0.3)
    shape.commit()
    for i, label in enumerate(("Mur coupe feu", "Plan de toiture", "Détail A")):
        page.insert_text((60, 60 + i * 14), label, fontsize=8)


def _scan_page(doc) -> None:
    page = doc.new_page(width=A4[0], height=A4[1])
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 60, 80), 0)
    pix.clear_with(200)
    page.insert_image(page.rect, pixmap=pix)


class ClassifyTests(unittest.TestCase):
    def _classify(self, builders) -> str:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "doc.pdf"
        doc = fitz.open()
        for build in builders:
            build(doc)
        doc.save(path)
        return classify_document(path, is_oversized=lambda w, h: is_oversized_for_threshold("A3", w, h))

    def test_kinds(self) -> None:
        self.assertEqual(self._classify([_text_page, _text_page]).kind, KIND_TEXT)
        self.assertEqual(self._classify([_drawing_page]).kind, KIND_DRAWING)
        self.assertEqual(self._classify([lambda d: _drawing_page(d, A1)]).kind, KIND_DRAWING)
        self.assertEqual(self._classify([_scan_page]).kind, KIND_SCAN)
        self.assertEqual(self._classify([_text_page, _scan_page]).kind, KIND_MIXED)
        blank = self._classify([_text_page, lambda d: d.new_page()])
        self.assertEqual(blank.kind, KIND_TEXT)
        self.assertEqual(blank.pages[1].kind, KIND_BLANK)


class FontNormalizationTests(unittest.TestCase):
    def test_subset_and_cid_fonts_get_real_names_and_an_east_asian_font(self) -> None:
        xml = ('<w:rPr><w:rFonts w:ascii="ABCDEF+Arial-BoldMT" w:hAnsi="CIDFont+F3" w:eastAsia="Arial"/>'
               '<w:lang w:val="en-US" w:eastAsia="zh-CN"/></w:rPr>')
        out = normalize_fonts_xml(xml)
        self.assertIn('w:ascii="Arial-BoldMT"', out)
        self.assertIn('w:hAnsi="Arial"', out)
        self.assertIn('<w:rFonts w:eastAsia="Microsoft YaHei"', out)
        self.assertIn('<w:lang w:val="en-US" w:eastAsia="zh-CN"/>', out)  # untouched


if __name__ == "__main__":
    unittest.main()


class WritebackGeometryTests(unittest.TestCase):
    def test_white_text_on_a_dark_band_counts_as_ink(self) -> None:
        from PIL import Image, ImageDraw

        from core.pdf_vector.writeback import ink_count, is_light

        im = Image.new("L", (100, 20), 30)            # dark title bar
        ImageDraw.Draw(im).rectangle((10, 5, 40, 14), fill=255)  # white "text"
        self.assertGreater(ink_count(im, (0, 0, 100, 20), light=True)[0], 200)
        self.assertEqual(ink_count(Image.new("L", (100, 20), 30), (0, 0, 100, 20), light=True)[0], 0)
        self.assertTrue(is_light((1.0, 1.0, 1.0)))
        self.assertFalse(is_light((0.0, 0.2, 0.5)))

    def test_content_box_is_the_outermost_ink(self) -> None:
        from PIL import Image, ImageDraw

        from core.pdf_vector.writeback import Layout, content_box

        im = Image.new("L", (600, 800), 255)
        ImageDraw.Draw(im).rectangle((80, 100, 520, 700), outline=0)
        box = content_box(im)
        self.assertEqual(box, (80, 100, 521, 701))
        layout = Layout(Image.new("L", (600, 800), 255), 72, bounds=box)
        self.assertTrue(layout.blank((100, 200, 300, 220)))
        self.assertFalse(layout.blank((500, 200, 560, 220)))  # would run into the margin
