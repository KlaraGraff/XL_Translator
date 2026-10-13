"""「原位替换文字（保留矢量，Beta）」接入 PDF 任务的端到端测试。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fitz

from core.api_scheduler import WeightedApiScheduler
from core.model_roles import ROLE_IMAGE, SOURCE_INDEPENDENT, resolve_effective_model_config
from core.pdf_image_translation import (
    PDF_OUTPUT_STATE_NEEDS_REVIEW,
    PdfFileItem,
    _page_has_text_layer,
    PdfImageTranslationRunner,
    is_oversized_for_threshold,
    max_page_generation_attempts,
)
from core.pdf_vector import InplaceHooks, find_cjk_font
from settings import AppSettings, PdfSettings
from tests.test_pdf_image_translation import _png_bytes, _RecordingImageClient

A4 = (595.276, 841.89)
A3 = (841.89, 1190.551)
A1 = (1683.78, 2383.94)

LABELS = {
    "Plan de toiture": "屋面平面图",
    "Mur coupe feu 1 heure": "1小时防火墙",
    "Détection automatique d'incendie": "自动火灾探测",
}


def _write_pdf(path: Path, sizes, *, with_text: list[bool]) -> None:
    doc = fitz.open()
    for (w, h), text in zip(sizes, with_text):
        page = doc.new_page(width=w, height=h)
        page.draw_line((40, 200), (w - 40, 200), color=(1, 0, 0), width=1)  # a drawing line that must survive
        if text:
            y = 120
            for label in LABELS:
                page.insert_text((60, y), label, fontsize=14)
                y += 30
    doc.save(path)


def _hooks(translate=None) -> InplaceHooks:
    def fake_translate(texts, _prompt):
        return {t: LABELS.get(t, t) for t in texts}

    return InplaceHooks(translate=translate or fake_translate, profile=None, tesseract=None,
                        tesseract_languages=None, source_lang="fr", ocr_wanted=False)


@unittest.skipUnless(find_cjk_font(), "no CJK font on this machine")
class InplaceTaskTests(unittest.TestCase):
    def _run(self, *, route="auto", target="zh", hooks=None, sizes=(A4, A3), text=(True, True),
             image_model_missing="", extra=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        source_pdf = root / "source.pdf"
        _write_pdf(source_pdf, list(sizes), with_text=list(text))
        settings = AppSettings(target_lang=target)
        settings.pdf = PdfSettings.model_validate({"target_lang": target, "translation_route": route,
                                                   **(extra or {})})
        settings.image_model_role.source_role = SOURCE_INDEPENDENT
        settings.image_model_role.cloud_provider = "custom_openai"
        settings.image_model_role.cloud_model = "image-model"
        settings.image_model_role.cloud_base_url = "https://images.example/v1"
        client = _RecordingImageClient(_png_bytes(2481, 3508))
        runner = PdfImageTranslationRunner(
            [PdfFileItem(path=source_pdf, name="source", size_kb=1.0, page_count=len(sizes))],
            settings, source_root=root, image_client=client, task_logger_enabled=False,
            inplace_hooks=hooks or _hooks(),
        )
        runner._image_model_missing = image_model_missing
        with patch("core.model_roles.get_key", return_value="secret"):
            prepared = runner._prepare_pdf_files(output_dir=root / "out", app_managed=True)[0]
            runner._total_page_count = len(sizes)
            runner._process_prepared_pages(
                [prepared], max_attempts=max_page_generation_attempts(0), scheduler=WeightedApiScheduler(1),
                review_scheduler=WeightedApiScheduler(1), model_config=resolve_effective_model_config(settings, ROLE_IMAGE),
                review_model_config=None, concurrency=1, total_pages=len(sizes),
            )
            runner._finalize_file_record(prepared, should_assemble=True)
        runner._prepared_files = [prepared]
        return runner, prepared.record, client

    def test_oversized_page_is_translated_in_place_and_keeps_vectors(self):
        runner, record, client = self._run()
        self.assertEqual(len(client.calls), 1)  # only the A4 page went to the image model
        self.assertTrue(record.translated_pdf_path)  # page 1 is a blank fake image, so the file may be flagged
        page = sorted(record.pages, key=lambda p: p.page_number)[1]
        self.assertEqual(page.inplace_status, "done")
        self.assertEqual(page.inplace_written, 3)
        self.assertEqual(record.skipped_oversize_page_count, 0)
        with fitz.open(record.translated_pdf_path) as out:
            text = out[1].get_text()
            self.assertIn("屋面平面图", text)
            self.assertNotIn("Plan de toiture", text)
            self.assertEqual(len(out[1].get_drawings()), 1)  # the red line is untouched
            self.assertAlmostEqual(out[1].rect.width, A3[0], places=1)
        snap = runner.pdf_page_snapshot()["files"][0]["pages"][1]
        self.assertEqual(snap["inplace_status"], "done")
        self.assertEqual(snap["inplace_written"], 3)

    def test_failed_translation_keeps_the_original_page(self):
        def broken(_texts, _prompt):
            raise RuntimeError("service down")

        _runner, record, _client = self._run(hooks=_hooks(broken))
        page = sorted(record.pages, key=lambda p: p.page_number)[1]
        self.assertEqual(page.inplace_status, "kept_original")
        self.assertEqual(record.skipped_oversize_page_count, 1)
        with fitz.open(record.translated_pdf_path) as out:
            self.assertIn("Plan de toiture", out[1].get_text())

    def test_failed_page_flags_the_file_for_review_and_is_not_retried_on_rerun(self):
        calls = []

        def broken(texts, _prompt):
            calls.append(len(texts))
            raise RuntimeError("service down")

        runner, record, _client = self._run(hooks=_hooks(broken))
        self.assertEqual(record.status, PDF_OUTPUT_STATE_NEEDS_REVIEW)
        before = len(calls)
        # a later single-page rerun finalizes the file again: the failed in-place
        # page must not be re-OCRed and re-translated as a side effect
        runner._run_inplace_pages(record, runner._prepared_files[0])
        self.assertEqual(len(calls), before)

    def test_non_chinese_target_keeps_original(self):
        _runner, record, _client = self._run(target="en")
        page = sorted(record.pages, key=lambda p: p.page_number)[1]
        self.assertEqual(page.inplace_status, "kept_original")

    def test_skip_mode_does_not_touch_oversized_page(self):
        _runner, record, _client = self._run(route="image")
        page = sorted(record.pages, key=lambda p: p.page_number)[1]
        self.assertFalse(page.inplace_requested)
        self.assertTrue(page.skipped_oversize)

    def test_normal_page_with_text_layer_goes_in_place(self):
        _runner, record, client = self._run(route="inplace", sizes=(A4, A4), text=(True, False))
        pages = sorted(record.pages, key=lambda p: p.page_number)
        self.assertEqual(pages[0].inplace_status, "done")    # had a text layer
        self.assertFalse(pages[1].inplace_requested)         # no text: image route
        self.assertEqual(len(client.calls), 1)


    def test_without_an_image_model_image_pages_are_kept_and_listed(self):
        _runner, record, client = self._run(route="inplace", sizes=(A4, A3), text=(False, True),
                                            image_model_missing="还没有填写 API 密钥")
        pages = sorted(record.pages, key=lambda p: p.page_number)
        self.assertEqual(len(client.calls), 0)
        self.assertTrue(pages[0].kept_reason)          # scan-like page: needed the image model
        self.assertEqual(pages[1].inplace_status, "done")
        self.assertEqual(record.status, PDF_OUTPUT_STATE_NEEDS_REVIEW)
        self.assertEqual(record.skipped_oversize_page_count, 0)

    def test_text_document_goes_to_word(self):
        from core.pdf_word_route import WordRouteResult

        calls = []

        def fake_word(pdf_path, work_dir, final_path, settings, **kwargs):
            calls.append(pdf_path)
            final_path.parent.mkdir(parents=True, exist_ok=True)
            final_path.write_bytes(b"docx")
            return WordRouteResult(ok=True, output_path=str(final_path))

        with patch("core.pdf_word_route.translate_pdf_via_word", fake_word), \
                patch("core.pdf_image_translation.classify_document") as classify:
            from core.pdf_routes import DocumentKind, PageKind

            classify.return_value = DocumentKind("text", [PageKind(1, "text", False), PageKind(2, "text", False)])
            runner, record, client = self._run(route="word", sizes=(A4, A4))
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(client.calls), 0)          # no page went to the image model
        self.assertEqual(record.translation_route, "word")
        self.assertTrue(record.translated_docx_path.endswith(".docx"))
        self.assertEqual(record.status, "completed")
        snap = runner.pdf_page_snapshot()["files"][0]
        self.assertEqual(snap["translation_route"], "word")
        self.assertEqual(snap["pages"], [])

    def test_failed_word_conversion_fails_only_that_file(self):
        from core.pdf_word_route import WordRouteResult

        def fake_word(*_args, **_kwargs):
            return WordRouteResult(ok=False, error="转成 Word 失败")

        with patch("core.pdf_word_route.translate_pdf_via_word", fake_word), \
                patch("core.pdf_image_translation.classify_document") as classify:
            from core.pdf_routes import DocumentKind, PageKind

            classify.return_value = DocumentKind("text", [PageKind(1, "text", False), PageKind(2, "text", False)])
            _runner, record, _client = self._run(route="word", sizes=(A4, A4))
        self.assertEqual(record.status, "failed")
        self.assertIn("整页图像翻译", record.error)


class TextLayerTests(unittest.TestCase):
    def test_invisible_ocr_text_of_a_searchable_scan_does_not_count(self):
        import pypdfium2 as pdfium

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "scan.pdf"
        doc = fitz.open()
        page = doc.new_page(width=A4[0], height=A4[1])
        page.insert_text((60, 120), "Plan de toiture et détails de la toiture " * 2, fontsize=12, render_mode=3)
        visible = doc.new_page(width=A4[0], height=A4[1])
        visible.insert_text((60, 120), "Plan de toiture et détails de la toiture " * 2, fontsize=12)
        doc.save(path)
        pdf = pdfium.PdfDocument(path)
        try:
            self.assertFalse(_page_has_text_layer(pdf.get_page(0), str(path), 0))
            self.assertTrue(_page_has_text_layer(pdf.get_page(1), str(path), 1))
        finally:
            pdf.close()


class ThresholdTests(unittest.TestCase):
    def test_thresholds(self):
        self.assertTrue(is_oversized_for_threshold("A3", *A3))
        self.assertFalse(is_oversized_for_threshold("A3", *A4))
        self.assertFalse(is_oversized_for_threshold("A2", *A3))
        self.assertTrue(is_oversized_for_threshold("A1", *A1))
        self.assertTrue(is_oversized_for_threshold("A1", A1[1], A1[0]))  # landscape
        self.assertFalse(is_oversized_for_threshold("A0", *A1))


if __name__ == "__main__":
    unittest.main()
