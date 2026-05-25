from __future__ import annotations

import io
import json
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from core import diagnostics
from core.api_scheduler import WeightedApiScheduler
from core.image_generation import ImageModelUnavailableError
from core.model_roles import (
    ROLE_IMAGE,
    ROLE_PDF_REVIEW,
    SOURCE_INDEPENDENT,
    resolve_effective_model_config,
)
from core.pdf_image_translation import (
    PDF_MANIFEST_FILENAME,
    PDF_OUTPUT_STATE_COMPLETED,
    PDF_OUTPUT_STATE_FAILED,
    PDF_OUTPUT_STATE_NEEDS_REVIEW,
    PDF_OUTPUT_STATE_STOPPED,
    PDF_REPORT_FILENAME,
    PdfFileItem,
    PdfFileRecord,
    PdfImageTranslationRunner,
    PdfPageRecord,
    PdfTaskSummary,
    check_page_quality,
    create_failure_placeholder_page,
    determine_pdf_task_status,
    page_image_name,
    resolve_pdf_page_archive_dirs,
    resolve_translated_pdf_variant_paths,
    resolve_translated_pdf_path,
    scan_pdf_path,
    write_pdf_manifest_and_report,
)
from core.pdf_review import PdfPageReviewResult, PdfReviewIssue
from core.task_runner import DoneMsg
from settings import AppSettings


def _png_bytes(width: int, height: int, color: str = "white") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


