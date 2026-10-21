"""「原位替换文字（保留矢量，Beta）」接入 PDF 任务的端到端测试。"""
from __future__ import annotations

import tempfile
import unittest

import pytest
from pathlib import Path
from unittest.mock import patch


from core.api_scheduler import WeightedApiScheduler
from core.model_roles import ROLE_IMAGE, SOURCE_INDEPENDENT, resolve_effective_model_config
from core.pdf_image_translation import (
    PDF_OUTPUT_STATE_NEEDS_REVIEW,
    PdfFileItem,
    _page_has_text_layer,
    PdfImageTranslationRunner,
    max_page_generation_attempts,
)
from core.pdf_vector import InplaceHooks, find_cjk_font
from settings import AppSettings, PdfSettings
from tests.test_pdf_image_translation import _png_bytes, _RecordingImageClient

# PyMuPDF 属于「PDF 处理组件」，不随安装包分发；没装它的环境跳过这些测试。
fitz = pytest.importorskip("fitz")

A4 = (595.276, 841.89)
A3 = (841.89, 1190.551)
A2 = (1190.551, 1683.78)   # 长边超过 Word 的页面上限，按图纸和大图处理
A1 = (1683.78, 2383.94)

LABELS = {
    "Plan de toiture": "屋面平面图",
    "Mur coupe feu 1 heure": "1小时防火墙",
    "Détection automatique d'incendie": "自动火灾探测",
}


