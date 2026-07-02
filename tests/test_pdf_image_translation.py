from __future__ import annotations

import io
import json
import sys
import tempfile
import threading
import time
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
    SOURCE_TYPE_IMAGE,
    SOURCE_TYPE_PDF,
    PdfFileItem,
    PdfFileRecord,
    PdfImageTranslationRunner,
    PdfPageRecord,
    PdfTaskSummary,
    check_page_quality,
    create_failure_placeholder_page,
    determine_pdf_task_status,
    _file_record_to_result,
    _localized_pdf_placeholder_problem,
    max_page_generation_attempts,
    page_image_name,
    resolve_pdf_page_archive_dirs,
    resolve_translated_pdf_variant_paths,
    resolve_translated_pdf_path,
    scan_pdf_path,
    translated_image_base_name,
    translated_pdf_base_name,
    write_pdf_manifest_and_report,
)
from core.pdf_review import PdfPageReviewResult, PdfReviewIssue
from core.task_runner import DoneMsg, LogMsg, StoppedMsg
from settings import AppSettings


def _png_bytes(width: int, height: int, color: str = "white") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _jpeg_bytes(width: int, height: int, color: str = "white") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="JPEG")
    return buffer.getvalue()


class PdfImageTranslationTests(unittest.TestCase):
    def test_scan_skips_generated_dirs_and_non_pdf_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.pdf").write_bytes(b"%PDF-1.4\n")
            (root / "photo.png").write_bytes(_png_bytes(1200, 1600))
            (root / "note.txt").write_text("not copied", encoding="utf-8")
            (root / "_pdf_pages").mkdir()
            (root / "_pdf_pages" / "page.pdf").write_bytes(b"%PDF-1.4\n")
            (root / "_pdf_pages" / "page.png").write_bytes(_png_bytes(1200, 1600))
            generated = root / "root_翻译输出_20260525_120000"
            generated.mkdir()
            (generated / "old.pdf").write_bytes(b"%PDF-1.4\n")
            (generated / "old.png").write_bytes(_png_bytes(1200, 1600))

            with patch.dict(
                scan_pdf_path.__globals__,
                {"_read_pdf_page_count": lambda _path: 1},
            ):
                items = scan_pdf_path(root)
                mixed_items = scan_pdf_path(root, include_images=True)

        self.assertEqual([item.path.name for item in items], ["a.pdf"])
        self.assertEqual([item.path.name for item in mixed_items], ["a.pdf", "photo.png"])
        self.assertEqual([item.source_type for item in mixed_items], [SOURCE_TYPE_PDF, SOURCE_TYPE_IMAGE])

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
                    max_attempts=max_page_generation_attempts(3),
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

    def test_image_pipeline_outputs_model_image_format_without_pdf_variants(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = root / "images"
            source_dir.mkdir()
            source_image = source_dir / "diagram.png"
            source_image.write_bytes(_png_bytes(1200, 1600))
            output_dir = root / "out"
            settings = AppSettings(target_lang="en")
            settings.image_model_role.source_role = SOURCE_INDEPENDENT
            settings.image_model_role.cloud_provider = "custom_openai"
            settings.image_model_role.cloud_model = "image-model"
            settings.image_model_role.cloud_base_url = "https://images.example/v1"
            runner = PdfImageTranslationRunner(
                [
                    PdfFileItem(
                        path=source_image,
                        name="diagram",
                        size_kb=1.0,
                        page_count=1,
                        source_type=SOURCE_TYPE_IMAGE,
                    )
                ],
                settings,
                source_root=root,
                image_client=_FakeImageClient(_jpeg_bytes(1200, 1600)),
                task_logger_enabled=False,
            )

            with patch("core.model_roles.get_key", return_value="secret"):
                prepared = runner._prepare_pdf_files(output_dir=output_dir, app_managed=True)[0]
                runner._total_page_count = 1
                runner._process_prepared_pages(
                    [prepared],
                    max_attempts=max_page_generation_attempts(0),
                    scheduler=WeightedApiScheduler(1),
                    review_scheduler=WeightedApiScheduler(1),
                    model_config=resolve_effective_model_config(settings, ROLE_IMAGE),
                    review_model_config=None,
                    concurrency=1,
                    total_pages=1,
                )
                runner._finalize_file_record(prepared, should_assemble=True)

            record = prepared.record
            self.assertEqual(record.status, PDF_OUTPUT_STATE_COMPLETED)
            self.assertEqual(record.source_type, SOURCE_TYPE_IMAGE)
            self.assertFalse(record.translated_pdf_path)
            self.assertFalse(record.compressed_pdf_path)
            self.assertTrue(record.translated_image_path.endswith("译文(英文)_diagram.jpg"))
            self.assertEqual(record.translated_image_format, "JPEG")
            self.assertTrue((output_dir / "images" / "diagram.png").exists())
            self.assertTrue(
                (
                    output_dir
                    / "_pdf_pages"
                    / "source_pages"
                    / "images"
                    / "diagram"
                    / "page_001.png"
                ).exists()
            )
            translated_page = (
                output_dir
                / "_pdf_pages"
                / "translated_pages"
                / "images"
                / "diagram"
                / "page_001.jpg"
            )
            self.assertTrue(translated_page.exists())
            with Image.open(record.translated_image_path) as image:
                self.assertEqual(image.format, "JPEG")

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

    def test_translated_artifact_names_sanitize_windows_invalid_fragments(self) -> None:
        settings = AppSettings(target_lang="en")

        self.assertEqual(
            translated_pdf_base_name('site:plan?"A".pdf', "en", settings),
            "译文(英文)_site_plan_A_.pdf",
        )
        self.assertEqual(
            translated_image_base_name(
                'diagram:phase*1.png',
                "en",
                settings,
                output_suffix=".jpg",
            ),
            "译文(英文)_diagram_phase_1.jpg",
        )

    def test_revision_lookup_handles_glob_special_characters_in_source_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target_dir = Path(tmp)
            settings = AppSettings(target_lang="en")
            (target_dir / "译文(英文)_[source]_R1.pdf").write_text("old", encoding="utf-8")

            next_path = resolve_translated_pdf_path(
                target_dir,
                "[source].pdf",
                "en",
                settings,
                app_managed=True,
            )

            self.assertEqual(next_path.name, "译文(英文)_[source]_R2.pdf")

    def test_page_quality_checks_decode_and_ratio_only(self) -> None:
        decode = check_page_quality(b"not an image", source_width=1600, source_height=1200)
        self.assertEqual(decode.status, "decode_error")

        ratio = check_page_quality(
            _png_bytes(1600, 1000),
            source_width=1600,
            source_height=1200,
        )
        self.assertEqual(ratio.status, "ratio_error")

        low_pixel_but_same_ratio = check_page_quality(
            _png_bytes(1055, 1491),
            source_width=2479,
            source_height=3508,
        )
        self.assertTrue(low_pixel_but_same_ratio.ok)

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

    def test_failure_placeholder_problem_localizes_common_api_errors(self) -> None:
        problem = _localized_pdf_placeholder_problem(
            "Server error '502 Bad Gateway' for url 'https://api.example/v1/images/edits/' "
            "For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/502"
        )

        self.assertIn("图像翻译接口返回服务器错误", problem)
        self.assertIn("502 网关错误", problem)
        self.assertIn("https://api.example/v1/images/edits/", problem)

    def test_success_pdf_result_does_not_show_size_detail_as_error_reason(self) -> None:
        result = _file_record_to_result(
            PdfFileRecord(
                name="source.pdf",
                source_path="/src/source.pdf",
                relative_path="source.pdf",
                translated_pdf_path="/out/译文(中文)_source_高清.pdf",
                compressed_pdf_path="/out/译文(中文)_source_压缩.pdf",
                status=PDF_OUTPUT_STATE_COMPLETED,
                high_quality_pdf_size_bytes=10 * 1024 * 1024,
                compressed_pdf_size_bytes=2 * 1024 * 1024,
            )
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["detail"], "")
        self.assertNotIn("节省", result["detail"])

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
            self.assertEqual(manifest["terminal_reason"], "completed_needs_review")
            self.assertTrue(manifest["partial_artifacts_available"])
            self.assertIn("completed", {PDF_OUTPUT_STATE_COMPLETED})
            self.assertIn("needs_review", {PDF_OUTPUT_STATE_NEEDS_REVIEW})
            self.assertIn("stopped", {PDF_OUTPUT_STATE_STOPPED})
            self.assertIn("failed", {PDF_OUTPUT_STATE_FAILED})
            self.assertIn("结束原因：已完成，存在需复核页面", report)
            self.assertIn("失败占位页：1", report)

    def test_stopped_manifest_report_marks_user_stop_and_no_placeholder_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            completed_file = PdfFileRecord(
                name="done.pdf",
                source_path="/src/done.pdf",
                relative_path="done.pdf",
                translated_pdf_path="/out/译文(英文)_done_高清.pdf",
                status=PDF_OUTPUT_STATE_COMPLETED,
                page_count=1,
                generated_page_count=1,
            )
            stopped_file = PdfFileRecord(
                name="partial.pdf",
                source_path="/src/partial.pdf",
                relative_path="partial.pdf",
                status=PDF_OUTPUT_STATE_STOPPED,
                page_count=3,
                generated_page_count=1,
                review_enabled=True,
                pages=[
                    PdfPageRecord(
                        page_number=1,
                        source_image_path="/out/_pdf_pages/source_pages/partial/page_001.png",
                        translated_image_path="/out/_pdf_pages/translated_pages/partial/page_001.png",
                        status="success",
                    )
                ],
            )
            summary = PdfTaskSummary(
                status=PDF_OUTPUT_STATE_STOPPED,
                output_dir=str(output_dir),
                target_lang="en",
                target_lang_label="英文",
                started_at="2026-05-25T10:00:00",
                completed_at="2026-05-25T10:01:00",
                elapsed_sec=60,
                file_count=2,
                total_page_count=4,
                generated_pdf_count=1,
                placeholder_page_count=0,
                emergency_ratio_normalized_count=0,
                retry_count=0,
                review_enabled=True,
                partial_artifacts_available=True,
                stopped=True,
                files=[completed_file, stopped_file],
            )

            manifest_path, report_path = write_pdf_manifest_and_report(summary)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            report = report_path.read_text(encoding="utf-8")

            self.assertEqual(manifest["status"], PDF_OUTPUT_STATE_STOPPED)
            self.assertEqual(manifest["terminal_reason"], "user_stopped")
            self.assertEqual(manifest["completed_pdf_file_count"], 1)
            self.assertEqual(manifest["unfinished_pdf_file_count"], 1)
            self.assertIn("结束原因：用户主动中止", report)
            self.assertIn("已完成 PDF 文件：1", report)
            self.assertIn("未完成 PDF 文件：1", report)
            self.assertIn("未生成（未完成，不生成占位版）", report)
            self.assertIn("页面完成进度：1/3", report)
            self.assertIn("已完成到页码：第 1 页", report)
            self.assertIn(
                f"源页素材：{output_dir / '_pdf_pages' / 'source_pages' / 'partial'}",
                report,
            )
            self.assertIn(
                f"译后页素材：{output_dir / '_pdf_pages' / 'translated_pages' / 'partial'}",
                report,
            )
            self.assertIn(
                f"审核候选图：{output_dir / '_pdf_pages' / 'review_candidates' / 'partial'}",
                report,
            )

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
                    max_attempts=max_page_generation_attempts(3),
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
                    max_attempts=max_page_generation_attempts(3),
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
            self.assertEqual(image_client.calls[0]["target_language"], "英文")
            self.assertEqual(image_client.calls[0]["target_lang_code"], "en")
            self.assertIn("编号标签误译", image_client.calls[1]["review_feedback"])

    def test_single_page_all_generation_failures_do_not_create_placeholder_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_pdf = root / "source.pdf"
            source_pdf.write_bytes(b"%PDF-1.4\n")
            settings = AppSettings(target_lang="en")
            settings.output.use_custom_output_dir = True
            settings.output.custom_output_dir = str(root / "out")
            settings.pdf.page_retry_attempts = 0
            settings.image_model_role.source_role = SOURCE_INDEPENDENT
            settings.image_model_role.cloud_provider = "custom_openai"
            settings.image_model_role.cloud_model = "image-model"
            settings.image_model_role.cloud_base_url = "https://images.example/v1"
            runner = PdfImageTranslationRunner(
                [PdfFileItem(path=source_pdf, name="source", size_kb=1.0, page_count=1)],
                settings,
                source_root=root,
                image_client=_AlwaysFailImageClient("temporary image failure"),
                task_logger_enabled=False,
            )

            with patch.dict(
                sys.modules,
                {"fitz": _fake_fitz_module_by_page_count({"source.pdf": 1})},
            ), patch("core.model_roles.get_key", return_value="secret"):
                runner._run()

            done = _drain_last_message(runner, DoneMsg)
            self.assertIsNotNone(done)
            result = done.file_results[0]
            self.assertFalse(result.get("success"))
            self.assertEqual(result.get("placeholder_page_count"), 1)
            self.assertEqual(result.get("output"), "")
            self.assertEqual(result.get("compressed_output"), "")
            self.assertIn("未生成译文 PDF", result.get("error") or "")
            output_dir = Path(done.output_dir)
            self.assertFalse((output_dir / "译文(英文)_source_高清.pdf").exists())
            self.assertFalse((output_dir / "译文(英文)_source_压缩.pdf").exists())
            self.assertEqual(done.issues[0]["file"], "source.pdf")
            self.assertEqual(done.issues[0]["location_label"], "第 1 页")

    def test_review_exhaustion_uses_placeholder_and_marks_file_failed(self) -> None:
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
                    max_attempts=max_page_generation_attempts(3),
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
            self.assertEqual(record.status, PDF_OUTPUT_STATE_FAILED)
            self.assertEqual(record.translated_pdf_path, "")
            self.assertEqual(record.compressed_pdf_path, "")
            self.assertEqual(record.placeholder_page_count, 1)
            self.assertEqual(record.review_failed_page_count, 1)
            self.assertEqual(page.review_status, "failed")
            self.assertTrue(page.placeholder)
            self.assertIn("审核未通过", page.error)
            self.assertIn("未生成译文 PDF", record.error)
            candidate_dir = output_dir / "_pdf_pages" / "review_candidates" / "source" / "page_001"
            self.assertTrue((candidate_dir / "attempt_04.png").exists())

    def test_multi_file_runner_uses_task_level_page_concurrency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first.pdf"
            second = root / "second.pdf"
            first.write_bytes(b"%PDF-1.4\n")
            second.write_bytes(b"%PDF-1.4\n")
            settings = AppSettings(target_lang="en")
            settings.output.use_custom_output_dir = True
            settings.output.custom_output_dir = str(root / "out")
            settings.pdf.page_generation_concurrency = 2
            settings.image_model_role.source_role = SOURCE_INDEPENDENT
            settings.image_model_role.cloud_provider = "custom_openai"
            settings.image_model_role.cloud_model = "image-model"
            settings.image_model_role.cloud_base_url = "https://images.example/v1"
            image_client = _ConcurrentImageClient(_png_bytes(1200, 1600), sleep_seconds=0.03)
            runner = PdfImageTranslationRunner(
                [
                    PdfFileItem(path=first, name="first", size_kb=1.0, page_count=1),
                    PdfFileItem(path=second, name="second", size_kb=1.0, page_count=1),
                ],
                settings,
                source_root=root,
                image_client=image_client,
                task_logger_enabled=False,
            )

            with patch.dict(
                sys.modules,
                {"fitz": _fake_fitz_module_by_page_count({"first.pdf": 1, "second.pdf": 1})},
            ), patch("core.model_roles.get_key", return_value="secret"):
                runner._run()

            done = _drain_last_message(runner, DoneMsg)
            self.assertIsNotNone(done)
            self.assertEqual(image_client.max_active, 2)
            self.assertEqual(len(done.file_results), 2)
            self.assertTrue(all(item.get("success") for item in done.file_results))

    def test_runner_uses_conservative_pdf_concurrency_default(self) -> None:
        settings = AppSettings(target_lang="en")
        settings.engine.concurrency = 20
        settings.pdf.page_generation_concurrency = None
        runner = PdfImageTranslationRunner(
            [],
            settings,
            task_logger_enabled=False,
        )

        self.assertEqual(runner._resolve_pdf_concurrency(), 2)

    def test_stopped_runner_assembles_completed_pdf_but_not_partial_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first.pdf"
            second = root / "second.pdf"
            first.write_bytes(b"%PDF-1.4\n")
            second.write_bytes(b"%PDF-1.4\n")
            settings = AppSettings(target_lang="en")
            settings.output.use_custom_output_dir = True
            settings.output.custom_output_dir = str(root / "out")
            settings.pdf.page_generation_concurrency = 1
            settings.image_model_role.source_role = SOURCE_INDEPENDENT
            settings.image_model_role.cloud_provider = "custom_openai"
            settings.image_model_role.cloud_model = "image-model"
            settings.image_model_role.cloud_base_url = "https://images.example/v1"
            runner = PdfImageTranslationRunner(
                [
                    PdfFileItem(path=first, name="first", size_kb=1.0, page_count=1),
                    PdfFileItem(path=second, name="second", size_kb=1.0, page_count=3),
                ],
                settings,
                source_root=root,
                image_client=_StopAfterFirstImageClient(_png_bytes(1200, 1600)),
                task_logger_enabled=False,
            )
            runner._image_client.runner = runner

            with patch.dict(
                sys.modules,
                {"fitz": _fake_fitz_module_by_page_count({"first.pdf": 1, "second.pdf": 3})},
            ), patch("core.model_roles.get_key", return_value="secret"):
                runner._run()

            stopped = _drain_last_message(runner, StoppedMsg)
            self.assertIsNotNone(stopped)
            output_dir = Path(stopped.output_dir)
            self.assertTrue((output_dir / "译文(英文)_first_高清.pdf").exists())
            self.assertFalse((output_dir / "译文(英文)_second_高清.pdf").exists())
            report = Path(stopped.report_path).read_text(encoding="utf-8")
            self.assertIn("结束原因：用户主动中止", report)
            self.assertIn("未生成（未完成，不生成占位版）", report)

    def test_runner_resume_continues_after_soft_stop_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_pdf = root / "source.pdf"
            source_pdf.write_bytes(b"%PDF-1.4\n")
            settings = AppSettings(target_lang="en")
            settings.output.use_custom_output_dir = True
            settings.output.custom_output_dir = str(root / "out")
            settings.pdf.page_generation_concurrency = 1
            settings.image_model_role.source_role = SOURCE_INDEPENDENT
            settings.image_model_role.cloud_provider = "custom_openai"
            settings.image_model_role.cloud_model = "image-model"
            settings.image_model_role.cloud_base_url = "https://images.example/v1"
            image_client = _StopThenWaitImageClient(_png_bytes(1200, 1600))
            runner = PdfImageTranslationRunner(
                [PdfFileItem(path=source_pdf, name="source", size_kb=1.0, page_count=2)],
                settings,
                source_root=root,
                image_client=image_client,
                task_logger_enabled=False,
            )
            image_client.runner = runner

            with patch.dict(
                sys.modules,
                {"fitz": _fake_fitz_module_by_page_count({"source.pdf": 2})},
            ), patch("core.model_roles.get_key", return_value="secret"), patch(
                "core.pdf_image_translation.PDF_PAGE_RENDER_AHEAD_COUNT",
                0,
            ):
                thread = threading.Thread(target=runner._run)
                thread.start()
                try:
                    self.assertTrue(image_client.stop_seen.wait(2))
                    self.assertTrue(runner.stop_requested())
                    runner.resume()
                    image_client.release.set()
                    thread.join(3)
                finally:
                    image_client.release.set()

            self.assertFalse(thread.is_alive())
            messages = _drain_all_messages(runner)
            self.assertTrue(any(isinstance(msg, DoneMsg) for msg in messages))
            self.assertFalse(any(isinstance(msg, StoppedMsg) for msg in messages))
            self.assertEqual(image_client.calls, 2)

    def test_runner_allows_repeated_stop_and_resume_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_pdf = root / "source.pdf"
            source_pdf.write_bytes(b"%PDF-1.4\n")
            settings = AppSettings(target_lang="en")
            settings.output.use_custom_output_dir = True
            settings.output.custom_output_dir = str(root / "out")
            settings.pdf.page_generation_concurrency = 1
            settings.image_model_role.source_role = SOURCE_INDEPENDENT
            settings.image_model_role.cloud_provider = "custom_openai"
            settings.image_model_role.cloud_model = "image-model"
            settings.image_model_role.cloud_base_url = "https://images.example/v1"
            image_client = _StopOnCallsImageClient(_png_bytes(1200, 1600), stop_calls={1, 2})
            runner = PdfImageTranslationRunner(
                [PdfFileItem(path=source_pdf, name="source", size_kb=1.0, page_count=3)],
                settings,
                source_root=root,
                image_client=image_client,
                task_logger_enabled=False,
            )
            image_client.runner = runner

            with patch.dict(
                sys.modules,
                {"fitz": _fake_fitz_module_by_page_count({"source.pdf": 3})},
            ), patch("core.model_roles.get_key", return_value="secret"), patch(
                "core.pdf_image_translation.PDF_PAGE_RENDER_AHEAD_COUNT",
                0,
            ):
                thread = threading.Thread(target=runner._run)
                thread.start()
                try:
                    self.assertTrue(image_client.stop_events[1].wait(2))
                    self.assertTrue(runner.stop_requested())
                    runner.resume()
                    image_client.release_events[1].set()

                    self.assertTrue(image_client.stop_events[2].wait(2))
                    self.assertTrue(runner.stop_requested())
                    runner.resume()
                    image_client.release_events[2].set()
                    thread.join(3)
                finally:
                    for event in image_client.release_events.values():
                        event.set()

            self.assertFalse(thread.is_alive())
            messages = _drain_all_messages(runner)
            self.assertTrue(any(isinstance(msg, DoneMsg) for msg in messages))
            self.assertFalse(any(isinstance(msg, StoppedMsg) for msg in messages))
            self.assertEqual(image_client.calls, 3)

    def test_runner_resume_interrupts_partial_pdf_assembly_and_finishes_remaining_pages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first.pdf"
            second = root / "second.pdf"
            first.write_bytes(b"%PDF-1.4\n")
            second.write_bytes(b"%PDF-1.4\n")
            settings = AppSettings(target_lang="en")
            settings.output.use_custom_output_dir = True
            settings.output.custom_output_dir = str(root / "out")
            settings.pdf.page_generation_concurrency = 1
            settings.image_model_role.source_role = SOURCE_INDEPENDENT
            settings.image_model_role.cloud_provider = "custom_openai"
            settings.image_model_role.cloud_model = "image-model"
            settings.image_model_role.cloud_base_url = "https://images.example/v1"
            image_client = _StopAfterFirstImageClient(_png_bytes(1200, 1600))
            runner = _SlowAssemblePdfRunner(
                [
                    PdfFileItem(path=first, name="first", size_kb=1.0, page_count=1),
                    PdfFileItem(path=second, name="second", size_kb=1.0, page_count=1),
                ],
                settings,
                source_root=root,
                image_client=image_client,
                task_logger_enabled=False,
            )
            image_client.runner = runner

            with patch.dict(
                sys.modules,
                {"fitz": _fake_fitz_module_by_page_count({"first.pdf": 1, "second.pdf": 1})},
            ), patch("core.model_roles.get_key", return_value="secret"), patch(
                "core.pdf_image_translation.PDF_PAGE_RENDER_AHEAD_COUNT",
                0,
            ):
                thread = threading.Thread(target=runner._run)
                thread.start()
                try:
                    self.assertTrue(runner.assembly_started.wait(2))
                    self.assertTrue(runner.stop_requested())
                    runner.resume()
                    runner.release_assembly.set()
                    thread.join(3)
                finally:
                    runner.release_assembly.set()

            self.assertFalse(thread.is_alive())
            messages = _drain_all_messages(runner)
            logs = [msg.message for msg in messages if isinstance(msg, LogMsg)]
            done = [msg for msg in messages if isinstance(msg, DoneMsg)]
            self.assertTrue(done)
            self.assertFalse(any(isinstance(msg, StoppedMsg) for msg in messages))
            self.assertTrue(
                any("已中断 PDF 合成并清除旧产物，继续翻译剩余页面" in msg for msg in logs)
            )
            self.assertTrue(any("已跳过已完成页面 1 页" in msg for msg in logs))
            self.assertGreaterEqual(runner.assemble_calls, 3)
            self.assertTrue(all(item.get("success") for item in done[-1].file_results))

    def test_resume_continues_when_old_pdf_cleanup_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first.pdf"
            second = root / "second.pdf"
            first.write_bytes(b"%PDF-1.4\n")
            second.write_bytes(b"%PDF-1.4\n")
            settings = AppSettings(target_lang="en")
            settings.output.use_custom_output_dir = True
            settings.output.custom_output_dir = str(root / "out")
            settings.pdf.page_generation_concurrency = 1
            settings.image_model_role.source_role = SOURCE_INDEPENDENT
            settings.image_model_role.cloud_provider = "custom_openai"
            settings.image_model_role.cloud_model = "image-model"
            settings.image_model_role.cloud_base_url = "https://images.example/v1"
            image_client = _StopAfterFirstImageClient(_png_bytes(1200, 1600))
            runner = _SlowAssemblePdfRunner(
                [
                    PdfFileItem(path=first, name="first", size_kb=1.0, page_count=1),
                    PdfFileItem(path=second, name="second", size_kb=1.0, page_count=1),
                ],
                settings,
                source_root=root,
                image_client=image_client,
                task_logger_enabled=False,
            )
            image_client.runner = runner

            with patch.dict(
                sys.modules,
                {"fitz": _fake_fitz_module_by_page_count({"first.pdf": 1, "second.pdf": 1})},
            ), patch("core.model_roles.get_key", return_value="secret"), patch(
                "core.pdf_image_translation.PDF_PAGE_RENDER_AHEAD_COUNT",
                0,
            ), patch(
                "core.pdf_image_translation.Path.unlink",
                side_effect=OSError("locked"),
            ):
                thread = threading.Thread(target=runner._run)
                thread.start()
                try:
                    self.assertTrue(runner.assembly_started.wait(2))
                    runner.resume()
                    runner.release_assembly.set()
                    thread.join(3)
                finally:
                    runner.release_assembly.set()

            self.assertFalse(thread.is_alive())
            messages = _drain_all_messages(runner)
            logs = [msg.message for msg in messages if isinstance(msg, LogMsg)]
            done = [msg for msg in messages if isinstance(msg, DoneMsg)]
            self.assertTrue(done)
            self.assertTrue(all(item.get("success") for item in done[-1].file_results))
            self.assertTrue(any("清除旧 PDF 产物失败" in msg for msg in logs))
            self.assertFalse(any(isinstance(msg, StoppedMsg) for msg in messages))

    def test_render_and_submit_logs_are_diagnostic_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_pdf = root / "source.pdf"
            source_pdf.write_bytes(b"%PDF-1.4\n")
            settings = AppSettings(target_lang="en")
            settings.output.use_custom_output_dir = True
            settings.output.custom_output_dir = str(root / "out")
            settings.image_model_role.source_role = SOURCE_INDEPENDENT
            settings.image_model_role.cloud_provider = "custom_openai"
            settings.image_model_role.cloud_model = "image-model"
            settings.image_model_role.cloud_base_url = "https://images.example/v1"
            runner = PdfImageTranslationRunner(
                [PdfFileItem(path=source_pdf, name="source", size_kb=1.0, page_count=1)],
                settings,
                source_root=root,
                image_client=_FakeImageClient(_png_bytes(1200, 1600)),
                task_logger_enabled=False,
            )

            with patch.dict(
                sys.modules,
                {"fitz": _fake_fitz_module_by_page_count({"source.pdf": 1})},
            ), patch("core.model_roles.get_key", return_value="secret"):
                runner._run()

            logs = _drain_messages(runner, LogMsg)
            render_logs = [msg for msg in logs if "已渲染" in msg.message or "已提交图像生成" in msg.message]
            success_logs = [msg for msg in logs if "生成成功" in msg.message]
            self.assertTrue(render_logs)
            self.assertTrue(all(not msg.visible for msg in render_logs))
            self.assertTrue(success_logs)
            self.assertTrue(all(msg.visible for msg in success_logs))
            self.assertTrue(all("1/" not in msg.message for msg in success_logs))

    def test_page_retry_count_allows_first_attempt_plus_configured_retries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_pdf = root / "source.pdf"
            source_pdf.write_bytes(b"%PDF-1.4\n")
            settings = AppSettings(target_lang="en")
            settings.output.use_custom_output_dir = True
            settings.output.custom_output_dir = str(root / "out")
            settings.pdf.page_retry_attempts = 3
            settings.image_model_role.source_role = SOURCE_INDEPENDENT
            settings.image_model_role.cloud_provider = "custom_openai"
            settings.image_model_role.cloud_model = "image-model"
            settings.image_model_role.cloud_base_url = "https://images.example/v1"
            image_client = _FailThenPassImageClient(
                _png_bytes(1200, 1600),
                failures=3,
            )
            runner = PdfImageTranslationRunner(
                [PdfFileItem(path=source_pdf, name="source", size_kb=1.0, page_count=1)],
                settings,
                source_root=root,
                image_client=image_client,
                task_logger_enabled=False,
            )

            with patch.dict(
                sys.modules,
                {"fitz": _fake_fitz_module_by_page_count({"source.pdf": 1})},
            ), patch("core.model_roles.get_key", return_value="secret"):
                runner._run()

            messages = _drain_all_messages(runner)
            logs = [msg for msg in messages if isinstance(msg, LogMsg)]
            self.assertTrue(any(isinstance(msg, DoneMsg) for msg in messages))
            self.assertEqual(image_client.calls, 4)
            self.assertTrue(any("第 4/4 次生成成功" in msg.message for msg in logs))

    def test_review_success_log_waits_for_review_and_omits_first_attempt_fraction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_pdf = root / "source.pdf"
            source_pdf.write_bytes(b"%PDF-1.4\n")
            settings = AppSettings(target_lang="en")
            settings.output.use_custom_output_dir = True
            settings.output.custom_output_dir = str(root / "out")
            settings.pdf.review_enabled = True
            settings.image_model_role.source_role = SOURCE_INDEPENDENT
            settings.image_model_role.cloud_provider = "custom_openai"
            settings.image_model_role.cloud_model = "image-model"
            settings.image_model_role.cloud_base_url = "https://images.example/v1"
            settings.pdf_review_model_role.source_role = SOURCE_INDEPENDENT
            settings.pdf_review_model_role.cloud_provider = "custom_openai"
            settings.pdf_review_model_role.cloud_model = "vision-review-model"
            settings.pdf_review_model_role.cloud_base_url = "https://images.example/v1"
            review_client = _PassReviewClient()
            runner = PdfImageTranslationRunner(
                [PdfFileItem(path=source_pdf, name="source", size_kb=1.0, page_count=1)],
                settings,
                source_root=root,
                image_client=_FakeImageClient(_png_bytes(1200, 1600)),
                review_client=review_client,
                task_logger_enabled=False,
            )

            with patch.dict(
                sys.modules,
                {"fitz": _fake_fitz_module_by_page_count({"source.pdf": 1})},
            ), patch("core.model_roles.get_key", return_value="secret"):
                runner._run()

            messages = _drain_all_messages(runner)
            success_logs = [
                msg for msg in messages
                if isinstance(msg, LogMsg) and "生成成功" in msg.message
            ]
            self.assertEqual(review_client.calls, 1)
            self.assertTrue(any("生成成功，质检通过" in msg.message for msg in success_logs))
            self.assertTrue(all("1/" not in msg.message for msg in success_logs))


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


class _PassReviewClient:
    def __init__(self) -> None:
        self.calls = 0

    def review_page(self, **_kwargs):
        self.calls += 1
        return PdfPageReviewResult(passed=True, summary="可采用")


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


class _FailThenPassImageClient(_FakeImageClient):
    def __init__(self, image_bytes: bytes, *, failures: int) -> None:
        super().__init__(image_bytes)
        self.failures = failures
        self.calls = 0

    def generate_page(self, **kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("temporary image failure")
        return super().generate_page(**kwargs)


class _AlwaysFailImageClient:
    def __init__(self, message: str) -> None:
        self.message = message
        self.calls = 0

    def generate_page(self, **_kwargs):
        self.calls += 1
        raise RuntimeError(self.message)


class _RecordingImageClient(_FakeImageClient):
    def __init__(self, image_bytes: bytes):
        super().__init__(image_bytes)
        self.calls: list[dict[str, str]] = []

    def generate_page(self, **kwargs):
        self.calls.append(
            {
                "source_image_path": str(kwargs.get("source_image_path") or ""),
                "target_language": str(kwargs.get("target_language") or ""),
                "target_lang_code": str(kwargs.get("target_lang_code") or ""),
                "review_feedback": str(kwargs.get("review_feedback") or ""),
            }
        )
        return super().generate_page(**kwargs)


class _UnavailableImageClient:
    def generate_page(self, **_kwargs):
        raise ImageModelUnavailableError("invalid api key")


class _ConcurrentImageClient(_FakeImageClient):
    def __init__(self, image_bytes: bytes, *, sleep_seconds: float) -> None:
        super().__init__(image_bytes)
        self.sleep_seconds = sleep_seconds
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def generate_page(self, **kwargs):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(self.sleep_seconds)
            return super().generate_page(**kwargs)
        finally:
            with self._lock:
                self.active -= 1


class _StopAfterFirstImageClient(_FakeImageClient):
    def __init__(self, image_bytes: bytes) -> None:
        super().__init__(image_bytes)
        self.calls = 0
        self.runner: PdfImageTranslationRunner | None = None

    def generate_page(self, **kwargs):
        self.calls += 1
        if self.calls == 1 and self.runner is not None:
            self.runner.stop()
        return super().generate_page(**kwargs)


class _StopThenWaitImageClient(_FakeImageClient):
    def __init__(self, image_bytes: bytes) -> None:
        super().__init__(image_bytes)
        self.calls = 0
        self.runner: PdfImageTranslationRunner | None = None
        self.stop_seen = threading.Event()
        self.release = threading.Event()

    def generate_page(self, **kwargs):
        self.calls += 1
        if self.calls == 1 and self.runner is not None:
            self.runner.stop()
            self.stop_seen.set()
            self.release.wait(2)
        return super().generate_page(**kwargs)


class _StopOnCallsImageClient(_FakeImageClient):
    def __init__(self, image_bytes: bytes, *, stop_calls: set[int]) -> None:
        super().__init__(image_bytes)
        self.calls = 0
        self.runner: PdfImageTranslationRunner | None = None
        self.stop_calls = set(stop_calls)
        self.stop_events = {call: threading.Event() for call in self.stop_calls}
        self.release_events = {call: threading.Event() for call in self.stop_calls}

    def generate_page(self, **kwargs):
        self.calls += 1
        call = self.calls
        if call in self.stop_calls and self.runner is not None:
            self.runner.stop()
            self.stop_events[call].set()
            self.release_events[call].wait(2)
        return super().generate_page(**kwargs)


class _SlowAssemblePdfRunner(PdfImageTranslationRunner):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.assembly_started = threading.Event()
        self.release_assembly = threading.Event()
        self.assemble_calls = 0

    def _assemble_translated_pdf(self, record, output_path: Path, *, compressed: bool = False) -> None:
        self.assemble_calls += 1
        if self.assemble_calls == 1:
            self.assembly_started.set()
            self.release_assembly.wait(2)
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(b"%PDF-1.4\n% fake translated pdf\n")


def _drain_messages(runner: PdfImageTranslationRunner, message_type):
    messages = []
    while True:
        message = runner.get_message(timeout=0.0)
        if message is None:
            break
        if isinstance(message, message_type):
            messages.append(message)
    return messages


def _drain_all_messages(runner: PdfImageTranslationRunner):
    messages = []
    while True:
        message = runner.get_message(timeout=0.0)
        if message is None:
            break
        messages.append(message)
    return messages


def _drain_last_message(runner: PdfImageTranslationRunner, message_type):
    messages = _drain_messages(runner, message_type)
    return messages[-1] if messages else None


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


def _fake_fitz_module_by_page_count(page_counts: dict[str, int]):
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
        needs_pass = False

        def __init__(self, path: str):
            self.page_count = page_counts.get(Path(path).name, 1)

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
        return FakeInputDoc(path) if path else FakeOutputDoc()

    return types.SimpleNamespace(
        open=fake_open,
        Matrix=lambda x, y: (x, y),
        Rect=FakeRect,
    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
