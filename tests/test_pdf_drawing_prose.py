"""正文页配一张矢量插图不判成图纸；按设置原样保留的页写真实原因，不说成大幅面（反馈 #34）。

样例都是自造的：用户文件不进仓库。反馈里的页：165×242 mm，约 300 词正文配一张 1000 来段
曲线的流程图，「图纸和大图」设为原样保留，整页正文没翻，日志还写成「为大幅面」。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

from core.pdf_routes import (
    KIND_DRAWING,
    KIND_TEXT,
    PageKind,
    classify_document,
    exceeds_word_page,
    kept_by_setting_reason,
)
from settings import PdfSettings

fitz = pytest.importorskip("fitz")

MM = 72 / 25.4
B5ISH = (165 * MM, 242 * MM)
A4_LANDSCAPE = (841.89, 595.276)
A3_LANDSCAPE = (1190.551, 841.89)
_WORDS = ("the method of proof proceeds by induction on the length of the derivation and each "
          "step preserves the invariant stated above so that the conclusion follows directly from").split()


def _prose(page, x0: float, y0: float, width: float, count: int, size: float = 10) -> float:
    words, line, y = (_WORDS * 100)[:count], [], y0
    for word in words:
        if fitz.get_text_length(" ".join(line + [word]), fontsize=size) > width:
            page.insert_text((x0, y), " ".join(line), fontsize=size)
            y, line = y + size * 1.3, [word]
        else:
            line.append(word)
    if line:
        page.insert_text((x0, y), " ".join(line), fontsize=size)
        y += size * 1.3
    return y


def _flowchart(page, rect, curves: int = 1010) -> None:
    """圆角框加弯箭头：每个框 4 段曲线，每支箭头 1 段，凑够 curves 段。"""
    shape = page.new_shape()
    cols, rows, drawn = 4, 6, 0
    w, h = rect.width / cols, rect.height / rows
    while drawn < curves:
        for r in range(rows):
            for c in range(cols):
                if drawn >= curves:
                    break
                box = fitz.Rect(rect.x0 + c * w + 4, rect.y0 + r * h + 4,
                                rect.x0 + (c + 1) * w - 4, rect.y0 + (r + 1) * h - 4)
                shape.draw_rect(box, radius=0.2)
                shape.draw_bezier(box.bl, box.bl + (5, 8), box.br + (-5, 8), box.br)
                drawn += 5
    shape.finish(color=(0, 0, 0), width=0.3)
    shape.commit()
    for r in range(rows):
        for c in range(cols):
            page.insert_text((rect.x0 + c * w + 8, rect.y0 + r * h + h / 2), f"Step {r * cols + c}", fontsize=7)


def _paper_page(doc, words: int = 300) -> None:
    """正文分在流程图上下两段。"""
    page = doc.new_page(width=B5ISH[0], height=B5ISH[1])
    y = _prose(page, 20 * MM, 25 * MM, 125 * MM, words // 2)
    _flowchart(page, fitz.Rect(30 * MM, y + 5 * MM, 135 * MM, y + 95 * MM))
    _prose(page, 20 * MM, y + 105 * MM, 125 * MM, words - words // 2)


def _figure_page(doc) -> None:
    """一整页流程图，下面三行图注：还是图纸。"""
    page = doc.new_page()
    _flowchart(page, fitz.Rect(50, 60, page.rect.width - 50, page.rect.height - 140), 1200)
    _prose(page, 50, page.rect.height - 110, page.rect.width - 100, 40, size=9)


def _cad_page(doc, size=A4_LANDSCAPE, notes: int = 15) -> None:
    """CAD 图：满页斜线、几百个短标注，右栏一列总说明。"""
    page = doc.new_page(width=size[0], height=size[1])
    shape = page.new_shape()
    for i in range(1300):
        x, y = 30 + (i * 37) % (size[0] * 0.7), 40 + (i * 53) % (size[1] - 120)
        shape.draw_line((x, y), (x + 12, y + 7))
    shape.finish(color=(0, 0, 0), width=0.2)
    shape.commit()
    labels = ("FIRE DOOR FD-1", "WALL W2", "1200", "LEVEL +3.500", "DETAIL A", "SECTION B-B", "PUMP P-101")
    for i in range(300):
        page.insert_text((30 + (i * 71) % (size[0] * 0.7), 50 + (i * 29) % (size[1] - 140)),
                         labels[i % len(labels)], fontsize=6)
    note = "ALL DIMENSIONS ARE IN MILLIMETRES UNLESS OTHERWISE NOTED ON THE SHEET"
    x0, y = size[0] * 0.75, 60
    page.insert_text((x0, y), "GENERAL NOTES", fontsize=8)
    for k in range(notes):
        y += 9
        page.insert_text((x0, y), f"{k + 1}. {note}", fontsize=6)


def _save(builders) -> Path:
    tmp = tempfile.mkdtemp()
    path = Path(tmp) / "doc.pdf"
    doc = fitz.open()
    for build in builders:
        build(doc)
    doc.save(path)
    return path


class ProsePageWithFigureTests(unittest.TestCase):
    def _kinds(self, builders, *, component=True):
        path = _save(builders)
        with patch("core.pdf_component.is_available", return_value=component):
            return classify_document(path, is_oversized=exceeds_word_page).pages

    def test_text_page_with_a_dense_flowchart_stays_a_text_page(self):
        for component in (True, False):   # PyMuPDF and pypdfium2 measure the same way
            with self.subTest(component=component):
                page = self._kinds([_paper_page], component=component)[0]
                self.assertGreaterEqual(page.slanted_items, 1000)   # dense enough to have counted as a drawing
                self.assertEqual(page.kind, KIND_TEXT)
                self.assertGreater(page.prose_share, 0.2)
                fewer = self._kinds([lambda d: _paper_page(d, words=200)], component=component)[0]
                self.assertEqual(fewer.kind, KIND_TEXT)   # about half the page is prose

    def test_drawings_stay_drawings(self):
        for component in (True, False):
            with self.subTest(component=component):
                pages = self._kinds([_figure_page, _cad_page, lambda d: _cad_page(d, A3_LANDSCAPE, 30),
                                     lambda d: _cad_page(d, A4_LANDSCAPE, 40)], component=component)
                self.assertEqual([page.kind for page in pages], [KIND_DRAWING] * 4)
                self.assertTrue(all(page.prose_share < 0.15 for page in pages))


class KeptBySettingReasonTests(unittest.TestCase):
    def test_reason_names_the_real_cause(self):
        auto = PdfSettings.model_validate({"translation_route": "auto", "auto_drawing_route": "skip",
                                           "auto_scan_route": "skip"})
        drawing = PageKind(9, KIND_DRAWING, False, words=40, vector_items=1300, slanted_items=1300)
        self.assertEqual(kept_by_setting_reason(auto, drawing, page_kind=KIND_DRAWING, short_mm=165, long_mm=242),
                         "按「图纸和大图」处理（线条密集、字都是零散的标注），这类页设为原样保留")
        big_text = PageKind(2, KIND_DRAWING, True, words=400, vector_items=10, slanted_items=0)
        self.assertIn("页面大到放不进 Word",
                      kept_by_setting_reason(auto, big_text, page_kind=KIND_DRAWING, short_mm=594, long_mm=841))
        self.assertEqual(kept_by_setting_reason(auto, None, page_kind="scan", short_mm=210, long_mm=297),
                         "提取不到文字，按「扫描页」处理，这类页设为原样保留")
        image = PdfSettings.model_validate({"translation_route": "image"})
        self.assertEqual(kept_by_setting_reason(image, None, page_kind="", short_mm=297, long_mm=420),
                         "为大幅面（297×420 mm），已开启「跳过 A3 及更大的页面」")
        for reason in (kept_by_setting_reason(auto, drawing, page_kind=KIND_DRAWING, short_mm=165, long_mm=242),
                       kept_by_setting_reason(auto, None, page_kind="scan", short_mm=165, long_mm=242)):
            self.assertNotIn("大幅面", reason)


class RunnerLogsTheRealReasonTests(unittest.TestCase):
    """整个 PDF 任务：正文配流程图的页照常翻译；图纸页按设置保留，日志和报告写真实原因。"""

    def test_auto_route_with_drawings_kept(self):
        from core.api_scheduler import WeightedApiScheduler
        from core.model_roles import ROLE_IMAGE, SOURCE_INDEPENDENT, resolve_effective_model_config
        from core.pdf_image_translation import PdfFileItem, PdfImageTranslationRunner, max_page_generation_attempts
        from core.pdf_task_report import _kept_by_setting_lines, _page_status_text, fully_skipped_oversize_message
        from settings import AppSettings
        from tests.test_pdf_image_translation import _png_bytes, _RecordingImageClient

        source_pdf = _save([_paper_page, _cad_page])
        root = source_pdf.parent
        settings = AppSettings(target_lang="zh")
        # 正文页走图像翻译（测试里用假图像模型），图纸页原样保留：用户的设置。
        settings.pdf = PdfSettings.model_validate({"target_lang": "zh", "translation_route": "auto",
                                                   "auto_text_route": "image", "auto_drawing_route": "skip"})
        settings.image_model_role.source_role = SOURCE_INDEPENDENT
        settings.image_model_role.cloud_provider = "custom_openai"
        settings.image_model_role.cloud_model = "image-model"
        settings.image_model_role.cloud_base_url = "https://images.example/v1"
        client = _RecordingImageClient(_png_bytes(1949, 2858))
        runner = PdfImageTranslationRunner(
            [PdfFileItem(path=source_pdf, name="paper", size_kb=1.0, page_count=2)],
            settings, source_root=root, image_client=client, task_logger_enabled=False,
        )
        with patch("core.model_roles.get_key", return_value="secret"):
            prepared = runner._prepare_pdf_files(output_dir=root / "out", app_managed=True)[0]
            runner._total_page_count = 2
            runner._process_prepared_pages(
                [prepared], max_attempts=max_page_generation_attempts(0), scheduler=WeightedApiScheduler(1),
                review_scheduler=WeightedApiScheduler(1),
                model_config=resolve_effective_model_config(settings, ROLE_IMAGE),
                review_model_config=None, concurrency=1, total_pages=2,
            )
            runner._finalize_file_record(prepared, should_assemble=True)
        record = prepared.record
        pages = sorted(record.pages, key=lambda p: p.page_number)
        self.assertEqual(len(client.calls), 1)                 # the paper page was translated
        self.assertFalse(pages[0].skipped_oversize)
        self.assertEqual(pages[1].kept_by_setting,
                         "按「图纸和大图」处理（线条密集、字都是零散的标注），这类页设为原样保留")
        self.assertEqual(pages[1].kept_reason, "")             # by setting: not a problem to review
        self.assertEqual(record.skipped_oversize_page_count, 1)
        logs = "\n".join(str(getattr(msg, "message", "")) for msg in list(runner._queue.queue))
        self.assertIn("第 2 页按「图纸和大图」处理（线条密集、字都是零散的标注），这类页设为原样保留，"
                      "原页将直接放入输出 PDF。", logs)
        self.assertNotIn("大幅面", logs)
        self.assertEqual(_page_status_text(pages[1]), "按设置原样保留")
        self.assertEqual(_kept_by_setting_lines(record),
                         ["  - 第 2 页：按「图纸和大图」处理（线条密集、字都是零散的标注），这类页设为原样保留"])
        self.assertIn("都按设置原样保留", fully_skipped_oversize_message(record))
        self.assertNotIn("幅面", fully_skipped_oversize_message(record))


if __name__ == "__main__":
    unittest.main()