class PdfImageTranslationTests(unittest.TestCase):
    def test_scan_skips_generated_dirs_and_non_pdf_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.pdf").write_bytes(b"%PDF-1.4\n")
            (root / "note.txt").write_text("not copied", encoding="utf-8")
            (root / "_pdf_pages").mkdir()
            (root / "_pdf_pages" / "page.pdf").write_bytes(b"%PDF-1.4\n")
            generated = root / "root_翻译输出_20260525_120000"
            generated.mkdir()
            (generated / "old.pdf").write_bytes(b"%PDF-1.4\n")

            with patch.dict(
                scan_pdf_path.__globals__,
                {"_read_pdf_page_count": lambda _path: 1},
            ):
                items = scan_pdf_path(root)

        self.assertEqual([item.path.name for item in items], ["a.pdf"])

    def test_pdf_output_page_archives_live_at_package_root(self) -> None:
        output_dir = Path("/out/package")
        source_dir, translated_dir = resolve_pdf_page_archive_dirs(
            output_dir,
            Path("section/source.pdf"),
        )

        self.assertEqual(
            source_dir,
            output_dir / "_pdf_pages" / "source_pages" / "section" / "source",
        )
        self.assertEqual(
            translated_dir,
            output_dir / "_pdf_pages" / "translated_pages" / "section" / "source",
        )

    def test_fake_pdf_pipeline_mirrors_pdf_only_and_writes_page_archives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = root / "docs"
            source_dir.mkdir()
            source_pdf = source_dir / "source.pdf"
            source_pdf.write_bytes(b"%PDF-1.4\n")
            (source_dir / "note.txt").write_text("not copied", encoding="utf-8")
            output_dir = root / "out"
            settings = AppSettings(target_lang="en")
            settings.image_model_role.source_role = SOURCE_INDEPENDENT
            settings.image_model_role.cloud_provider = "custom_openai"
            settings.image_model_role.cloud_model = "image-model"
            settings.image_model_role.cloud_base_url = "https://images.example/v1"
            runner = PdfImageTranslationRunner(
                [
                    PdfFileItem(
                        path=source_pdf,
                        name="source",
                        size_kb=1.0,
                        page_count=1,
                    )
                ],
                settings,
                source_root=root,
                image_client=_FakeImageClient(_png_bytes(1200, 1600)),
                task_logger_enabled=False,
            )

            with patch.dict(sys.modules, {"fitz": _fake_fitz_module()}), patch(
                "core.model_roles.get_key",
                return_value="secret",
            ):
                record = runner._process_file(
                    runner._files[0],
                    output_dir=output_dir,
                    app_managed=True,
                    retry_attempts=3,
                    scheduler=WeightedApiScheduler(1),
                    model_config=resolve_effective_model_config(settings, ROLE_IMAGE),
                    review_model_config=None,
                    concurrency=1,
                    processed_page_offset=0,
                    total_pages=1,
                )

            self.assertEqual(record.status, PDF_OUTPUT_STATE_COMPLETED)
            self.assertTrue((output_dir / "docs" / "source.pdf").exists())
            self.assertFalse((output_dir / "docs" / "note.txt").exists())
            self.assertTrue((output_dir / "docs" / "译文(英文)_source_高清.pdf").exists())
            self.assertTrue((output_dir / "docs" / "译文(英文)_source_压缩.pdf").exists())
            self.assertTrue(record.compressed_pdf_path.endswith("译文(英文)_source_压缩.pdf"))
            self.assertTrue(
                (
                    output_dir
                    / "_pdf_pages"
                    / "source_pages"
                    / "docs"
                    / "source"
                    / "page_001.png"
                ).exists()
            )
            self.assertTrue(
                (
                    output_dir
                    / "_pdf_pages"
                    / "translated_pages"
                    / "docs"
                    / "source"
                    / "page_001.png"
                ).exists()
            )

    def test_page_image_naming_is_one_based_and_zero_padded(self) -> None:
        self.assertEqual(page_image_name(1, 3), "page_001.png")
        self.assertEqual(page_image_name(12, 120), "page_012.png")
        self.assertEqual(page_image_name(2, 3, failed=True), "page_002_failed.png")

    def test_translated_pdf_revision_renames_unsuffixed_app_managed_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target_dir = Path(tmp)
            settings = AppSettings(target_lang="en")
            base = target_dir / "译文(英文)_source.pdf"
            base.write_text("old", encoding="utf-8")

            next_path = resolve_translated_pdf_path(
                target_dir,
                "source.pdf",
                "en",
                settings,
                app_managed=True,
            )

            self.assertFalse(base.exists())
            self.assertTrue((target_dir / "译文(英文)_source_R1.pdf").exists())
            self.assertEqual(next_path.name, "译文(英文)_source_R2.pdf")

            next_path.write_text("new", encoding="utf-8")
            r3 = resolve_translated_pdf_path(
                target_dir,
                "source.pdf",
                "en",
                settings,
                app_managed=True,
            )
            self.assertEqual(r3.name, "译文(英文)_source_R3.pdf")

    def test_custom_output_revision_does_not_rename_unsuffixed_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target_dir = Path(tmp)
            settings = AppSettings(target_lang="en")
            base = target_dir / "译文(英文)_source.pdf"
            base.write_text("old", encoding="utf-8")

            next_path = resolve_translated_pdf_path(
                target_dir,
                "source.pdf",
                "en",
                settings,
                app_managed=False,
            )

            self.assertTrue(base.exists())
            self.assertEqual(next_path.name, "译文(英文)_source_R1.pdf")

    def test_translated_pdf_variant_paths_use_matched_revision_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target_dir = Path(tmp)
            settings = AppSettings(target_lang="en")
            (target_dir / "译文(英文)_source_高清.pdf").write_text("old", encoding="utf-8")

            high, compressed = resolve_translated_pdf_variant_paths(
                target_dir,
                "source.pdf",
                "en",
                settings,
                app_managed=True,
            )

            self.assertTrue((target_dir / "译文(英文)_source_高清_R1.pdf").exists())
            self.assertEqual(high.name, "译文(英文)_source_高清_R2.pdf")
            self.assertEqual(compressed.name, "译文(英文)_source_压缩_R2.pdf")

    def test_page_quality_order_decode_ratio_then_resolution(self) -> None:
        decode = check_page_quality(b"not an image", source_width=1600, source_height=1200)
        self.assertEqual(decode.status, "decode_error")

        ratio = check_page_quality(
            _png_bytes(1600, 1000),
            source_width=1600,
            source_height=1200,
        )
        self.assertEqual(ratio.status, "ratio_error")

        low_resolution = check_page_quality(
            _png_bytes(1199, 1600),
            source_width=1200,
            source_height=1601,
        )
        self.assertEqual(low_resolution.status, "low_resolution")

        ok = check_page_quality(
            _png_bytes(1608, 1200),
            source_width=1600,
            source_height=1200,
        )
        self.assertTrue(ok.ok)

    def test_failure_placeholder_page_contains_expected_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            placeholder = Path(tmp) / "page_001_failed.png"
            result = create_failure_placeholder_page(
                page_number=1,
                failure_ordinal="1/3",
                error_summary="low resolution",
                source_image_path=Path(tmp) / "source.png",
                placeholder_path=placeholder,
                width=1200,
                height=1600,
            )

            self.assertEqual(result, placeholder)
            self.assertTrue(placeholder.exists())
            with Image.open(placeholder) as image:
                self.assertEqual(image.size, (1200, 1600))
                self.assertEqual(image.getpixel((20, 20)), (178, 34, 34))

    def test_manifest_report_and_status_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            review_file = PdfFileRecord(
                name="source.pdf",
                source_path="/src/source.pdf",
                relative_path="source.pdf",
                translated_pdf_path="/out/译文(英文)_source.pdf",
                status=PDF_OUTPUT_STATE_NEEDS_REVIEW,
                page_count=2,
                generated_page_count=2,
                placeholder_page_count=1,
                emergency_ratio_normalized_count=1,
                retry_count=3,
                pages=[
                    PdfPageRecord(
                        page_number=1,
                        source_image_path="/out/_pdf_pages/source_pages/source/page_001.png",
                        translated_image_path="/out/_pdf_pages/translated_pages/source/page_001_failed.png",
                        status="placeholder",
                        error="low resolution",
                        placeholder=True,
                        failure_ordinal="1/1",
                    )
                ],
            )
            summary = PdfTaskSummary(
                status=PDF_OUTPUT_STATE_NEEDS_REVIEW,
                output_dir=str(output_dir),
                target_lang="en",
                target_lang_label="英文",
                started_at="2026-05-25T10:00:00",
                completed_at="2026-05-25T10:01:00",
                elapsed_sec=60,
                file_count=1,
                total_page_count=2,
                generated_pdf_count=1,
                placeholder_page_count=1,
                emergency_ratio_normalized_count=1,
                retry_count=3,
                rate_limit_reduction_count=1,
                partial_artifacts_available=True,
                files=[review_file],
            )

            manifest_path, report_path = write_pdf_manifest_and_report(summary)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            report = report_path.read_text(encoding="utf-8")

            self.assertEqual(manifest_path.name, PDF_MANIFEST_FILENAME)
            self.assertEqual(report_path.name, PDF_REPORT_FILENAME)
            self.assertEqual(manifest["status"], PDF_OUTPUT_STATE_NEEDS_REVIEW)
            self.assertTrue(manifest["partial_artifacts_available"])
            self.assertIn("completed", {PDF_OUTPUT_STATE_COMPLETED})
            self.assertIn("needs_review", {PDF_OUTPUT_STATE_NEEDS_REVIEW})
            self.assertIn("stopped", {PDF_OUTPUT_STATE_STOPPED})
            self.assertIn("failed", {PDF_OUTPUT_STATE_FAILED})
            self.assertIn("失败占位页：1", report)

    def test_task_status_completed_needs_review_stopped_failed(self) -> None:
        completed = PdfFileRecord(
            name="a.pdf",
            source_path="/a.pdf",
            relative_path="a.pdf",
            translated_pdf_path="/out/a.pdf",
            status=PDF_OUTPUT_STATE_COMPLETED,
        )
        review = PdfFileRecord(
            name="b.pdf",
            source_path="/b.pdf",
            relative_path="b.pdf",
            translated_pdf_path="/out/b.pdf",
            status=PDF_OUTPUT_STATE_NEEDS_REVIEW,
            placeholder_page_count=1,
        )
        failed = PdfFileRecord(
            name="c.pdf",
            source_path="/c.pdf",
            relative_path="c.pdf",
            status=PDF_OUTPUT_STATE_FAILED,
        )

        self.assertEqual(
            determine_pdf_task_status(stopped=False, file_records=[completed]),
            PDF_OUTPUT_STATE_COMPLETED,
        )
        self.assertEqual(
            determine_pdf_task_status(stopped=False, file_records=[review]),
            PDF_OUTPUT_STATE_NEEDS_REVIEW,
        )
        self.assertEqual(
            determine_pdf_task_status(stopped=True, file_records=[completed]),
            PDF_OUTPUT_STATE_STOPPED,
        )
        self.assertEqual(
            determine_pdf_task_status(stopped=False, file_records=[failed]),
            PDF_OUTPUT_STATE_FAILED,
        )

    def test_pdf_diagnostics_archive_is_lightweight_and_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records = root / "diagnostics" / "records"
            source_pdf = root / "source.pdf"
            output_pdf = root / "译文(英文)_source.pdf"
            page_png = root / "_pdf_pages" / "translated_pages" / "source" / "page_001.png"
            source_pdf.write_bytes(b"source pdf bytes")
            output_pdf.write_bytes(b"translated pdf bytes")
            page_png.parent.mkdir(parents=True)
            page_png.write_bytes(_png_bytes(1200, 1600))
            settings = AppSettings()
            done = DoneMsg(
                output_dir=str(root),
                file_results=[
                    {
                        "name": source_pdf.name,
                        "success": True,
                        "output": str(output_pdf),
                    }
                ],
                elapsed_sec=1,
                tm_hit_count=0,
                api_call_count=1,
                issues=[
                    {
                        "file": source_pdf.name,
                        "source_image_path": str(page_png),
                        "problem": "api_key=secret-token",
                    }
                ],
            )

            with patch.object(diagnostics, "DIAGNOSTIC_RECORDS_DIR", records):
                record_dir = diagnostics.archive_task_diagnostics(
                    surface="pdf",
                    phase="done",
                    task_id="pdf-test",
                    settings=settings,
                    selected_files=[
                        type("Item", (), {"path": source_pdf, "name": "source", "size_kb": 1.0})()
                    ],
                    logs=[{"level": "INFO", "message": "Authorization: Bearer secret-token"}],
                    done=done,
                    source_root=root,
                    status="done",
                )
                data, _ = diagnostics.build_diagnostic_zip_bytes(record_dir)

            with zipfile.ZipFile(io.BytesIO(data), "r") as archive:
                names = archive.namelist()
                payload = b"".join(archive.read(name) for name in names)

            self.assertFalse(any(name.endswith((".pdf", ".png")) for name in names))
            self.assertNotIn(b"secret-token", payload)
            self.assertTrue(any(name.endswith("task/pdf_summary.json") for name in names))

    def test_model_unavailable_keeps_current_file_artifacts_in_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_pdf = root / "source.pdf"
            source_pdf.write_bytes(b"%PDF-1.4\n")
            output_dir = root / "out"
            settings = AppSettings(target_lang="en")
            settings.image_model_role.source_role = SOURCE_INDEPENDENT
            settings.image_model_role.cloud_provider = "custom_openai"
            settings.image_model_role.cloud_model = "image-model"
            settings.image_model_role.cloud_base_url = "https://images.example/v1"
            runner = PdfImageTranslationRunner(
                [PdfFileItem(path=source_pdf, name="source", size_kb=1.0, page_count=1)],
                settings,
                source_root=root,
                image_client=_UnavailableImageClient(),
                task_logger_enabled=False,
            )

            with patch.dict(sys.modules, {"fitz": _fake_fitz_module()}), patch(
                "core.model_roles.get_key",
                return_value="secret",
            ):
                record = runner._process_file(
                    runner._files[0],
                    output_dir=output_dir,
                    app_managed=True,
                    retry_attempts=3,
                    scheduler=WeightedApiScheduler(1),
                    model_config=resolve_effective_model_config(settings, ROLE_IMAGE),
                    review_model_config=None,
                    concurrency=1,
                    processed_page_offset=0,
                    total_pages=1,
                )

            self.assertEqual(record.status, PDF_OUTPUT_STATE_FAILED)
            self.assertTrue(record.source_copy_path)
            self.assertEqual(len(record.pages), 1)
            self.assertTrue(record.pages[0].source_image_path.endswith("page_001.png"))
            self.assertIn("invalid api key", record.error)

    def test_review_failure_regenerates_from_source_and_keeps_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_pdf = root / "source.pdf"
            source_pdf.write_bytes(b"%PDF-1.4\n")
            output_dir = root / "out"
            settings = AppSettings(target_lang="en")
            settings.pdf.review_enabled = True
            settings.image_model_role.source_role = SOURCE_INDEPENDENT
            settings.image_model_role.cloud_provider = "custom_openai"
            settings.image_model_role.cloud_model = "image-model"
            settings.image_model_role.cloud_base_url = "https://images.example/v1"
            settings.pdf_review_model_role.source_role = SOURCE_INDEPENDENT
            settings.pdf_review_model_role.cloud_provider = "custom_openai"
            settings.pdf_review_model_role.cloud_model = "vision-review-model"
            settings.pdf_review_model_role.cloud_base_url = "https://images.example/v1"
            review_client = _FailThenPassReviewClient()
            image_client = _RecordingImageClient(_png_bytes(1200, 1600))
            runner = PdfImageTranslationRunner(
                [PdfFileItem(path=source_pdf, name="source", size_kb=1.0, page_count=1)],
                settings,
                source_root=root,
                image_client=image_client,
                review_client=review_client,
                task_logger_enabled=False,
            )

            with patch.dict(sys.modules, {"fitz": _fake_fitz_module()}), patch(
                "core.model_roles.get_key",
                return_value="secret",
            ):
                record = runner._process_file(
                    runner._files[0],
                    output_dir=output_dir,
                    app_managed=True,
                    retry_attempts=3,
                    scheduler=WeightedApiScheduler(1),
                    model_config=resolve_effective_model_config(settings, ROLE_IMAGE),
                    review_model_config=resolve_effective_model_config(
                        settings,
                        ROLE_PDF_REVIEW,
                    ),
                    concurrency=1,
                    processed_page_offset=0,
                    total_pages=1,
                )

            page = record.pages[0]
            self.assertEqual(record.status, PDF_OUTPUT_STATE_COMPLETED)
            self.assertEqual(review_client.calls, 2)
            self.assertEqual(record.review_passed_page_count, 1)
            self.assertEqual(record.review_repaired_page_count, 1)
            self.assertEqual(record.review_retry_count, 1)
            self.assertEqual(page.review_status, "passed")
            self.assertEqual(page.final_candidate_attempt, 2)
            candidate_dir = output_dir / "_pdf_pages" / "review_candidates" / "source" / "page_001"
            self.assertTrue((candidate_dir / "attempt_01.png").exists())
            self.assertTrue((candidate_dir / "attempt_01_review.json").exists())
            self.assertTrue((candidate_dir / "attempt_02.png").exists())
            self.assertIn("字体略细", page.review_minor_suggestions)
            self.assertEqual(len(image_client.calls), 2)
            self.assertEqual(
                image_client.calls[0]["source_image_path"],
                image_client.calls[1]["source_image_path"],
            )
            self.assertIn("编号标签误译", image_client.calls[1]["review_feedback"])

    def test_review_exhaustion_uses_placeholder_and_marks_needs_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_pdf = root / "source.pdf"
            source_pdf.write_bytes(b"%PDF-1.4\n")
            output_dir = root / "out"
            settings = AppSettings(target_lang="en")
            settings.pdf.review_enabled = True
            settings.image_model_role.source_role = SOURCE_INDEPENDENT
            settings.image_model_role.cloud_provider = "custom_openai"
            settings.image_model_role.cloud_model = "image-model"
            settings.image_model_role.cloud_base_url = "https://images.example/v1"
            settings.pdf_review_model_role.source_role = SOURCE_INDEPENDENT
            settings.pdf_review_model_role.cloud_provider = "custom_openai"
            settings.pdf_review_model_role.cloud_model = "vision-review-model"
            settings.pdf_review_model_role.cloud_base_url = "https://images.example/v1"
            runner = PdfImageTranslationRunner(
                [PdfFileItem(path=source_pdf, name="source", size_kb=1.0, page_count=1)],
                settings,
                source_root=root,
                image_client=_FakeImageClient(_png_bytes(1200, 1600)),
                review_client=_AlwaysFailReviewClient(),
                task_logger_enabled=False,
            )

            with patch.dict(sys.modules, {"fitz": _fake_fitz_module()}), patch(
                "core.model_roles.get_key",
                return_value="secret",
            ):
                record = runner._process_file(
                    runner._files[0],
                    output_dir=output_dir,
                    app_managed=True,
                    retry_attempts=3,
                    scheduler=WeightedApiScheduler(1),
                    model_config=resolve_effective_model_config(settings, ROLE_IMAGE),
                    review_model_config=resolve_effective_model_config(
                        settings,
                        ROLE_PDF_REVIEW,
                    ),
                    concurrency=1,
                    processed_page_offset=0,
                    total_pages=1,
                )

            page = record.pages[0]
            self.assertEqual(record.status, PDF_OUTPUT_STATE_NEEDS_REVIEW)
            self.assertEqual(record.placeholder_page_count, 1)
            self.assertEqual(record.review_failed_page_count, 1)
            self.assertEqual(page.review_status, "failed")
            self.assertTrue(page.placeholder)
            self.assertIn("审核未通过", page.error)
            candidate_dir = output_dir / "_pdf_pages" / "review_candidates" / "source" / "page_001"
            self.assertTrue((candidate_dir / "attempt_03.png").exists())


