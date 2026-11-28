"""开始前检查「PDF 处理组件」（反馈 #33）。

智能混合里图纸首选原位替换、文字页首选转 Word，都要用组件。没装时：
* A3 图纸不改用图像翻译，会保留原样 → 开始前拦下，说「会保留原样」；
* A4 文字页能改用图像翻译 → 也拦下，说「改用整页图像翻译」；
* 扫描页、整页图像翻译方式、装了组件 → 不拦；
* 「仍然开始」（allow_missing_pdf_component）→ 不拦。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import api.surfaces.pdf as pdf_surface
from api.task_contracts import REASON_PDF_COMPONENT_MISSING, TaskInputError, TaskOptions
from core.pdf_component_needs import pdf_component_need
from core.pdf_image_translation import PdfFileItem
from settings import AppSettings

# 测试 PDF 用 PyMuPDF 画；被测的判断本身只用随安装包分发的 pypdfium2。
fitz = pytest.importorskip("fitz")

from tests.test_pdf_routes import A3, _drawing_page, _scan_page, _text_page  # noqa: E402


def _pdf(path: Path, builders) -> PdfFileItem:
    doc = fitz.open()
    for build in builders:
        build(doc)
    doc.save(path)
    doc.close()
    return PdfFileItem(path, path.stem, 1.0, page_count=len(builders), relative_path=path.name)


def _settings(route: str = "auto") -> AppSettings:
    settings = AppSettings(target_lang="zh")
    settings.pdf.target_lang = "zh"
    settings.pdf.translation_route = route
    settings.pdf.review_enabled = False
    return settings


class ComponentNeedTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_a3_drawing_is_kept_without_the_component(self) -> None:
        item = _pdf(self.root / "plan.pdf", [lambda d: _drawing_page(d, A3)])
        need = pdf_component_need([item], _settings().pdf, image_available=True)
        self.assertEqual((need.pages, need.kept_pages), (1, 1))
        self.assertEqual(need.kept_files, ["plan.pdf"])

    def test_a4_pages_fall_back_to_image_when_an_image_model_is_set(self) -> None:
        item = _pdf(self.root / "doc.pdf", [_text_page, _drawing_page, _scan_page])
        need = pdf_component_need([item], _settings().pdf, image_available=True)
        # 文字页（转 Word）和 A4 图纸（原位替换）要组件，扫描页不要；都能改用图像翻译。
        self.assertEqual((need.pages, need.kept_pages), (2, 0))
        without_image = pdf_component_need([item], _settings().pdf, image_available=False)
        self.assertEqual((without_image.pages, without_image.kept_pages), (2, 2))

    def test_scans_images_and_the_image_route_need_nothing(self) -> None:
        scan = _pdf(self.root / "scan.pdf", [_scan_page, _scan_page])
        self.assertFalse(pdf_component_need([scan], _settings().pdf, image_available=True).needed)
        drawing = _pdf(self.root / "plan.pdf", [lambda d: _drawing_page(d, A3)])
        self.assertFalse(pdf_component_need([drawing], _settings("image").pdf, image_available=True).needed)
        picture = PdfFileItem(self.root / "photo.png", "photo", 1.0, source_type="image")
        self.assertFalse(pdf_component_need([picture], _settings().pdf, image_available=True).needed)

    def test_unsupported_target_language_is_not_blamed_on_the_component(self) -> None:
        item = _pdf(self.root / "plan.pdf", [lambda d: _drawing_page(d, A3)])
        settings = _settings()
        settings.pdf.target_lang = "ar"
        self.assertFalse(pdf_component_need([item], settings.pdf, image_available=True).needed)

    def test_unreadable_file_is_left_to_the_task(self) -> None:
        broken = self.root / "broken.pdf"
        broken.write_bytes(b"%PDF-1.4\nnot really")
        item = PdfFileItem(broken, "broken", 1.0)
        self.assertFalse(pdf_component_need([item], _settings().pdf, image_available=True).needed)


class PreflightTests(unittest.TestCase):
    """PdfSurface.validate_preflight：模型那几步换成「都配好了」，只看组件这一关。"""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.component = False
        self.image_ok = True
        model = SimpleNamespace(model="text-model", provider="custom_openai", mode="cloud", api_key="k")

        def image_check(_settings) -> None:
            if not self.image_ok:
                raise TaskInputError("请先填写图像翻译模型的名称。", reason="image_model_missing")

        for patcher in (
            patch("core.pdf_component.is_available", side_effect=lambda **_kw: self.component),
            patch.object(pdf_surface, "resolve_effective_model_config", return_value=model),
            patch.object(pdf_surface, "_validate_image_model", side_effect=image_check),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _preflight(self, items, settings=None, **options) -> None:
        pdf_surface.PdfSurface().validate_preflight(
            files=items, settings=settings or _settings(), options=TaskOptions(**options)
        )

    def test_a3_drawing_without_component_is_stopped_with_a_way_out(self) -> None:
        item = _pdf(self.root / "plan.pdf", [lambda d: _drawing_page(d, A3)])
        with self.assertRaises(TaskInputError) as caught:
            self._preflight([item])
        self.assertEqual(caught.exception.reason, REASON_PDF_COMPONENT_MISSING)
        self.assertEqual(caught.exception.accept, {"allow_missing_pdf_component": True})
        self.assertEqual(
            str(caught.exception),
            "所选文件中有 1 页要用「PDF 处理组件」，本机还没有安装。这些页没有别的方式可以翻译，会保留原样。",
        )

    def test_message_says_which_pages_fall_back(self) -> None:
        item = _pdf(self.root / "mix.pdf", [_text_page, lambda d: _drawing_page(d, A3)])
        with self.assertRaises(TaskInputError) as caught:
            self._preflight([item])
        self.assertIn("其中 1 页会保留原样，其余改用整页图像翻译。", str(caught.exception))
        a4 = _pdf(self.root / "a4.pdf", [_text_page])
        with self.assertRaises(TaskInputError) as caught:
            self._preflight([a4])
        self.assertIn("这些页会改用整页图像翻译", str(caught.exception))

    def test_continue_anyway_installed_or_image_route_passes(self) -> None:
        item = _pdf(self.root / "plan.pdf", [lambda d: _drawing_page(d, A3)])
        self._preflight([item], allow_missing_pdf_component=True)
        self._preflight([item], settings=_settings("image"))
        self.component = True
        self._preflight([item])

    def test_without_image_model_text_pages_are_kept_too(self) -> None:
        self.image_ok = False
        item = _pdf(self.root / "a4.pdf", [_text_page])
        with self.assertRaises(TaskInputError) as caught:
            self._preflight([item])
        self.assertIn("会保留原样", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