def _write_pdf(path: Path, sizes, *, with_text: list[bool], blank=()) -> None:
    doc = fitz.open()
    for index, ((w, h), text) in enumerate(zip(sizes, with_text)):
        page = doc.new_page(width=w, height=h)
        if index in blank:
            continue  # nothing on it at all: a blank page
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
    def _run(self, *, route="auto", target="zh", hooks=None, sizes=(A4, A2), text=(True, True),
             image_model_missing="", extra=None, office=None, blank=(), prefill=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        source_pdf = root / "source.pdf"
        _write_pdf(source_pdf, list(sizes), with_text=list(text), blank=blank)
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
        office = office or ("", "no office engine in tests")
        with patch("core.model_roles.get_key", return_value="secret"), \
                patch("core.pdf_word_route.word_route_available", return_value=True), \
                patch("core.pdf_word_route.office_pdf_engine", return_value=office):
            prepared = runner._prepare_pdf_files(output_dir=root / "out", app_managed=True)[0]
            if prefill:
                prefill(prepared)   # what 「继续上次翻译」 adopts from the previous run
            runner._run_word_segments([prepared])
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
        runner, record, client = self._run(extra={"auto_text_route": "image"})
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
            self.assertAlmostEqual(out[1].rect.width, A2[0], places=1)
        snap = runner.pdf_page_snapshot()["files"][0]["pages"][1]
        self.assertEqual(snap["inplace_status"], "done")
        self.assertEqual(snap["inplace_written"], 3)

    def test_without_the_pdf_component_drawings_take_the_safety_net(self):
        # 没装「PDF 处理组件」：页面类型照常判断（pypdfium2），原位替换的页改用保底方式，
        # 大幅面图纸保留原样，A4 文字页照常走图像翻译。
        with patch("core.pdf_component.is_available", return_value=False):
            runner, record, client = self._run(extra={"auto_text_route": "image"})
        self.assertEqual(len(client.calls), 1)
        page = sorted(record.pages, key=lambda p: p.page_number)[1]
        self.assertFalse(page.inplace_status)
        with fitz.open(record.translated_pdf_path) as out:
            self.assertIn("Plan de toiture", out[1].get_text())  # kept as it is
        logs = "\n".join(str(getattr(msg, "message", "")) for msg in list(runner._queue.queue))
        self.assertIn("未安装「PDF 处理组件」", logs)

    def test_failed_translation_keeps_the_original_page(self):
        def broken(_texts, _prompt):
            raise RuntimeError("service down")

        _runner, record, _client = self._run(hooks=_hooks(broken))
        page = sorted(record.pages, key=lambda p: p.page_number)[1]
        self.assertEqual(page.inplace_status, "kept_original")
        # 试过没换成，不是「因幅面过大被跳过」
        self.assertEqual(record.skipped_oversize_page_count, 0)
        self.assertNotIn("幅面", page.inplace_message)
        with fitz.open(record.translated_pdf_path) as out:
            self.assertIn("Plan de toiture", out[1].get_text())

    def test_large_page_where_only_rotated_text_was_found_says_so(self):
        # 只有一页大幅面图纸，识别到的字都是旋转的、没能确认：页面和整份文件都要照实说，
        # 不能说成「全部页面因幅面过大被跳过，请关闭「跳过 A3 及更大的页面」」。
        from core.pdf_task_report import _file_record_to_result
        from core.pdf_vector import InplaceResult

        rotated = InplaceResult(ok=True, written=0, review=2, review_items=[
            {"text": "NIVEAU R+1", "reason": "旋转的文字未能确认", "box_px": [0, 0, 1, 1]},
            {"text": "COUPE A-A", "reason": "旋转的文字未能确认", "box_px": [0, 0, 1, 1]},
        ])
        with patch("core.pdf_vector.translate_page_inplace", return_value=rotated):
            runner, record, client = self._run(sizes=(A2,), text=(True,))
        self.assertEqual(len(client.calls), 0)
        page = record.pages[0]
        self.assertEqual(page.inplace_status, "kept_original")
        self.assertEqual(page.inplace_message, "识别到 2 处文字，均未替换（旋转的文字未能确认），这一页保留原样")
        self.assertEqual(record.skipped_oversize_page_count, 0)
        self.assertEqual(record.status, PDF_OUTPUT_STATE_NEEDS_REVIEW)
        self.assertNotIn("幅面", record.error)
        self.assertEqual(
            record.error,
            "本文件没有翻译任何内容，输出 PDF 与源文件一致。原因：识别到 2 处文字，均未替换（旋转的文字未能确认），这一页保留原样。",
        )
        result = _file_record_to_result(record)
        self.assertFalse(result["success"])
        self.assertTrue(result["untranslated"])
        self.assertFalse(result["all_pages_skipped_oversize"])
        snap = runner.pdf_page_snapshot()["files"][0]["pages"][0]
        self.assertEqual(snap["inplace_status"], "kept_original")
        self.assertIn("旋转的文字未能确认", snap["inplace_message"])

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

    def test_non_chinese_target_keeps_the_large_page_original(self):
        _runner, record, _client = self._run(target="en")
        page = sorted(record.pages, key=lambda p: p.page_number)[1]
        self.assertFalse(page.inplace_requested)       # in place only writes Chinese
        self.assertIn("只支持译成中文", page.kept_reason)  # large pages never fall back to image

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
        _runner, record, client = self._run(route="inplace", sizes=(A4, A2), text=(False, True),
                                            image_model_missing="还没有填写 API 密钥")
        pages = sorted(record.pages, key=lambda p: p.page_number)
        self.assertEqual(len(client.calls), 0)
        self.assertTrue(pages[0].kept_reason)          # scan-like page: needed the image model
        self.assertEqual(pages[1].inplace_status, "done")
        self.assertEqual(record.status, PDF_OUTPUT_STATE_NEEDS_REVIEW)
        self.assertEqual(record.skipped_oversize_page_count, 0)

    def _fake_word_and_pdf(self, pages_out=1):
        from core.pdf_word_route import WordRouteResult

        def fake_word(pdf_path, work_dir, final_path, settings, **kwargs):
            final_path.parent.mkdir(parents=True, exist_ok=True)
            final_path.write_bytes(b"docx")
            return WordRouteResult(ok=True, output_path=str(final_path))

        def fake_docx_to_pdf(_docx, pdf_path, **_kwargs):
            doc = fitz.open()
            for _ in range(pages_out):
                doc.new_page(width=A4[0], height=A4[1]).insert_text((60, 100), "SEGMENT-TRANSLATED", fontsize=12)
            doc.save(pdf_path)
            return "word"

        return fake_word, fake_docx_to_pdf

    def test_text_document_comes_back_as_pdf(self):
        fake_word, fake_pdf = self._fake_word_and_pdf()
        with patch("core.pdf_word_route.translate_pdf_via_word", fake_word), \
                patch("core.pdf_word_route.docx_to_pdf", fake_pdf):
            _runner, record, client = self._run(sizes=(A4, A4), office=("word", "test"))
        self.assertEqual(len(client.calls), 0)
        self.assertTrue(record.translated_pdf_path.endswith(".pdf"))
        self.assertEqual(record.translated_docx_path, "")          # no Word unless asked for
        with fitz.open(record.translated_pdf_path) as out:
            self.assertIn("SEGMENT-TRANSLATED", out[0].get_text())

    def test_also_save_word_for_a_whole_text_document(self):
        fake_word, fake_pdf = self._fake_word_and_pdf()
        with patch("core.pdf_word_route.translate_pdf_via_word", fake_word), \
                patch("core.pdf_word_route.docx_to_pdf", fake_pdf):
            _runner, record, _client = self._run(sizes=(A4, A4), office=("word", "test"),
                                                 extra={"also_save_word": True})
        self.assertTrue(record.translated_docx_path.endswith(".docx"))
        self.assertEqual(Path(record.translated_docx_path).parent, Path(record.translated_pdf_path).parent)

    def test_no_word_copy_for_a_mixed_file(self):
        fake_word, fake_pdf = self._fake_word_and_pdf()
        with patch("core.pdf_word_route.translate_pdf_via_word", fake_word), \
                patch("core.pdf_word_route.docx_to_pdf", fake_pdf):
            _runner, record, _client = self._run(sizes=(A4, A2), office=("word", "test"),
                                                 extra={"also_save_word": True})
        self.assertEqual(record.translated_docx_path, "")

    def test_text_pages_of_a_mixed_file_become_a_word_segment(self):
        from core.pdf_word_route import WordRouteResult

        def fake_word(pdf_path, work_dir, final_path, settings, **kwargs):
            with fitz.open(pdf_path) as part:
                self.assertEqual(len(part), 2)          # both text pages, one segment
            final_path.parent.mkdir(parents=True, exist_ok=True)
            final_path.write_bytes(b"docx")
            return WordRouteResult(ok=True, output_path=str(final_path))

        def fake_docx_to_pdf(_docx, pdf_path, **_kwargs):
            doc = fitz.open()
            doc.new_page(width=A4[0], height=A4[1]).insert_text((60, 100), "SEGMENT-TRANSLATED", fontsize=12)
            doc.save(pdf_path)
            return "word"

        with patch("core.pdf_word_route.translate_pdf_via_word", fake_word), \
                patch("core.pdf_word_route.docx_to_pdf", fake_docx_to_pdf):
            _runner, record, client = self._run(sizes=(A4, A4, A2), text=(True, True, True),
                                                office=("word", "test"))
        pages = sorted(record.pages, key=lambda p: p.page_number)
        self.assertEqual([p.word_status for p in pages], ["done", "done", ""])
        self.assertEqual(pages[2].inplace_status, "done")
        self.assertEqual(len(client.calls), 0)
        self.assertEqual(len(record.word_segments), 1)
        self.assertTrue(record.word_segments[0]["docx_path"].endswith("_p1-2_translated.docx"))
        out_dir = Path(record.translated_pdf_path).parent
        self.assertEqual(list(out_dir.glob("*.docx")), [])   # the Word stays in the process folder
        with fitz.open(record.translated_pdf_path) as out:
            self.assertEqual(len(out), 2)               # 2 text pages became 1 translated page
            self.assertIn("SEGMENT-TRANSLATED", out[0].get_text())
            self.assertIn("屋面平面图", out[1].get_text())

    def _word_spy(self, seen: list[int]):
        from core.pdf_word_route import WordRouteResult

        def fake_word(pdf_path, work_dir, final_path, settings, **kwargs):
            with fitz.open(pdf_path) as part:
                seen.append(len(part))
            final_path.parent.mkdir(parents=True, exist_ok=True)
            final_path.write_bytes(b"docx")
            return WordRouteResult(ok=True, output_path=str(final_path))

        return fake_word

    def test_page_reused_by_resume_is_not_translated_again_via_word(self):
        from core.pdf_models import PdfPageRecord

        def prefill(prepared):
            image = Path(prepared.translated_pages_dir) / "page_002.png"
            image.parent.mkdir(parents=True, exist_ok=True)
            image.write_bytes(_png_bytes(100, 141))
            prepared.record.pages.append(PdfPageRecord(
                page_number=2, source_image_path="", file_name="source", translated_image_path=str(image),
                status="success", attempts=1, page_width_pt=A4[0], page_height_pt=A4[1]))

        seen: list[int] = []
        _fake, fake_pdf = self._fake_word_and_pdf()
        with patch("core.pdf_word_route.translate_pdf_via_word", self._word_spy(seen)), \
                patch("core.pdf_word_route.docx_to_pdf", fake_pdf):
            _runner, record, client = self._run(sizes=(A4, A4, A4), text=(True, True, True),
                                                office=("word", "test"), prefill=prefill)
        self.assertEqual(seen, [1, 1])                  # pages 1 and 3, each on its own
        self.assertEqual([(s["first_page"], s["last_page"]) for s in record.word_segments], [(1, 1), (3, 3)])
        self.assertEqual(len(client.calls), 0)
        with fitz.open(record.translated_pdf_path) as out:
            self.assertEqual(len(out), 3)               # segment, reused page, segment: nothing twice

    def test_blank_page_inside_a_text_document_stays_in_one_segment(self):
        seen: list[int] = []
        _fake, fake_pdf = self._fake_word_and_pdf(pages_out=3)
        with patch("core.pdf_word_route.translate_pdf_via_word", self._word_spy(seen)), \
                patch("core.pdf_word_route.docx_to_pdf", fake_pdf):
            _runner, record, client = self._run(sizes=(A4, A4, A4), text=(True, False, True), blank=(1,),
                                                office=("word", "test"), extra={"also_save_word": True})
        self.assertEqual(seen, [3])
        pages = sorted(record.pages, key=lambda p: p.page_number)
        self.assertEqual([p.word_status for p in pages], ["done", "done", "done"])
        self.assertTrue(record.translated_docx_path.endswith(".docx"))
        self.assertEqual(len(client.calls), 0)
        with fitz.open(record.translated_pdf_path) as out:
            self.assertEqual(len(out), 3)

    def test_rerouted_and_kept_pages_are_explained_in_the_report(self):
        from core.pdf_task_report import _file_record_to_result, _route_report_lines

        _runner, record, _client = self._run(target="en", sizes=(A4, A2), text=(True, True),
                                             extra={"auto_text_route": "inplace"})
        lines = "\n".join(_route_report_lines(record))
        self.assertIn("第 1 页：原位替换目前只支持译成中文，改用整页图像翻译", lines)
        self.assertIn("第 2 页：原位替换目前只支持译成中文，这一页保留原样", lines)
        detail = _file_record_to_result(record)
        text = str(detail)
        self.assertNotIn("没有设置图像翻译模型", text)
        self.assertIn("改用整页图像翻译", text)

    def test_failed_word_segment_falls_back_to_the_safety_net(self):
        from core.pdf_word_route import WordRouteResult

        def broken_word(*_args, **_kwargs):
            return WordRouteResult(ok=False, error="转成 Word 失败")

        with patch("core.pdf_word_route.translate_pdf_via_word", broken_word):
            _runner, record, client = self._run(sizes=(A4, A2), text=(True, True), office=("word", "test"))
        first = sorted(record.pages, key=lambda p: p.page_number)[0]
        self.assertFalse(first.word_requested)
        self.assertIn("改用整页图像翻译", first.route_note)
        self.assertEqual(len(client.calls), 1)          # the safety net: whole-page image translation
        self.assertEqual(record.word_segments[0]["status"], "failed")
        self.assertEqual(record.status, PDF_OUTPUT_STATE_NEEDS_REVIEW)

    def test_no_office_engine_sends_text_pages_to_the_safety_net(self):
        _runner, record, client = self._run(sizes=(A4, A2), text=(True, True))   # office probe says none
        first = sorted(record.pages, key=lambda p: p.page_number)[0]
        self.assertIn("Microsoft Word", first.route_note)
        self.assertEqual(len(client.calls), 1)

    def test_safety_net_keep_original(self):
        _runner, record, client = self._run(sizes=(A4, A2), text=(True, True),
                                            extra={"auto_fallback": "skip"})
        first = sorted(record.pages, key=lambda p: p.page_number)[0]
        self.assertTrue(first.kept_reason)
        self.assertEqual(len(client.calls), 0)


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


class WordPageSizeTests(unittest.TestCase):
    def test_pages_larger_than_word_allows(self):
        from core.pdf_routes import exceeds_word_page

        self.assertFalse(exceeds_word_page(*A4))
        self.assertFalse(exceeds_word_page(*A3))
        self.assertFalse(exceeds_word_page(A3[1], A3[0]))   # landscape A3
        self.assertTrue(exceeds_word_page(1190.551, 1683.78))  # A2: 594 mm > 558.8 mm
        self.assertTrue(exceeds_word_page(A1[1], A1[0]))


if __name__ == "__main__":
    unittest.main()


class WordRouteTaskTests(unittest.TestCase):
    """The whole task (``_run``) with a word-route file: stop and review status."""

    def _run_task(self, fake_word, *, route="auto", sizes=(A4, A4), threshold="A3", text=(True, True), kinds=None):
        from core.pdf_routes import DocumentKind, PageKind
        from core.task_runner import DoneMsg, ErrorMsg, StoppedMsg

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        src = root / "source.pdf"
        _write_pdf(src, list(sizes), with_text=list(text))
        settings = AppSettings(target_lang="zh")
        settings.pdf = PdfSettings.model_validate({"target_lang": "zh", "translation_route": route,
                                                   "oversized_page_threshold": threshold})
        settings.image_model_role.source_role = SOURCE_INDEPENDENT
        settings.image_model_role.cloud_provider = "custom_openai"
        settings.image_model_role.cloud_model = "image-model"
        settings.image_model_role.cloud_base_url = "https://images.example/v1"
        client = _RecordingImageClient(_png_bytes(10, 10))
        runner = PdfImageTranslationRunner(
            [PdfFileItem(path=src, name="source", size_kb=1.0, page_count=len(sizes))], settings,
            source_root=root, image_client=client, task_logger_enabled=False, inplace_hooks=_hooks())
        kinds = kinds or DocumentKind("text", [PageKind(i + 1, "text", False) for i in range(len(sizes))])
        def fake_docx_to_pdf(_docx, pdf_path, **_kwargs):
            doc = fitz.open()
            doc.new_page(width=A4[0], height=A4[1]).insert_text((60, 100), "SEGMENT", fontsize=12)
            doc.save(pdf_path)
            return "word"

        with patch("core.pdf_word_route.translate_pdf_via_word", lambda *a, **k: fake_word(runner, *a, **k)), \
                patch("core.pdf_word_route.docx_to_pdf", fake_docx_to_pdf), \
                patch("core.pdf_word_route.office_pdf_engine", return_value=("word", "test")), \
                patch("core.pdf_word_route.word_route_available", return_value=True), \
                patch("core.pdf_image_translation.classify_document", return_value=kinds), \
                patch("core.model_roles.get_key", return_value="secret"), \
                patch("core.pdf_image_translation.record_image_model_availability", lambda *a, **k: None):
            runner._run()
        final = None
        while True:
            message = runner.get_message(timeout=0.01)
            if message is None:
                break
            if isinstance(message, (DoneMsg, StoppedMsg, ErrorMsg)):
                final = message
        return runner, final, client

    def test_stop_during_the_word_run_stops_the_task(self):
        from core.pdf_word_route import WordRouteResult
        from core.task_runner import StoppedMsg

        def fake(runner, *_args, **_kwargs):
            runner.stop()
            return WordRouteResult(ok=False, stopped=True, error="任务已停止")

        runner, final, _client = self._run_task(fake)
        self.assertIsInstance(final, StoppedMsg)
        self.assertEqual(runner._prepared_files[0].record.word_segments[0]["status"], "stopped")

    def test_pause_holds_the_next_word_segment(self):
        import threading

        from core.pdf_routes import DocumentKind, PageKind
        from core.pdf_word_route import WordRouteResult
        from core.task_runner import DoneMsg

        events: list[str] = []

        def fake(runner, _pdf, _work, final_path, _settings, **_kwargs):
            if not events:
                runner.pause()
                threading.Timer(0.3, lambda: (events.append("resumed"), runner.resume())).start()
            events.append("segment")
            final_path.parent.mkdir(parents=True, exist_ok=True)
            final_path.write_bytes(b"docx")
            return WordRouteResult(ok=True, output_path=str(final_path))

        kinds = DocumentKind("mixed", [PageKind(1, "text", False), PageKind(2, "scan", False),
                                       PageKind(3, "text", False)])
        _runner, final, _client = self._run_task(fake, sizes=(A4, A4, A4), text=(True, True, True), kinds=kinds)
        self.assertIsInstance(final, DoneMsg)
        self.assertEqual(events, ["segment", "resumed", "segment"])   # the second waited for 继续

    def test_word_issues_make_the_task_need_review(self):
        from core.pdf_word_route import WordRouteResult
        from core.task_runner import DoneMsg

        def fake(_runner, _pdf, _work, final_path, _settings, **_kwargs):
            final_path.parent.mkdir(parents=True, exist_ok=True)
            final_path.write_bytes(b"docx")
            return WordRouteResult(ok=True, output_path=str(final_path), issues=[{"problem": "kept"}])

        _runner, final, _client = self._run_task(fake)
        self.assertIsInstance(final, DoneMsg)
        self.assertTrue(final.issues)
        self.assertEqual(final.file_results[0]["status"], "needs_review")
        self.assertEqual(final.file_results[0]["output_format"], "pdf")

    def test_a3_text_pages_go_to_word(self):
        # 先看内容再看尺寸：A3 放得进 Word，文字页照常转 Word，不再按大幅面送去原位替换。
        from core.pdf_routes import DocumentKind, PageKind
        from core.pdf_word_route import WordRouteResult
        from core.task_runner import DoneMsg

        calls: list[int] = []

        def fake(_runner, _pdf, _work, final_path, _settings, **_kwargs):
            calls.append(1)
            final_path.parent.mkdir(parents=True, exist_ok=True)
            final_path.write_bytes(b"docx")
            return WordRouteResult(ok=True, output_path=str(final_path))

        kinds = DocumentKind("text", [PageKind(1, "text", False), PageKind(2, "text", False)])
        runner, final, client = self._run_task(fake, sizes=(A3, A3), kinds=kinds)
        self.assertIsInstance(final, DoneMsg)
        self.assertEqual(calls, [1])
        self.assertEqual(len(client.calls), 0)
        pages = runner._prepared_files[0].record.pages
        self.assertFalse(any(page.inplace_requested for page in pages))
        self.assertEqual(runner._prepared_files[0].record.skipped_oversize_page_count, 0)

    def test_large_scans_are_scanned_pages(self):
        from core.pdf_routes import DocumentKind, PageKind
        from core.task_runner import DoneMsg

        def never(*_args, **_kwargs):
            raise AssertionError("not a text page")

        kinds = DocumentKind("scan", [PageKind(1, "scan", True)])
        runner, final, client = self._run_task(never, sizes=(A1,), text=(False,), kinds=kinds)
        self.assertIsInstance(final, DoneMsg)
        self.assertGreaterEqual(len(client.calls), 1)   # 扫描页的首选：整页图像翻译
        self.assertFalse(runner._prepared_files[0].record.pages[0].inplace_requested)

    def test_skipped_large_pages_count_whatever_the_saved_threshold(self):
        from core.task_runner import DoneMsg

        def never(*_args, **_kwargs):
            raise AssertionError("not a text document")

        runner, final, client = self._run_task(never, route="image", sizes=(A3, A3), threshold="A1")
        self.assertEqual(len(client.calls), 0)
        record = runner._prepared_files[0].record
        self.assertEqual(record.skipped_oversize_page_count, 2)
        self.assertIsInstance(final, DoneMsg)
        self.assertFalse(final.file_results[0]["success"])   # nothing was translated


@unittest.skipUnless(find_cjk_font(), "no CJK font on this machine")
class InplaceApiGaveUpTests(unittest.TestCase):
    """接口反复失败、放弃的文字：按 CAD 的口径记「未获得译文」并标需复核，
    不能被当成「模型有意保留原文」（数字和编号那一类）而没人复核。"""

    _run = InplaceTaskTests._run

    def _service_hooks(self, fail: set[str]):
        from api.surfaces.pdf import _inplace_hooks
        from core import tm_manager

        calls: list[list[str]] = []

        class Engine:
            engine_name = "openai/fake"

            def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
                calls.append(list(texts))
                if fail & set(texts):
                    raise ValueError("503 Service Unavailable")
                return {text: LABELS.get(text, text) for text in texts}

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        settings = AppSettings(target_lang="zh")
        settings.pdf = PdfSettings.model_validate({"target_lang": "zh"})
        settings.rate.pdf_text.batch_items = 10
        settings.rate.pdf_text.concurrency = 1
        for patcher in (
            patch("core.engine_dispatcher.build_role_engine", return_value=Engine()),
            patch("core.engine_dispatcher._backoff_sleep", lambda *args, **kwargs: None),
            patch("core.ocr_engine.resolve_tesseract", return_value=None),
            patch.object(tm_manager, "DB_PATH", Path(tmp.name) / "tm.db"),
            patch.object(tm_manager, "BACKUPS_DIR", Path(tmp.name) / "backups"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        hooks = _inplace_hooks(settings, ())
        hooks.source_lang = "fr"
        return hooks, calls

    def test_labels_the_api_gave_up_on_are_untranslated_and_need_review(self):
        from core.pdf_task_report import _inplace_report_lines, _summary_issues

        hooks, calls = self._service_hooks({"Mur coupe feu 1 heure"})
        runner, record, _client = self._run(hooks=hooks, sizes=(A2,), text=(True,))
        page = record.pages[0]

        self.assertEqual(page.inplace_status, "done")
        self.assertEqual(page.inplace_written, 2)
        self.assertEqual(page.inplace_untranslated, 1)
        # 不算「认不准」，也不算「数字和编号等不需要翻译」
        self.assertEqual(page.inplace_review, 0)
        self.assertEqual(page.inplace_kept, 0)
        self.assertIn("1 处未获得译文，需复核", page.inplace_message)
        self.assertIn({"text": "Mur coupe feu 1 heure", "reason": "未获得译文", "kind": "untranslated"},
                      [{k: item[k] for k in ("text", "reason", "kind") if k in item} for item in page.inplace_review_items])
        # 页面层面再试过一次，仍没有译文才这样记
        self.assertGreaterEqual(sum(texts.count("Mur coupe feu 1 heure") for texts in calls), 2)
        self.assertEqual(record.status, PDF_OUTPUT_STATE_NEEDS_REVIEW)
        snap = runner.pdf_page_snapshot()["files"][0]["pages"][0]
        self.assertEqual(snap["inplace_untranslated"], 1)
        issues = _summary_issues([record])
        self.assertIn(("第 1 页", "1 处未获得译文，已保留原文，需复核"),
                      [(issue["location_label"], issue["problem"]) for issue in issues])
        report = "\n".join(_inplace_report_lines(record))
        self.assertIn("未获得译文 1 处（需复核）", report)
        self.assertIn("未获得译文「Mur coupe feu 1 heure」", report)
        with fitz.open(record.translated_pdf_path) as out:
            text = out[0].get_text()
            self.assertIn("屋面平面图", text)
            self.assertIn("Mur coupe feu 1 heure", text)  # 原文留在图上

    def test_api_giving_up_on_every_label_keeps_the_page_and_says_why(self):
        hooks, _calls = self._service_hooks(set(LABELS))
        _runner, record, _client = self._run(hooks=hooks, sizes=(A2,), text=(True,))
        page = record.pages[0]

        self.assertEqual(page.inplace_status, "kept_original")
        self.assertEqual(page.inplace_untranslated, 3)
        self.assertEqual(page.inplace_message, "识别到 3 处文字，均未替换（未获得译文），这一页保留原样")
        self.assertEqual(record.status, PDF_OUTPUT_STATE_NEEDS_REVIEW)


@unittest.skipUnless(find_cjk_font(), "no CJK font on this machine")
class InplaceProgressTests(unittest.TestCase):
    """原位替换的页属于「翻译」阶段：进度按识别、翻译返回、写回的真实完成量推进，逐页结果
    写「原位替换中」，运行日志逐批说明。以前它在翻译阶段一开头就记成「已完成」，真正的活
    放在「生成文件」里做：进度条几秒就到 90%，然后几分钟不动、日志一行没有。"""

    _service_hooks = InplaceApiGaveUpTests._service_hooks

    def test_whole_task_reports_in_place_work_inside_the_translate_phase(self):
        from core.task_runner_messages import LogMsg, ProgressMsg, StatusMsg

        hooks, _calls = self._service_hooks(set())
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        source_pdf = root / "drawing.pdf"
        _write_pdf(source_pdf, [A4, A1], with_text=[True, True])
        settings = AppSettings(target_lang="zh")
        settings.pdf = PdfSettings.model_validate({"target_lang": "zh", "translation_route": "auto",
                                                   "auto_text_route": "image"})
        settings.output.use_custom_output_dir = True
        settings.output.custom_output_dir = str(root / "out")
        settings.image_model_role.source_role = SOURCE_INDEPENDENT
        settings.image_model_role.cloud_provider = "custom_openai"
        settings.image_model_role.cloud_model = "image-model"
        settings.image_model_role.cloud_base_url = "https://images.example/v1"
        runner = PdfImageTranslationRunner(
            [PdfFileItem(path=source_pdf, name="drawing.pdf", size_kb=1.0, page_count=2)],
            settings, source_root=root, image_client=_RecordingImageClient(_png_bytes(2481, 3508)),
            task_logger_enabled=False, inplace_hooks=hooks,
        )
        seen: dict[str, object] = {}
        translate = hooks.translate

        def spying_translate(texts, prompt, **extra):
            # 翻译请求发出时：图像页早已返回，原位替换页正在做。
            if "snapshot" not in seen:
                seen["snapshot"] = runner.pdf_page_snapshot()["files"][0]["pages"][1]
            return translate(texts, prompt, **extra)

        hooks.translate = spying_translate
        with patch("core.model_roles.get_key", return_value="secret"), \
                patch("core.pdf_word_route.word_route_available", return_value=True), \
                patch("core.pdf_word_route.office_pdf_engine", return_value=("", "no office engine in tests")):
            runner._run()

        messages = []
        while not runner._queue.empty():
            messages.append(runner._queue.get_nowait())
        record = runner._prepared_files[0].record
        self.assertEqual(sorted(record.pages, key=lambda p: p.page_number)[1].inplace_status, "done")

        # 进度：原位替换的页做完以前，「翻译」阶段到不了头，也不会进「生成文件」。
        logs = [m.message for m in messages if isinstance(m, LogMsg)]
        order = [m for m in messages if isinstance(m, (ProgressMsg, LogMsg))]
        writing = next(i for i, m in enumerate(order)
                       if isinstance(m, LogMsg) and "第 2 页：译文已返回" in m.message)
        finished = next(i for i, m in enumerate(order)
                        if isinstance(m, LogMsg) and "第 2 页：已替换" in m.message)
        translate_steps = [m.step_done / m.step_total for m in order[:writing]
                           if isinstance(m, ProgressMsg) and m.phase_index == 2]
        self.assertTrue(translate_steps)
        self.assertLess(max(translate_steps), 1.0)
        self.assertEqual(translate_steps, sorted(translate_steps))  # 只进不退
        # 图像页返回后是一半；原位替换这一页一点一点往上走，不是一下跳满。
        self.assertGreater(len({round(v, 3) for v in translate_steps if 0.5 < v < 1.0}), 1)
        self.assertFalse([m for m in order[:finished] if isinstance(m, ProgressMsg) and m.phase_index >= 3])
        self.assertTrue([m for m in order if isinstance(m, ProgressMsg) and m.phase_index == 2
                         and m.step_done == m.step_total])

        # 进度卡上的计数：原位替换这一页做完以前，既不算「已生成」也不算「原样保留」。
        from core.task_runner_messages import PdfPageRecoveryStatusMsg

        started = next(i for i, m in enumerate(messages)
                       if isinstance(m, LogMsg) and m.message.endswith("第 2 页：原位替换文字（Beta）开始。"))
        live = [m for m in messages[:started] if isinstance(m, PdfPageRecoveryStatusMsg)][-1]
        self.assertEqual((live.completed_pages, live.skipped_oversize_page_count), (1, 0))
        final = [m for m in messages if isinstance(m, PdfPageRecoveryStatusMsg)][-1]
        self.assertEqual((final.completed_pages, final.skipped_oversize_page_count), (2, 0))

        # 逐页结果：正在做的这一页是「原位替换中」，不是「按幅面跳过」。
        snap = seen["snapshot"]
        self.assertTrue(snap["inplace_requested"])
        self.assertEqual(snap["inplace_status"], "")
        self.assertTrue(snap["inplace_active"])
        self.assertFalse(runner.pdf_page_snapshot()["files"][0]["pages"][1]["inplace_active"])

        # 日志：每个阶段都有一行，翻译按批说明。
        self.assertIn("[drawing.pdf] 第 2 页：原位替换文字（Beta）开始。", logs)
        self.assertTrue([line for line in logs if line.startswith("[drawing.pdf] 第 2 页：读出 ")])
        self.assertTrue([line for line in logs if line.startswith("[drawing.pdf 第 2 页] 开始翻译 3 处内容")])
        self.assertTrue([line for line in logs if line.startswith("[drawing.pdf 第 2 页] 第 1/1 批已返回")])
        self.assertIn("[drawing.pdf] 第 2 页：译文已返回，正在写回原位置并自检。", logs)
        statuses = [m.phase_desc for m in messages if isinstance(m, StatusMsg)]
        self.assertIn("状态：正在原位替换「drawing.pdf」第 2 页的文字…", statuses)

    def test_ocr_tiles_move_the_page_forward(self):
        """识别画成线条的字按块计：块数一出来就有一行日志，进度跟着识别完的块走。"""
        from core.pdf_image_translation import _InplacePageProgress

        sent: list[float] = []
        lines: list[str] = []

        class Runner:
            _inplace_page_fraction: dict = {}

            def _translation_progress_units(self):
                return 1.0 - 1.0 + self._inplace_page_fraction[("f", 1)]

            def _send_translation_progress(self, units, total_pages):
                sent.append(units / total_pages)

        progress = _InplacePageProgress(Runner(), ("f", 1), total_pages=1,
                                        log=lambda level, message: lines.append(message), batch_label="f 第 1 页")
        progress.report("ocr", 0, 40)
        for done in range(1, 41):
            progress.report("ocr", done, 40)
        progress.report("read", 1, 1, rows=12, seconds=3.2)
        self.assertEqual(lines, ["正在识别画成线条的文字，共 40 块。", "读出 12 处文字，用时 3 秒。"])
        self.assertAlmostEqual(sent[-1], 0.2, places=3)
        self.assertEqual(len(sent), 41)  # 起点，之后每识别完一块一步，没有按时间的估算
        progress.report("write", 0, 2)
        self.assertAlmostEqual(sent[-1], 0.9, places=3)  # 没走模型的页，翻译那一份直接越过
        progress.report("write", 2, 2)
        self.assertAlmostEqual(sent[-1], 1.0, places=3)


def test_word_segment_progress_folds_the_word_phases():
    """转 Word 的正文段：Word 流程每个阶段各自从 0 数起，要按 Word 的阶段权重折成整段的份额。"""
    from core.pdf_word_route import _word_run_fraction

    assert _word_run_fraction(1, 3, 1, 1) == pytest.approx(0.05)
    assert _word_run_fraction(2, 3, 5, 10) == pytest.approx(0.45)
    assert _word_run_fraction(3, 3, 1, 1) == pytest.approx(1.0)


class SharedDocumentProfileTests(unittest.TestCase):
    """「自动识别场景」：整份文件走同一种方式时沿用开头的说明，混合文件各部分自己写。"""

    def _runner(self, kinds, *, requester=True):
        from core.pdf_routes import DocumentKind, PageKind

        runner = PdfImageTranslationRunner(
            [], AppSettings(), image_client=_RecordingImageClient(_png_bytes(10, 10)),
            task_logger_enabled=False,
            profile_requester=(lambda lines, log: "电气图纸") if requester else None,
        )
        runner._document_kinds["a.pdf"] = DocumentKind("mixed", [PageKind(i + 1, kind, False)
                                                                 for i, kind in enumerate(kinds)])
        runner._document_contexts["a.pdf"] = "电气图纸"
        return runner

    def test_whole_file_reuses_the_opening_description(self):
        runner = self._runner(["drawing", "drawing", "blank"])
        self.assertEqual(runner._shared_document_profile("a.pdf", [1, 2]), "电气图纸")

    def test_mixed_file_asks_again_for_each_part(self):
        runner = self._runner(["text", "scan", "drawing"])
        self.assertIsNone(runner._shared_document_profile("a.pdf", [3]))
        self.assertIsNone(runner._shared_document_profile("a.pdf", [1]))

    def test_consecutive_inplace_pages_share_one_description(self):
        from types import SimpleNamespace

        runner = self._runner(["scan", "text", "drawing", "blank", "drawing", "text", "drawing"])
        record = SimpleNamespace(source_path="a.pdf", pages=[
            SimpleNamespace(page_number=n, inplace_requested=n in {3, 5, 7}) for n in range(1, 8)])
        asked: list[list[str]] = []

        def profile(texts):
            asked.append(list(texts))
            return f"说明{len(asked)}"

        self.assertEqual(runner._inplace_group_profile(record, 3, profile)(["p3"]), "说明1")
        self.assertEqual(runner._inplace_group_profile(record, 5, profile)(["p5"]), "说明1")  # 3–5 one group
        self.assertEqual(runner._inplace_group_profile(record, 7, profile)(["p7"]), "说明2")  # page 6 breaks it
        self.assertEqual(asked, [["p3"], ["p7"]])

    def test_switch_off_means_no_description(self):
        runner = self._runner(["drawing"], requester=False)
        self.assertEqual(runner._shared_document_profile("a.pdf", [1]), "")


def test_word_runner_uses_a_given_description_without_asking():
    from core.word_task_runner import WordTaskRunner

    runner = WordTaskRunner([], AppSettings(), document_profile="电气图纸")
    assert runner._preset_profile == "电气图纸"