class _FailThenPassReviewClient:
    def __init__(self) -> None:
        self.calls = 0

    def review_page(self, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            return PdfPageReviewResult(
                passed=False,
                blocking_issues=[
                    PdfReviewIssue(
                        type="wrong_translation",
                        location="表格右上角",
                        problem="编号标签误译",
                        suggestion="改为报告号",
                    )
                ],
                summary="审核未通过：编号标签误译",
            )
        return PdfPageReviewResult(
            passed=True,
            minor_suggestions=["字体略细"],
            summary="可采用",
        )


class _AlwaysFailReviewClient:
    def review_page(self, **_kwargs):
        return PdfPageReviewResult(
            passed=False,
            blocking_issues=[
                PdfReviewIssue(
                    type="missing_translation",
                    location="页眉",
                    problem="仍有漏译",
                    suggestion="重新生成完整页面",
                )
            ],
            summary="审核未通过：仍有漏译",
        )


class _FakeImageClient:
    def __init__(self, image_bytes: bytes):
        self.image_bytes = image_bytes

    def generate_page(self, **_kwargs):
        return self.image_bytes


class _RecordingImageClient(_FakeImageClient):
    def __init__(self, image_bytes: bytes):
        super().__init__(image_bytes)
        self.calls: list[dict[str, str]] = []

    def generate_page(self, **kwargs):
        self.calls.append(
            {
                "source_image_path": str(kwargs.get("source_image_path") or ""),
                "review_feedback": str(kwargs.get("review_feedback") or ""),
            }
        )
        return super().generate_page(**kwargs)


class _UnavailableImageClient:
    def generate_page(self, **_kwargs):
        raise ImageModelUnavailableError("invalid api key")


def _fake_fitz_module():
    class FakeRect:
        def __init__(self, x0=0, y0=0, x1=288, y1=384):
            self.x0 = x0
            self.y0 = y0
            self.x1 = x1
            self.y1 = y1
            self.width = x1 - x0
            self.height = y1 - y0

    class FakePix:
        width = 1200
        height = 1600

        def save(self, path: str) -> None:
            Path(path).write_bytes(_png_bytes(self.width, self.height))

    class FakePage:
        rect = FakeRect()

        def get_pixmap(self, matrix=None, alpha=False):  # noqa: ARG002
            return FakePix()

        def insert_image(self, rect, filename: str) -> None:  # noqa: ARG002
            return

    class FakeInputDoc:
        page_count = 1
        needs_pass = False

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
            return False

        def load_page(self, page_index: int):  # noqa: ARG002
            return FakePage()

    class FakeOutputDoc:
        def new_page(self, *, width: float, height: float):  # noqa: ARG002
            return FakePage()

        def save(self, path: str) -> None:
            Path(path).write_bytes(b"%PDF-1.4\n% fake translated pdf\n")

        def close(self) -> None:
            return

    def fake_open(path: str | None = None):
        return FakeInputDoc() if path else FakeOutputDoc()

    return types.SimpleNamespace(
        open=fake_open,
        Matrix=lambda x, y: (x, y),
        Rect=FakeRect,
    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
