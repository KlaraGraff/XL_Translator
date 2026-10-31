"""任务中的保底：模型在本次任务里还没成功过，又连续两页报「不收图 / 生成不了图」。

* 审核模型：后面的页不再送审（译图照常交付，标「未审核」），结论存回设置，结果里多一条
  「整个任务」的待复核说明；
* 图像翻译模型：整页图像翻译时整个任务停下；其他翻译方式里，剩下要整页图像翻译的页
  保留原样。

所有模型调用都是假客户端，不会打到收费 API。
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from core.api_scheduler import WeightedApiScheduler
from core.image_generation import ImageModelUnavailableError
from core.image_input_check import IMAGE_INPUT_NO
from core.model_roles import ROLE_IMAGE, ROLE_PDF_REVIEW, SOURCE_INDEPENDENT, resolve_effective_model_config
from core.pdf_image_translation import (
    PDF_PAGE_STATUS_SKIPPED_OVERSIZE,
    ROUTE_IMAGE,
    SOURCE_TYPE_PDF,
    PdfFileItem,
    PdfImageTranslationRunner,
    PdfPageRecord,
    max_page_generation_attempts,
    write_pdf_manifest_and_report,
)
from core.pdf_review import PdfPageReviewResult
from settings import AppSettings
from tests.test_pdf_image_translation import (
    _FakeImageClient,
    _fake_pdfium_module,
    _png_bytes,
    _run_single_file,
)


def _refusal() -> Exception:
    request = httpx.Request("POST", "https://review.example/v1/chat/completions")
    response = httpx.Response(
        400, json={"error": {"message": "image input is not supported by this model"}}, request=request
    )
    try:
        raise RuntimeError("审核模型请求失败") from httpx.HTTPStatusError("400", request=request, response=response)
    except RuntimeError as exc:
        return exc


class _ReviewClient:
    """``passes`` 里列的调用（从 1 数）回「通过」，其余都报「不收图」。"""

    def __init__(self, passes: set[int] | None = None) -> None:
        self.passes = passes or set()
        self.calls = 0

    def review_page(self, **_kwargs):
        self.calls += 1
        if self.calls in self.passes:
            return PdfPageReviewResult(passed=True, blocking_issues=[], minor_suggestions=[], summary="ok", raw_text="{}")
        raise _refusal()


class _NoImageClient:
    """填了文字模型的图像翻译：每次都拿不到图。"""

    def __init__(self) -> None:
        self.calls = 0

    def generate_page(self, **_kwargs):
        self.calls += 1
        raise ValueError("图像模型没有返回可用图片")


def _settings(*, review: bool, route: str = "image") -> AppSettings:
    settings = AppSettings(target_lang="en")
    settings.pdf.target_lang = "en"
    settings.pdf.review_enabled = review
    settings.pdf.translation_route = route
    for role, model in (
        (settings.image_model_role, "image-model"),
        (settings.pdf_review_model_role, "review-model"),
    ):
        role.source_role = SOURCE_INDEPENDENT
        role.cloud_provider = "custom_openai"
        role.cloud_model = model
        role.cloud_base_url = "https://models.example/v1"
    return settings


class GuardRunTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source.pdf"
        self.source.write_bytes(b"%PDF-1.4\n")
        # 存回设置的两处换成记录：这里只看它们有没有被叫到、叫的是什么。
        self.saved_verdicts: list[tuple] = []
        self.saved_roles: list[tuple] = []
        for patcher in (
            patch.dict(sys.modules, {"pypdfium2": _fake_pdfium_module({"source.pdf": 3})}),
            patch("core.model_roles.get_key", return_value="secret"),
            patch(
                "core.pdf_image_translation.save_image_input_verdict",
                side_effect=lambda config, result, message, **kw: self.saved_verdicts.append((config, result, kw)),
            ),
            patch(
                "core.pdf_image_translation.save_role_availability",
                side_effect=lambda config, role, **kw: self.saved_roles.append((config, role, kw)),
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _runner(self, settings: AppSettings, *, image_client, review_client=None) -> PdfImageTranslationRunner:
        return PdfImageTranslationRunner(
            [PdfFileItem(path=self.source, name="source", size_kb=1.0, page_count=3)],
            settings,
            source_root=self.root,
            image_client=image_client,
            review_client=review_client,
            task_logger_enabled=False,
        )

    def _run(self, runner: PdfImageTranslationRunner, settings: AppSettings):
        return _run_single_file(
            runner,
            output_dir=self.root / "out",
            max_attempts=max_page_generation_attempts(3),
            model_config=resolve_effective_model_config(settings, ROLE_IMAGE),
            review_model_config=resolve_effective_model_config(settings, ROLE_PDF_REVIEW),
            total_pages=3,
        )

    def test_review_stops_after_two_pages_refuse_images(self) -> None:
        settings = _settings(review=True)
        review = _ReviewClient()
        runner = self._runner(settings, image_client=_FakeImageClient(_png_bytes(1200, 1600)), review_client=review)

        record = self._run(runner, settings)

        # 第 3 页不再送审；三页的译图都照常交付。
        self.assertEqual(review.calls, 2)
        self.assertEqual([page.status for page in record.pages], ["success"] * 3)
        self.assertEqual([page.review_status for page in record.pages[:2]], ["failed", "failed"])
        self.assertIn(record.pages[2].review_status, {"", "skipped"})
        self.assertIn("不能识别图片", record.pages[0].error)
        # 结论存回设置（不是只记在任务自己那份设置副本里）。
        self.assertEqual(len(self.saved_verdicts), 1)
        config, result, kwargs = self.saved_verdicts[0]
        self.assertEqual(result, IMAGE_INPUT_NO)
        self.assertEqual(config.model, "review-model")
        self.assertEqual(kwargs["role"], ROLE_PDF_REVIEW)
        # 结果里说清楚审核中途关掉了。
        issues = runner._task_issues([record])
        task_issue = [issue for issue in issues if issue.get("kind") == "review_no_image_input"]
        self.assertEqual(len(task_issue), 1)
        self.assertIn("之后的页面不再审核", task_issue[0]["problem"])
        # 报告里也写一句。
        summary = runner._build_summary(
            output_dir=self.root / "out", started=datetime.now(), file_records=[record], stopped=False, fatal_error="",
        )
        _manifest, report = write_pdf_manifest_and_report(summary, artifact_dir=self.root / "artifacts")
        self.assertIn("审核中途停止：图像翻译审核模型不能识别图片", report.read_text(encoding="utf-8"))

    def test_review_keeps_going_once_it_has_worked_in_this_task(self) -> None:
        settings = _settings(review=True)
        review = _ReviewClient(passes={1})
        runner = self._runner(settings, image_client=_FakeImageClient(_png_bytes(1200, 1600)), review_client=review)

        record = self._run(runner, settings)

        # 第 1 页审核成功过：后面两页的报错只算那两页的问题，照常送审。
        self.assertEqual(review.calls, 3)
        self.assertEqual(record.pages[0].review_status, "passed")
        self.assertEqual(self.saved_verdicts, [])
        self.assertFalse([issue for issue in runner._task_issues([record]) if issue.get("kind") == "review_no_image_input"])
        # 只有审核真的回了结果才记「调用成功」。
        self.assertEqual(runner._settings.pdf_review_model_role.availability_status, "available")

    def test_failed_reviews_alone_never_mark_the_model_as_working(self) -> None:
        settings = _settings(review=True)
        runner = self._runner(settings, image_client=_FakeImageClient(_png_bytes(1200, 1600)), review_client=_ReviewClient())

        self._run(runner, settings)

        self.assertNotEqual(runner._settings.pdf_review_model_role.availability_status, "available")

    def test_full_page_image_route_stops_when_the_image_model_makes_no_images(self) -> None:
        settings = _settings(review=False)
        image = _NoImageClient()
        runner = self._runner(settings, image_client=image)
        prepared = runner._prepare_pdf_files(output_dir=self.root / "out", app_managed=True)[0]

        with self.assertRaises(ImageModelUnavailableError) as raised:
            runner._process_prepared_pages(
                [prepared],
                max_attempts=max_page_generation_attempts(3),
                scheduler=WeightedApiScheduler(1),
                review_scheduler=WeightedApiScheduler(1),
                model_config=resolve_effective_model_config(settings, ROLE_IMAGE),
                review_model_config=None,
                concurrency=1,
                total_pages=3,
            )

        # 第 1 页试满 3 次，第 2 页第 1 次也失败：停下，不再试第 3 页。
        self.assertEqual(image.calls, max_page_generation_attempts(3) + 1)
        self.assertIn("连续两页均未能生成图片", str(raised.exception))
        self.assertEqual(len(self.saved_roles), 1)
        _config, role, kwargs = self.saved_roles[0]
        self.assertEqual(role, ROLE_IMAGE)
        self.assertFalse(kwargs["ok"])

    def test_other_routes_keep_the_remaining_full_page_image_pages_as_they_are(self) -> None:
        settings = _settings(review=False, route="auto")
        runner = self._runner(settings, image_client=_NoImageClient())

        runner._reject_image_model(resolve_effective_model_config(settings, ROLE_IMAGE))

        self.assertTrue(runner._image_model_rejected)
        self.assertTrue(runner._image_model_missing)
        self.assertEqual(runner._route_unusable_reason(ROUTE_IMAGE, str(self.source), 2), "图像翻译模型无法生成图片")
        page = PdfPageRecord(page_number=2, source_image_path=str(self.root / "page_002.png"), file_name="source")
        kept = runner._page_kept_after_image_model_rejected(page, SOURCE_TYPE_PDF)
        self.assertEqual(kept.status, PDF_PAGE_STATUS_SKIPPED_OVERSIZE)
        self.assertTrue(kept.skipped_oversize)
        self.assertEqual(kept.source_image_path, "")
        self.assertIn("已保留原样", kept.kept_reason)


if __name__ == "__main__":
    unittest.main()
