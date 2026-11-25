"""译图把整页内容画小了：量出来、重做一次、还小就交图并提示换模型。"""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

from core.api_scheduler import WeightedApiScheduler
from core.model_roles import ROLE_IMAGE, SOURCE_INDEPENDENT, resolve_effective_model_config
from core.pdf_image_translation import (
    CONTENT_SHRUNK_RETRY_FEEDBACK,
    PDF_QC_CONTENT_SHRUNK,
    PdfImageTranslationRunner,
    PdfPageRecord,
    check_page_quality,
)
from core.pdf_page_content_scale import content_scale
from settings import AppSettings

A4 = (1240, 1754)


def _page(size=A4, *, scale: float = 1.0, line_length: float = 1.0) -> Image.Image:
    """一页「文档」：页框 + 30 行文字条。scale<1 时整页内容缩小居中，四周留白。"""
    width, height = size
    inner_w, inner_h = int(width * scale), int(height * scale)
    inner = Image.new("RGB", (inner_w, inner_h), "white")
    draw = ImageDraw.Draw(inner)
    margin_x, margin_y = int(inner_w * 0.06), int(inner_h * 0.05)
    draw.rectangle([margin_x, margin_y, inner_w - margin_x, inner_h - margin_y], outline="black", width=4)
    usable = inner_w - 4 * margin_x
    for row in range(30):
        y = margin_y * 2 + row * int(inner_h * 0.027)
        draw.rectangle([margin_x * 2, y, margin_x * 2 + int(usable * line_length), y + 8], fill="black")
    page = Image.new("RGB", size, "white")
    page.paste(inner, ((width - inner_w) // 2, (height - inner_h) // 2))
    return page


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class ContentScaleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.source = Path(self._tmp.name) / "page.png"
        _page().save(self.source)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_same_page_is_full_size(self) -> None:
        scale = content_scale(_png(_page()), self.source)
        self.assertAlmostEqual(scale, 1.0, delta=0.02)

    def test_page_drawn_at_43_percent_is_measured_as_such(self) -> None:
        scale = content_scale(_png(_page(scale=0.43)), self.source)
        self.assertAlmostEqual(scale, 0.43, delta=0.03)

    def test_same_page_at_another_resolution_is_full_size(self) -> None:
        # 模型按 1440x2048 交图：画布大小变了，内容占比没变。
        scale = content_scale(_png(_page((1440, 2048))), self.source)
        self.assertAlmostEqual(scale, 1.0, delta=0.03)

    def test_shorter_translated_lines_are_not_shrinkage(self) -> None:
        # 译文每行短了四成：宽度上的内容变窄，但页框和行数都在原位。
        scale = content_scale(_png(_page(line_length=0.6)), self.source)
        self.assertGreater(scale, 0.95)

    def test_sparse_cover_page_is_not_judged(self) -> None:
        cover = Image.new("RGB", A4, "white")
        ImageDraw.Draw(cover).rectangle([400, 800, 840, 860], fill="black")
        cover_path = Path(self._tmp.name) / "cover.png"
        cover.save(cover_path)
        small_title = Image.new("RGB", A4, "white")
        ImageDraw.Draw(small_title).rectangle([520, 800, 720, 860], fill="black")
        self.assertIsNone(content_scale(_png(small_title), cover_path))

    def test_quality_gate_flags_shrunk_page_without_failing_it(self) -> None:
        result = check_page_quality(
            _png(_page(scale=0.43)),
            source_width=A4[0],
            source_height=A4[1],
            source_image_path=self.source,
        )
        self.assertTrue(result.ok)
        self.assertIn(PDF_QC_CONTENT_SHRUNK, result.review_reasons)
        self.assertIn("图像模型把这页内容画小了（只有原页的约 4", result.review_message)

        normal = check_page_quality(
            _png(_page()),
            source_width=A4[0],
            source_height=A4[1],
            source_image_path=self.source,
        )
        self.assertNotIn(PDF_QC_CONTENT_SHRUNK, normal.review_reasons)


class _SequenceImageClient:
    def __init__(self, results: list) -> None:
        self.results = list(results)
        self.calls: list[dict] = []

    def generate_page(self, **kwargs):
        self.calls.append(kwargs)
        result = self.results[min(len(self.calls) - 1, len(self.results) - 1)]
        if isinstance(result, Exception):
            raise result
        return result


class ShrunkPageRetryTests(unittest.TestCase):
    def _run(self, results: list, *, max_attempts: int = 2):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        settings = AppSettings(target_lang="en")
        settings.pdf.target_lang = "en"
        settings.output.use_custom_output_dir = True
        settings.output.custom_output_dir = str(root / "out")
        settings.pdf.page_generation_concurrency = 1
        settings.image_model_role.source_role = SOURCE_INDEPENDENT
        settings.image_model_role.cloud_provider = "custom_openai"
        settings.image_model_role.cloud_model = "gpt-image-2.6-flare"
        settings.image_model_role.cloud_base_url = "https://images.example/v1"
        client = _SequenceImageClient(results)
        runner = PdfImageTranslationRunner(
            [], settings, source_root=root, image_client=client, task_logger_enabled=False
        )
        source = root / "page_001.png"
        _page().save(source)
        record = PdfPageRecord(
            page_number=1,
            source_image_path=str(source),
            file_name="doc",
            status="rendered",
            source_width_px=A4[0],
            source_height_px=A4[1],
            page_width_pt=595,
            page_height_pt=842,
        )
        with patch("core.model_roles.get_key", return_value="secret"):
            model_config = resolve_effective_model_config(settings, ROLE_IMAGE)
            runner._generate_page_with_retries(
                record,
                1,
                root / "translated",
                root / "candidates",
                max_attempts,
                WeightedApiScheduler(1),
                WeightedApiScheduler(1),
                model_config,
                None,
            )
        return record, client

    def test_shrunk_page_is_redone_once_and_the_good_retry_is_kept(self) -> None:
        good = _png(_page((1440, 2048)))
        record, client = self._run([_png(_page((1440, 2048), scale=0.43)), good])
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.calls[1]["review_feedback"], CONTENT_SHRUNK_RETRY_FEEDBACK)
        self.assertEqual(record.status, "success")
        self.assertEqual(record.quality_flags, [])
        self.assertEqual(Path(record.translated_image_path).read_bytes(), good)

    def test_still_shrunk_after_the_redo_ships_with_the_switch_model_hint(self) -> None:
        shrunk = _png(_page((1440, 2048), scale=0.43))
        record, client = self._run([shrunk], max_attempts=3)
        # 只重做一次，不把剩下的重试次数都烧掉。
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(record.status, "success")
        self.assertFalse(record.placeholder)
        self.assertEqual(record.quality_flags, [PDF_QC_CONTENT_SHRUNK])
        self.assertIn("重做一次后仍然偏小。建议在设置中换一个图像模型", record.quality_message)
        # 不裁边、不放大：交的就是模型给的那张图。
        with Image.open(record.translated_image_path) as delivered:
            self.assertEqual(delivered.size, (1440, 2048))

    def test_failed_redo_falls_back_to_the_shrunk_page(self) -> None:
        shrunk = _png(_page((1440, 2048), scale=0.43))
        record, client = self._run([shrunk, RuntimeError("upstream 502")])
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(record.status, "success")
        self.assertFalse(record.placeholder)
        self.assertEqual(record.quality_flags, [PDF_QC_CONTENT_SHRUNK])
        self.assertIn("建议在设置中换一个图像模型", record.quality_message)

    def test_without_retry_budget_the_page_ships_with_the_hint(self) -> None:
        record, client = self._run([_png(_page((1440, 2048), scale=0.43))], max_attempts=1)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(record.status, "success")
        self.assertIn("。建议在设置中换一个图像模型", record.quality_message)
        self.assertNotIn("重做一次", record.quality_message)


if __name__ == "__main__":
    unittest.main()
