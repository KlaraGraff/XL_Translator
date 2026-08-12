from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

from config import PDF_RENDER_DPI_DEFAULT
from core import diagnostics
from core.api_scheduler import WeightedApiScheduler
from core.image_generation import (
    GPT_IMAGE_2_MODEL,
    ImageModelUnavailableError,
    pdf_page_ratio_tolerance,
    pdf_page_request_size,
)
from core.model_throughput import EffectiveModelThroughput
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
    PDF_PAGE_MAX_RENDER_PIXELS,
    PDF_PAGE_MIN_RENDER_DPI,
    PDF_PAGE_STATUS_SKIPPED_OVERSIZE,
    PDF_REPORT_FILENAME,
    SKIP_KIND_IMAGES_DISABLED,
    SOURCE_TYPE_IMAGE,
    SOURCE_TYPE_PDF,
    PdfFileItem,
    PdfFileRecord,
    PdfImageTranslationRunner,
    PdfPageActionError,
    PdfPageRecord,
    PdfTaskSummary,
    check_page_quality,
    create_failure_placeholder_page,
    determine_pdf_task_status,
    fully_skipped_oversize_message,
    is_oversized_page,
    _done_kpi,
    _file_record_to_result,
    _finished_page_count,
    _load_placeholder_font,
    _localized_pdf_placeholder_problem,
    _open_pdf_document,
    _pdf_render_scale_for_page,
    _read_pdf_oversized_page_count,
    _real_generated_page_count,
    _summary_issues,
    _unstarted_page_count,
    max_page_generation_attempts,
    page_image_name,
    resolve_pdf_page_archive_dirs,
    resolve_translated_pdf_variant_paths,
    resolve_translated_pdf_path,
    scan_pdf_path,
    scan_pdf_sources,
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


# ISO 216 精确 pt 尺寸，跟 config.PDF_A4_LONG_EDGE_PT / PDF_A4_SHORT_EDGE_PT 用的是
# 同一组数字——测试要卡在 1.15 倍阈值附近做断言，必须用和生产代码一致的精确值，
# 不能用四舍五入后的近似值（PIL 生成的 PDF 达不到这个精度，所以这里手搓字节）。
_A4_W_PT, _A4_H_PT = 595.276, 841.89
_A3_W_PT, _A3_H_PT = 841.89, 1190.551


def _write_multi_page_pdf(path: Path, page_specs: list[dict]) -> None:
    """手搓一份多页 PDF，每页可以指定精确的 /MediaBox 和可选的 /Rotate。

    没有 reportlab 之类的库可用，PIL 生成的多页 PDF 每页尺寸只是像素/DPI
    换算出来的近似值，达不到「4% 超出阈值」这种测试需要的精度，所以直接手写
    PDF 对象/xref/trailer。每页内容流留空（``BT ET``），这样输出页除了尺寸
    和 /Rotate 以外什么都不携带，方便后面用「页面对象数是不是 0」来判断一页
    在装订产物里到底是矢量原样导入，还是被换成了栅格图片页。
    """
    n = len(page_specs)
    content = b"BT ET"
    content_obj_nums = [4 + 2 * i for i in range(n)]
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))

    objs: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode(),
    ]
    for i, spec in enumerate(page_specs):
        x0, y0, x1, y1 = spec["media_box"]
        page_dict = f"<< /Type /Page /Parent 2 0 R /MediaBox [{x0} {y0} {x1} {y1}]"
        if spec.get("rotate") is not None:
            page_dict += f" /Rotate {spec['rotate']}"
        page_dict += f" /Resources << >> /Contents {content_obj_nums[i]} 0 R >>"
        objs.append(page_dict.encode())
        objs.append(f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream")

    buf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objs, start=1):
        offsets.append(len(buf))
        buf += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_offset = len(buf)
    total = len(objs) + 1
    buf += f"xref\n0 {total}\n".encode()
    buf += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        buf += f"{off:010d} 00000 n \n".encode()
    buf += f"trailer\n<< /Size {total} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF".encode()
    path.write_bytes(bytes(buf))


class PdfImageTranslationTests(unittest.TestCase):
    def test_stop_after_lease_acquire_releases_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_image = root / "source.png"
            source_image.write_bytes(_png_bytes(1200, 1600))
            settings = AppSettings(target_lang="en")
            runner = PdfImageTranslationRunner(
                [],
                settings,
                image_client=_FakeImageClient(_png_bytes(1200, 1600)),
                task_logger_enabled=False,
            )
            scheduler = _StopAfterAcquireScheduler(runner)
            page = PdfPageRecord(
                page_number=1,
                source_image_path=str(source_image),
                source_width_px=1200,
                source_height_px=1600,
            )

            result = runner._generate_page_with_retries(
                page,
                1,
                root / "translated",
                root / "review",
                1,
                scheduler,
                WeightedApiScheduler(1),
                None,
                None,
            )

            self.assertEqual(scheduler.release_count, 1)
            self.assertEqual(scheduler.snapshot().active_total_weight, 0)
            self.assertEqual(result.status, "placeholder_pending")

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
            settings.pdf.target_lang = "en"
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

            with patch.dict(sys.modules, {"pypdfium2": _fake_pdfium_module()}), patch(
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

    def test_real_pdfium_pipeline_preserves_page_dimensions(self) -> None:
        import pypdfium2 as pdfium

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_pdf = root / "source.pdf"
            Image.new("RGB", (1224, 1584), "white").save(
                source_pdf,
                format="PDF",
                resolution=144.0,
            )
            settings = AppSettings(target_lang="en")
            settings.image_model_role.source_role = SOURCE_INDEPENDENT
            settings.image_model_role.cloud_provider = "custom_openai"
            settings.image_model_role.cloud_model = "image-model"
            settings.image_model_role.cloud_base_url = "https://images.example/v1"
            runner = PdfImageTranslationRunner(
                [PdfFileItem(path=source_pdf, name="source", size_kb=1.0, page_count=1)],
                settings,
                source_root=root,
                image_client=_FakeImageClient(_png_bytes(1224, 1584)),
                task_logger_enabled=False,
            )

            with patch("core.model_roles.get_key", return_value="secret"):
                record = runner._process_file(
                    runner._files[0],
                    output_dir=root / "out",
                    app_managed=True,
                    max_attempts=max_page_generation_attempts(0),
                    scheduler=WeightedApiScheduler(1),
                    model_config=resolve_effective_model_config(settings, ROLE_IMAGE),
                    review_model_config=None,
                    concurrency=1,
                    processed_page_offset=0,
                    total_pages=1,
                )

            self.assertEqual(record.status, PDF_OUTPUT_STATE_COMPLETED)
            output = Path(record.translated_pdf_path)
            document = pdfium.PdfDocument(output)
            try:
                self.assertEqual(len(document), 1)
                width, height = document.get_page_size(0)
            finally:
                document.close()
            self.assertAlmostEqual(width, 612.0, places=1)
            self.assertAlmostEqual(height, 792.0, places=1)

    def test_image_pipeline_outputs_model_image_format_without_pdf_variants(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = root / "images"
            source_dir.mkdir()
            source_image = source_dir / "diagram.png"
            source_image.write_bytes(_png_bytes(1200, 1600))
            output_dir = root / "out"
            settings = AppSettings(target_lang="en")
            settings.pdf.target_lang = "en"
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

        # 占位页上印的「问题描述」是给人看的：一句中文，不带请求地址、
        # 不带 MDN 链接、不带英文状态行。
        self.assertIn("网关", problem)
        self.assertNotIn("https://", problem)
        self.assertNotIn("Bad Gateway", problem)
        self.assertNotIn("Server error", problem)
        self.assertNotIn("developer.mozilla.org", problem)

    def test_failure_placeholder_problem_keeps_quality_verdicts_readable(self) -> None:
        self.assertEqual(
            _localized_pdf_placeholder_problem("low resolution"),
            "生成图像分辨率过低。",
        )
        self.assertEqual(
            _localized_pdf_placeholder_problem("ratio error"),
            "页面比例不匹配。",
        )

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

    def test_done_kpi_reports_pages_and_hides_what_did_not_happen(self) -> None:
        # 任务中心的 PDF 指标格全靠这份 kpi；9.2.6 一个字段都不给，跑完点进详情连页数
        # 都看不到。键名必须对上 ui/src/views/tasks.ts 的 PDF 回退表。
        summary = PdfTaskSummary(
            status=PDF_OUTPUT_STATE_COMPLETED,
            output_dir="/out",
            target_lang="en",
            target_lang_label="英文",
            started_at="",
            completed_at="",
            elapsed_sec=1.0,
            file_count=2,
            total_page_count=7,
            generated_pdf_count=2,
            placeholder_page_count=1,
            emergency_ratio_normalized_count=0,
            retry_count=0,
        )

        kpi = _done_kpi(summary)

        self.assertEqual(kpi["file_count"], 2)
        self.assertEqual(kpi["total_page_count"], 7)
        self.assertEqual(kpi["generated_pdf_count"], 2)
        self.assertEqual(kpi["placeholder_page_count"], 1)
        # 没出译图、没有疑点页、没跳过大幅面页，就不要摆一排零出来让人以为哪里坏了。
        self.assertNotIn("generated_image_count", kpi)
        self.assertNotIn("suspect_adopted_page_count", kpi)
        self.assertNotIn("skipped_oversize_page_count", kpi)
        # 跟「失败占位」并列的那格只能是与它互斥的 suspect_adopted_page_count。
        # review_failed_page_count 里有一半是直接退回占位页的同一批页，摆进这排指标
        # 就会让一页坏页在两个格子里各数一次。
        summary.review_enabled = True
        summary.review_failed_page_count = 2
        kpi = _done_kpi(summary)
        self.assertNotIn("review_failed_page_count", kpi)

        summary.suspect_adopted_page_count = 1
        summary.skipped_oversize_page_count = 4
        summary.generated_image_count = 3
        kpi = _done_kpi(summary)

        self.assertEqual(kpi["suspect_adopted_page_count"], 1)
        self.assertEqual(kpi["skipped_oversize_page_count"], 4)
        self.assertEqual(kpi["generated_image_count"], 3)
        self.assertNotIn("review_failed_page_count", kpi)
        # 跑完的任务不出「已生成页 / 未开始页」：已生成页数就是总页数，多两格只是占地方。
        self.assertNotIn("generated_page_count", kpi)
        self.assertNotIn("unstarted_page_count", kpi)

    def test_done_kpi_gives_page_counts_when_the_task_stopped(self) -> None:
        # 中止的任务在任务中心此前是一排零（高清 PDF 0 / 失败占位 0），而工作区同一份任务
        # 写着「已生成 9 / 14 页」。两屏必须给得出同一个数。
        summary = PdfTaskSummary(
            status=PDF_OUTPUT_STATE_STOPPED,
            output_dir="/out",
            target_lang="en",
            target_lang_label="英文",
            started_at="",
            completed_at="",
            elapsed_sec=1.0,
            file_count=1,
            total_page_count=14,
            generated_pdf_count=0,
            placeholder_page_count=3,
            emergency_ratio_normalized_count=0,
            retry_count=0,
            generated_page_count=9,
            unstarted_page_count=2,
            stopped=True,
        )

        kpi = _done_kpi(summary)

        self.assertEqual(kpi["generated_page_count"], 9)
        self.assertEqual(kpi["unstarted_page_count"], 2)
        # 失败的任务同理；已生成 0 页也照样占格子，那正是用户要看的数。
        summary.stopped = False
        summary.status = PDF_OUTPUT_STATE_FAILED
        summary.generated_page_count = 0
        summary.unstarted_page_count = 0
        kpi = _done_kpi(summary)
        self.assertEqual(kpi["generated_page_count"], 0)
        self.assertNotIn("unstarted_page_count", kpi)

    def test_pending_placeholder_pages_are_not_counted_as_generated(self) -> None:
        # placeholder_pending 的页带着一个「page_00N_failed.png」路径，那是占位图**将要**
        # 写到的位置；中止时那一步根本没跑，文件并不存在。把它算成已完成，报告就会写
        # 「12/14」（其实是已提交数），跟界面的「已生成 9 / 14 页」对不上。
        record = PdfFileRecord(
            name="a.pdf",
            source_path="/src/a.pdf",
            relative_path="a.pdf",
            page_count=4,
            pages=[
                PdfPageRecord(
                    page_number=1,
                    source_image_path="/s/page_001.png",
                    translated_image_path="/t/page_001.png",
                    status="success",
                ),
                PdfPageRecord(
                    page_number=2,
                    source_image_path="/s/page_002.png",
                    translated_image_path="/t/page_002_failed.png",
                    status="placeholder_pending",
                    placeholder=True,
                ),
            ],
        )

        self.assertEqual(_finished_page_count(record), 1)
        self.assertEqual(_unstarted_page_count(record), 2)

    def test_placeholder_and_oversize_pages_do_not_count_as_generated(self) -> None:
        # 界面上的「已生成 N / M 页」早就是扣掉占位页之后的数。报告和任务中心要是把占位页
        # 也算进「已生成页面」，就会出现「已生成页面 4/4」和「失败占位页 1」并排的写法。
        record = PdfFileRecord(
            name="a.pdf",
            source_path="/src/a.pdf",
            relative_path="a.pdf",
            page_count=4,
            placeholder_page_count=1,
            skipped_oversize_page_count=1,
            pages=[
                PdfPageRecord(
                    page_number=1,
                    source_image_path="/s/page_001.png",
                    translated_image_path="/t/page_001.png",
                    status="success",
                ),
                PdfPageRecord(
                    page_number=2,
                    source_image_path="/s/page_002.png",
                    translated_image_path="/t/page_002.png",
                    status="success",
                ),
                PdfPageRecord(
                    page_number=3,
                    source_image_path="/s/page_003.png",
                    translated_image_path="/t/page_003_failed.png",
                    status="placeholder",
                    placeholder=True,
                ),
                PdfPageRecord(
                    page_number=4,
                    source_image_path="/s/page_004.png",
                    translated_image_path="/t/page_004.png",
                    status=PDF_PAGE_STATUS_SKIPPED_OVERSIZE,
                    skipped_oversize=True,
                ),
            ],
        )

        self.assertEqual(_finished_page_count(record), 4)
        self.assertEqual(_real_generated_page_count(record), 2)
        self.assertEqual(_unstarted_page_count(record), 0)

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
            # 「完成」在中止的报告里会被读成「已提交」，措辞统一到「已生成」；未开始的页
            # 单独占一行，和「失败占位页」加起来正好补满总页数。
            self.assertIn("已生成页面：1/3", report)
            self.assertIn("已连续处理到页码：第 1 页", report)
            self.assertIn("未开始页面：2", report)
            # 跑完的那份文件没有留页记录（只有产物路径），不能被算成「未开始 1 页」。
            self.assertIn("已生成页面：1/1", report)
            self.assertIn("未开始页面：0", report)
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
            self.assertFalse(any("source" in name or "task/" in name for name in names))
            self.assertTrue(any(name.endswith("manifest.json") for name in names))

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

            with patch.dict(sys.modules, {"pypdfium2": _fake_pdfium_module()}), patch(
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
            # 用户看到的是一句中文，不是上游那句 "invalid api key"。
            self.assertNotIn("invalid api key", record.error)
            self.assertIn("API Key", record.error)
            self.assertIn("请在设置里检查", record.error)

    def test_review_failure_regenerates_from_source_and_keeps_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_pdf = root / "source.pdf"
            source_pdf.write_bytes(b"%PDF-1.4\n")
            output_dir = root / "out"
            settings = AppSettings(target_lang="en")
            settings.pdf.target_lang = "en"
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

            with patch.dict(sys.modules, {"pypdfium2": _fake_pdfium_module()}), patch(
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

    def test_review_request_error_keeps_candidate_without_regeneration(self) -> None:
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
            image_client = _RecordingImageClient(_png_bytes(1200, 1600))
            review_client = _ReviewRequestErrorClient()
            runner = PdfImageTranslationRunner(
                [PdfFileItem(path=source_pdf, name="source", size_kb=1.0, page_count=1)],
                settings,
                source_root=root,
                image_client=image_client,
                review_client=review_client,
                task_logger_enabled=False,
            )

            with patch.dict(sys.modules, {"pypdfium2": _fake_pdfium_module()}), patch(
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
            self.assertEqual(len(image_client.calls), 1)
            self.assertEqual(review_client.calls, 1)
            self.assertEqual(record.status, PDF_OUTPUT_STATE_NEEDS_REVIEW)
            self.assertEqual(page.status, "success")
            self.assertEqual(page.review_status, "failed")
            self.assertIn("审核请求失败", page.error)
            self.assertTrue(Path(page.translated_image_path).exists())

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
                {"pypdfium2": _fake_pdfium_module_by_page_count({"source.pdf": 1})},
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
            # 指标格接的是这份 kpi。它是不是真的挂在终态消息上，只有这里能验：
            # 单测 _done_kpi 本身通过了，也不代表有人把它交给了 DoneMsg。
            self.assertEqual(done.kpi["file_count"], 1)
            self.assertEqual(done.kpi["total_page_count"], 1)
            self.assertEqual(done.kpi["placeholder_page_count"], 1)

    def test_upstream_503_failure_reaches_user_as_plain_chinese(self) -> None:
        """A 503 from the image endpoint must not print its URL/JSON body anywhere."""
        upstream_blob = (
            "Server error '503 Service Unavailable' for url "
            "'https://api.ai-pixel.online/v1/images/edits'\n"
            "For more information check: "
            "https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503\n"
            '接口返回：{"error":{"message":"Service temporarily unavailable",'
            '"type":"api_error"}}'
        )
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
                image_client=_AlwaysFailImageClient(upstream_blob),
                task_logger_enabled=False,
            )

            with patch.dict(
                sys.modules,
                {"pypdfium2": _fake_pdfium_module_by_page_count({"source.pdf": 1})},
            ), patch("core.model_roles.get_key", return_value="secret"):
                runner._run()

            done = _drain_last_message(runner, DoneMsg)
            self.assertIsNotNone(done)
            result = done.file_results[0]
            output_dir = Path(done.output_dir)
            manifest = json.loads(
                (output_dir / "pdf_translation_manifest.json").read_text(encoding="utf-8")
            )
            page_entry = manifest["files"][0]["pages"][0]
            visible_texts = [
                result.get("error") or "",
                done.issues[0].get("problem") or "",
                page_entry.get("error") or "",
                (output_dir / "pdf_translation_report.md").read_text(encoding="utf-8"),
            ]
            self.assertIn("未生成译文 PDF", result.get("error") or "")
            for text in visible_texts:
                self.assertIn("接口所在的服务暂时不可用", text)
                self.assertNotIn("http", text)
                self.assertNotIn('{"error"', text)
                self.assertNotIn("Service Unavailable", text)
                self.assertNotIn("Server error", text)
                self.assertNotIn("api_error", text)
                self.assertNotIn("For more information check", text)

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

            with patch.dict(sys.modules, {"pypdfium2": _fake_pdfium_module()}), patch(
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
            settings.pdf.target_lang = "en"
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
                {"pypdfium2": _fake_pdfium_module_by_page_count({"first.pdf": 1, "second.pdf": 1})},
            ), patch("core.model_roles.get_key", return_value="secret"):
                runner._run()

            done = _drain_last_message(runner, DoneMsg)
            self.assertIsNotNone(done)
            self.assertEqual(image_client.max_active, 2)
            self.assertEqual(len(done.file_results), 2)
            self.assertTrue(all(item.get("success") for item in done.file_results))

    def test_runner_uses_direct_mode_pdf_concurrency_default(self) -> None:
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
            settings.pdf.target_lang = "en"
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
                {"pypdfium2": _fake_pdfium_module_by_page_count({"first.pdf": 1, "second.pdf": 3})},
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
            # 中止的任务也要交出逐文件结果和指标：停在半路正是要看「哪几个文件已经写出来了」
            # 的时候，而这里此前只给一句话，任务中心的文件表和指标格都是空的。
            self.assertEqual([item["name"] for item in stopped.files], ["first.pdf", "second.pdf"])
            self.assertTrue(stopped.files[0]["output"])
            self.assertFalse(stopped.files[1]["output"])
            self.assertEqual(stopped.kpi["file_count"], 2)
            self.assertEqual(stopped.kpi["total_page_count"], 4)
            self.assertEqual(stopped.kpi["generated_pdf_count"], 1)
            # 中止的任务在任务中心不能只剩「高清 PDF 0 / 失败占位 0」：工作区那一屏写着
            # 「已生成 N / M 页」，这里得给得出同一个数。中止路径此前直接 return，逐页计数
            # 一次都没刷新过，清单里 generated_page_count 永远是 0。
            self.assertEqual(
                stopped.kpi["generated_page_count"],
                sum(item["page_count"] for item in stopped.files if item["output"]),
            )
            self.assertGreater(stopped.kpi["unstarted_page_count"], 0)
            manifest = json.loads(Path(stopped.manifest_path).read_text(encoding="utf-8"))
            self.assertEqual(
                manifest["generated_page_count"], stopped.kpi["generated_page_count"]
            )
            self.assertEqual(manifest["files"][0]["generated_page_count"], 1)

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
                {"pypdfium2": _fake_pdfium_module_by_page_count({"source.pdf": 2})},
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
                {"pypdfium2": _fake_pdfium_module_by_page_count({"source.pdf": 3})},
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
                {"pypdfium2": _fake_pdfium_module_by_page_count({"first.pdf": 1, "second.pdf": 1})},
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
                {"pypdfium2": _fake_pdfium_module_by_page_count({"first.pdf": 1, "second.pdf": 1})},
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
                {"pypdfium2": _fake_pdfium_module_by_page_count({"source.pdf": 1})},
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
                {"pypdfium2": _fake_pdfium_module_by_page_count({"source.pdf": 1})},
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
                {"pypdfium2": _fake_pdfium_module_by_page_count({"source.pdf": 1})},
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

    def test_paused_page_regenerate_reruns_the_page_without_duplicate_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_pdf = root / "source.pdf"
            source_pdf.write_bytes(b"%PDF-1.4\n")
            settings = _page_review_settings(root)
            image_client = _PauseOnCallImageClient(
                _png_bytes(1200, 1600),
                pause_calls={1},
                payloads={2: _png_bytes(1200, 1600, "red")},
            )
            runner = _PauseAwarePdfRunner(
                [PdfFileItem(path=source_pdf, name="source", size_kb=1.0, page_count=1)],
                settings,
                source_root=root,
                image_client=image_client,
                task_logger_enabled=False,
            )
            image_client.runner = runner

            with patch.dict(
                sys.modules,
                {"pypdfium2": _fake_pdfium_module_by_page_count({"source.pdf": 1})},
            ), patch("core.model_roles.get_key", return_value="secret"), patch(
                "core.pdf_image_translation.PDF_PAGE_RENDER_AHEAD_COUNT",
                0,
            ):
                thread = threading.Thread(target=runner._run)
                thread.start()
                try:
                    self.assertTrue(runner.pause_wait_entered.wait(3))
                    paused_snapshot = runner.pdf_page_snapshot()
                    accepted = runner.request_page_regenerate(
                        relative_path="source.pdf",
                        page_number=1,
                    )
                    runner.resume()
                    thread.join(5)
                finally:
                    runner.resume()

            self.assertFalse(thread.is_alive())
            self.assertEqual(accepted["action"], "regenerate")
            self.assertEqual(accepted["applies_on"], "resume")
            paused_page = paused_snapshot["files"][0]["pages"][0]
            self.assertEqual(paused_page["status"], "success")
            self.assertTrue(paused_page["has_translated_image"])

            messages = _drain_all_messages(runner)
            self.assertTrue(any(isinstance(msg, DoneMsg) for msg in messages))
            self.assertFalse(any(isinstance(msg, StoppedMsg) for msg in messages))
            self.assertEqual(image_client.calls, 2)

            prepared = runner._prepared_files[0]
            record = prepared.record
            self.assertEqual([page.page_number for page in record.pages], [1])
            self.assertEqual(record.pages[0].status, "success")
            self.assertEqual(record.status, PDF_OUTPUT_STATE_COMPLETED)
            self.assertEqual(runner._completed_page_count, 1)
            self.assertEqual(runner._total_page_count, 1)
            translated = Path(record.pages[0].translated_image_path)
            self.assertTrue(translated.is_file())
            with Image.open(translated) as image:
                # The second payload proves the page really ran again and the
                # stale translated image was replaced rather than kept.
                self.assertEqual(image.convert("RGB").getpixel((0, 0)), (255, 0, 0))

    def test_paused_page_skip_accepts_the_failed_page_as_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_pdf = root / "source.pdf"
            source_pdf.write_bytes(b"%PDF-1.4\n")
            settings = _page_review_settings(root)
            settings.pdf.page_retry_attempts = 0
            image_client = _PauseOnCallImageClient(
                _png_bytes(1200, 1600),
                pause_calls={1},
                fail_calls={1},
            )
            runner = _PauseAwarePdfRunner(
                [PdfFileItem(path=source_pdf, name="source", size_kb=1.0, page_count=2)],
                settings,
                source_root=root,
                image_client=image_client,
                task_logger_enabled=False,
            )
            image_client.runner = runner

            with patch.dict(
                sys.modules,
                {"pypdfium2": _fake_pdfium_module_by_page_count({"source.pdf": 2})},
            ), patch("core.model_roles.get_key", return_value="secret"), patch(
                "core.pdf_image_translation.PDF_PAGE_RENDER_AHEAD_COUNT",
                0,
            ):
                thread = threading.Thread(target=runner._run)
                thread.start()
                try:
                    self.assertTrue(runner.pause_wait_entered.wait(3))
                    paused_snapshot = runner.pdf_page_snapshot()
                    with self.assertRaises(PdfPageActionError):
                        # The second page has not been generated yet.
                        runner.request_page_skip(relative_path="source.pdf", page_number=2)
                    accepted = runner.request_page_skip(
                        relative_path="source.pdf",
                        page_number=1,
                    )
                    queued_snapshot = runner.pdf_page_snapshot()
                    runner.resume()
                    thread.join(5)
                finally:
                    runner.resume()

            self.assertFalse(thread.is_alive())
            self.assertEqual(accepted["action"], "skip")
            paused_page = paused_snapshot["files"][0]["pages"][0]
            self.assertEqual(paused_page["status"], "placeholder_pending")
            self.assertFalse(paused_page["has_translated_image"])
            self.assertFalse(paused_page["user_skipped"])
            self.assertEqual(queued_snapshot["files"][0]["pages"][0]["pending_action"], "skip")

            messages = _drain_all_messages(runner)
            self.assertTrue(any(isinstance(msg, DoneMsg) for msg in messages))
            self.assertEqual(image_client.calls, 2)

            record = runner._prepared_files[0].record
            self.assertEqual(
                sorted(page.page_number for page in record.pages),
                [1, 2],
            )
            skipped_page = next(page for page in record.pages if page.page_number == 1)
            self.assertEqual(skipped_page.status, "placeholder")
            self.assertTrue(skipped_page.placeholder)
            self.assertEqual(record.status, PDF_OUTPUT_STATE_NEEDS_REVIEW)

            final_page = runner.pdf_page_snapshot()["files"][0]["pages"][0]
            self.assertTrue(final_page["user_skipped"])
            self.assertEqual(final_page["pending_action"], "")
            self.assertTrue(final_page["has_translated_image"])

    def test_page_action_requests_reject_pages_that_cannot_be_touched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_pdf = root / "source.pdf"
            source_pdf.write_bytes(b"%PDF-1.4\n")
            settings = _page_review_settings(root)
            runner = PdfImageTranslationRunner(
                [PdfFileItem(path=source_pdf, name="source", size_kb=1.0, page_count=2)],
                settings,
                source_root=root,
                image_client=_FakeImageClient(_png_bytes(1200, 1600)),
                task_logger_enabled=False,
            )

            output_dir = root / "out" / "pages"
            output_dir.mkdir(parents=True, exist_ok=True)
            with patch.dict(
                sys.modules,
                {"pypdfium2": _fake_pdfium_module_by_page_count({"source.pdf": 2})},
            ), patch("core.model_roles.get_key", return_value="secret"):
                prepared_files = runner._prepare_pdf_files(
                    output_dir=output_dir,
                    app_managed=True,
                )
            runner._prepared_files = list(prepared_files)
            record = prepared_files[0].record

            # No page record yet: the page is still owned by the pipeline.
            with self.assertRaises(PdfPageActionError):
                runner.request_page_regenerate(relative_path="source.pdf", page_number=1)
            with self.assertRaises(PdfPageActionError):
                runner.request_page_regenerate(relative_path="missing.pdf", page_number=1)
            with self.assertRaises(PdfPageActionError):
                runner.request_page_regenerate(
                    relative_path="../../etc/passwd",
                    page_number=1,
                )
            with self.assertRaises(PdfPageActionError):
                runner.request_page_regenerate(relative_path="source.pdf", page_number=0)
            with self.assertRaises(PdfPageActionError):
                runner.request_page_regenerate(relative_path="source.pdf", page_number=9)

            record.pages.append(
                PdfPageRecord(
                    page_number=1,
                    source_image_path=str(prepared_files[0].source_pages_dir / "page_1.png"),
                    file_name=record.name,
                    status="success",
                )
            )
            self.assertEqual(
                runner.request_page_regenerate(
                    relative_path="source.pdf",
                    page_number=1,
                )["page_number"],
                1,
            )
            with self.assertRaises(PdfPageActionError):
                runner.request_page_skip(relative_path="source.pdf", page_number=1)

            record.pages[0].status = "placeholder_pending"
            self.assertEqual(
                runner.request_page_skip(
                    relative_path="source.pdf",
                    page_number=1,
                )["action"],
                "skip",
            )
            # The later request replaces the earlier one for the same page.
            self.assertEqual(len(runner._pending_page_actions), 1)
            self.assertEqual(runner._pending_page_actions[0].kind, "skip")

            record.status = PDF_OUTPUT_STATE_FAILED
            with self.assertRaises(PdfPageActionError):
                runner.request_page_regenerate(relative_path="source.pdf", page_number=1)

    def test_page_snapshot_exposes_quality_flags_even_when_review_passed(self) -> None:
        """本地质检的疑点必须单独送到前端，不能只靠 review_status。

        质检跑在送审之前，审核判「通过」也不会清掉 quality_flags，所以一页可以同时是
        「有疑点」和 review_status="passed"。这两个字段就是小结里「N 页译文有疑点，已采用
        但建议复核」的来源；快照里不给，逐页表格就只能照 review_status 显示绿色的「通过」，
        用户看到小结说有疑点却一页都找不到。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_pdf = root / "source.pdf"
            source_pdf.write_bytes(b"%PDF-1.4\n")
            settings = _page_review_settings(root)
            runner = PdfImageTranslationRunner(
                [PdfFileItem(path=source_pdf, name="source", size_kb=1.0, page_count=1)],
                settings,
                source_root=root,
                image_client=_FakeImageClient(_png_bytes(1200, 1600)),
                task_logger_enabled=False,
            )
            with patch.dict(
                sys.modules,
                {"pypdfium2": _fake_pdfium_module_by_page_count({"source.pdf": 1})},
            ), patch("core.model_roles.get_key", return_value="secret"):
                runner._run()

            page = runner._prepared_files[0].record.pages[0]
            entry = runner.pdf_page_snapshot()["files"][0]["pages"][0]
            self.assertFalse(entry["quality_flagged"])
            self.assertEqual(entry["quality_message"], "")
            self.assertFalse(entry["emergency_ratio_normalized"])

            page.review_status = "passed"
            page.quality_flags = ["text_ratio"]
            page.quality_message = "译文字数明显少于原文。"
            page.emergency_ratio_normalized = True
            entry = runner.pdf_page_snapshot()["files"][0]["pages"][0]
            self.assertEqual(entry["review_status"], "passed")
            self.assertTrue(entry["quality_flagged"])
            self.assertEqual(entry["quality_message"], "译文字数明显少于原文。")
            self.assertTrue(entry["emergency_ratio_normalized"])

    def test_page_image_paths_stay_inside_the_task_archive_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_pdf = root / "source.pdf"
            source_pdf.write_bytes(b"%PDF-1.4\n")
            settings = _page_review_settings(root)
            runner = PdfImageTranslationRunner(
                [PdfFileItem(path=source_pdf, name="source", size_kb=1.0, page_count=1)],
                settings,
                source_root=root,
                image_client=_FakeImageClient(_png_bytes(1200, 1600)),
                task_logger_enabled=False,
            )

            with patch.dict(
                sys.modules,
                {"pypdfium2": _fake_pdfium_module_by_page_count({"source.pdf": 1})},
            ), patch("core.model_roles.get_key", return_value="secret"):
                runner._run()

            prepared = runner._prepared_files[0]
            source_image = runner.resolve_page_image_path(
                relative_path="source.pdf",
                page_number=1,
                kind="source",
            )
            translated_image = runner.resolve_page_image_path(
                relative_path="source.pdf",
                page_number=1,
                kind="translated",
            )
            self.assertIsNotNone(source_image)
            self.assertIsNotNone(translated_image)
            self.assertEqual(source_image.parent, prepared.source_pages_dir)
            self.assertEqual(translated_image.parent, prepared.translated_pages_dir)

            for traversal_key in ("../../etc/passwd", "/etc/passwd", "", "source.pdf/../x"):
                self.assertIsNone(
                    runner.resolve_page_image_path(
                        relative_path=traversal_key,
                        page_number=1,
                        kind="source",
                    )
                )
            self.assertIsNone(
                runner.resolve_page_image_path(
                    relative_path="source.pdf",
                    page_number=2,
                    kind="source",
                )
            )
            with self.assertRaises(PdfPageActionError):
                runner.resolve_page_image_path(
                    relative_path="source.pdf",
                    page_number=1,
                    kind="../secrets",
                )

            snapshot = runner.pdf_page_snapshot()
            # 卡片副标题按这个字段改口：审核关着时还写「审核模型逐页检查」，跟同一张表里
            # 每一行的「未审核」自相矛盾。
            self.assertFalse(snapshot["review_enabled"])
            runner._settings.pdf.review_enabled = True
            self.assertTrue(runner.pdf_page_snapshot()["review_enabled"])
            self.assertEqual(len(snapshot["files"]), 1)
            self.assertEqual(snapshot["files"][0]["relative_path"], "source.pdf")
            self.assertEqual(len(snapshot["files"][0]["pages"]), 1)
            self.assertNotIn(str(root), json.dumps(snapshot, ensure_ascii=False))

    def test_is_oversized_page_compares_long_and_short_edges_not_width_height(self) -> None:
        # 竖版 A4：远低于阈值。
        self.assertFalse(is_oversized_page(_A4_W_PT, _A4_H_PT))
        # 横版 A4：宽度（841.89pt）本身超过 A4 短边阈值，如果误用「宽比宽」
        # 就会被错判成超大页；用长边比长边、短边比短边就不会。
        self.assertFalse(is_oversized_page(_A4_H_PT, _A4_W_PT))
        # 竖版/横版 A3：都应该命中。
        self.assertTrue(is_oversized_page(_A3_W_PT, _A3_H_PT))
        self.assertTrue(is_oversized_page(_A3_H_PT, _A3_W_PT))
        # 旋转 90 度的 A3：pypdfium2 的 page.get_size() 已经把宽高互换，
        # 传进来的就是横版尺寸，判定函数不需要再关心 /Rotate。
        self.assertTrue(is_oversized_page(_A3_H_PT, _A3_W_PT))
        # 超出 A4 4%：明显小于 1.15 倍阈值，不能被当成大幅面页。
        self.assertFalse(is_oversized_page(_A4_W_PT * 1.04, _A4_H_PT * 1.04))

    def test_skip_oversized_pages_end_to_end_preserves_order_and_vector_page(self) -> None:
        import pypdfium2 as pdfium

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_pdf = root / "source.pdf"
            _write_multi_page_pdf(
                source_pdf,
                [
                    {"media_box": (0, 0, _A4_W_PT, _A4_H_PT)},
                    {"media_box": (0, 0, _A3_W_PT, _A3_H_PT)},
                ],
            )
            output_dir = root / "out"
            settings = AppSettings(target_lang="en")
            settings.pdf.target_lang = "en"
            settings.pdf.skip_oversized_pages = True
            settings.image_model_role.source_role = SOURCE_INDEPENDENT
            settings.image_model_role.cloud_provider = "custom_openai"
            settings.image_model_role.cloud_model = "image-model"
            settings.image_model_role.cloud_base_url = "https://images.example/v1"
            image_client = _RecordingImageClient(_png_bytes(2481, 3508))
            runner = PdfImageTranslationRunner(
                [PdfFileItem(path=source_pdf, name="source", size_kb=1.0, page_count=2)],
                settings,
                source_root=root,
                image_client=image_client,
                task_logger_enabled=False,
            )

            with patch("core.model_roles.get_key", return_value="secret"):
                prepared = runner._prepare_pdf_files(output_dir=output_dir, app_managed=True)[0]
                runner._total_page_count = 2
                runner._process_prepared_pages(
                    [prepared],
                    max_attempts=max_page_generation_attempts(0),
                    scheduler=WeightedApiScheduler(1),
                    review_scheduler=WeightedApiScheduler(1),
                    model_config=resolve_effective_model_config(settings, ROLE_IMAGE),
                    review_model_config=None,
                    concurrency=1,
                    total_pages=2,
                )
                runner._finalize_file_record(prepared, should_assemble=True)

            record = prepared.record
            # 大幅面页从未被送进翻译模型：录制客户端只应该收到 1 次调用。
            self.assertEqual(len(image_client.calls), 1)
            self.assertEqual(record.status, PDF_OUTPUT_STATE_COMPLETED)
            self.assertEqual(record.page_count, 2)
            self.assertEqual(record.skipped_oversize_page_count, 1)
            self.assertEqual(runner._completed_page_count, 2)
            self.assertTrue(runner._record_has_all_pages_finished(record))

            pages = sorted(record.pages, key=lambda item: item.page_number)
            self.assertEqual(pages[0].status, "success")
            self.assertFalse(pages[0].skipped_oversize)
            self.assertEqual(pages[1].status, PDF_PAGE_STATUS_SKIPPED_OVERSIZE)
            self.assertTrue(pages[1].skipped_oversize)

            # 逐字段确认 skipped_oversize 能到达序列化给前端的页面快照。
            # pdf_page_snapshot() 读的是 runner._prepared_files，这里绕开了
            # _run() 的整体调度，所以要手动装填，跟其它同样绕开 _run() 的
            # 测试（如上面的 resolve_page_image_path 用例）做法一致。
            runner._prepared_files = [prepared]
            snapshot = runner.pdf_page_snapshot()
            snapshot_pages = snapshot["files"][0]["pages"]
            self.assertEqual(snapshot_pages[0]["skipped_oversize"], False)
            self.assertEqual(snapshot_pages[1]["skipped_oversize"], True)

            output = Path(record.translated_pdf_path)
            document = pdfium.PdfDocument(output)
            try:
                self.assertEqual(len(document), 2)
                translated_page = document.get_page(0)
                try:
                    # 第 1 页是模型译图：至少带一个插入的图片对象。
                    self.assertGreaterEqual(len(list(translated_page.get_objects())), 1)
                finally:
                    translated_page.close()
                vector_page = document.get_page(1)
                try:
                    # 第 2 页是矢量直传：内容流原样保留（空），没有被换成栅格图片，
                    # 尺寸也跟原始 A3 页完全一致（不是渲染后近似值）。
                    self.assertEqual(len(list(vector_page.get_objects())), 0)
                    width, height = vector_page.get_size()
                    self.assertAlmostEqual(width, _A3_W_PT, places=1)
                    self.assertAlmostEqual(height, _A3_H_PT, places=1)
                finally:
                    vector_page.close()
            finally:
                document.close()

    def test_scan_time_oversized_page_count_is_tri_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mixed_pdf = root / "mixed.pdf"
            _write_multi_page_pdf(
                mixed_pdf,
                [
                    {"media_box": (0, 0, _A4_W_PT, _A4_H_PT)},
                    {"media_box": (0, 0, _A3_W_PT, _A3_H_PT)},
                    {"media_box": (0, 0, _A3_H_PT, _A3_W_PT)},
                ],
            )
            plain_pdf = root / "plain.pdf"
            _write_multi_page_pdf(plain_pdf, [{"media_box": (0, 0, _A4_W_PT, _A4_H_PT)}])

            result = scan_pdf_sources(root)
            items_by_name = {item.name: item for item in result.items}
            self.assertEqual(items_by_name["mixed"].oversized_page_count, 2)
            self.assertEqual(items_by_name["plain"].oversized_page_count, 0)
            self.assertEqual(result.summary["oversized_page_count"], 2)
            self.assertEqual(result.summary["oversized_page_count_unknown_files"], 0)

    def test_scan_lists_folder_images_as_skipped_when_the_switch_is_off(self) -> None:
        # 「允许独立图片」关着时，文件夹里的图片以前是被 continue 直接吃掉的：扫描摘要写
        # 「独立图片 0 / 跳过项 0」，日志写「0 个图片文件」。用户把图纸截图放进来，从头到尾
        # 没有一句话告诉他这些图根本没参与。跳过项这一格正是它该出现的地方。
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_multi_page_pdf(root / "doc.pdf", [{"media_box": (0, 0, _A4_W_PT, _A4_H_PT)}])
            (root / "img.png").write_bytes(_png_bytes(64, 64))
            (root / "photo.heic").write_bytes(b"not really a heic file")

            off = scan_pdf_sources(root, include_images=False)

            self.assertEqual([item.path.name for item in off.items], ["doc.pdf"])
            skipped = {item.relative_path: item for item in off.skipped}
            self.assertEqual(set(skipped), {"img.png", "photo.heic"})
            self.assertEqual(skipped["img.png"].kind, SKIP_KIND_IMAGES_DISABLED)
            self.assertEqual(skipped["img.png"].source_type, SOURCE_TYPE_IMAGE)
            self.assertIn("允许独立图片", skipped["img.png"].reason)
            self.assertEqual(off.summary["skipped_count"], 2)
            self.assertIn("2 个图片文件", str(off.risk["message"]))

            on = scan_pdf_sources(root, include_images=True)

            self.assertIn("img.png", {item.path.name for item in on.items})
            # 开关打开后，认得出但不受支持的格式仍然按「格式」报，不再赖到开关头上。
            heic = [item for item in on.skipped if item.relative_path == "photo.heic"]
            self.assertEqual(len(heic), 1)
            self.assertEqual(heic[0].kind, "")
            self.assertIn("不支持的图片格式", heic[0].reason)

    def test_scan_one_image_file_says_the_switch_is_why_it_was_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "img.png"
            image.write_bytes(_png_bytes(64, 64))

            result = scan_pdf_sources(image, include_images=False)

            self.assertEqual(result.items, [])
            self.assertEqual(len(result.skipped), 1)
            self.assertEqual(result.skipped[0].kind, SKIP_KIND_IMAGES_DISABLED)
            self.assertIn("允许独立图片", result.skipped[0].reason)

    def test_read_pdf_oversized_page_count_is_none_not_zero_when_unreadable(self) -> None:
        # 「读不出来」和「确认没有大幅面页」必须是两个不同的值：前者是 None，
        # 不能悄悄退化成 0，否则界面会把「无法判断」误显示成「零大幅面页」。
        with patch(
            "core.pdf_image_translation._open_pdf_document",
            side_effect=RuntimeError("corrupt pdf"),
        ):
            self.assertIsNone(_read_pdf_oversized_page_count(Path("/nonexistent/does-not-matter.pdf")))


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


class _ReviewRequestErrorClient:
    def __init__(self) -> None:
        self.calls = 0

    def review_page(self, **_kwargs):
        self.calls += 1
        raise TimeoutError("review timeout")


class _FakeImageClient:
    def __init__(self, image_bytes: bytes):
        self.image_bytes = image_bytes

    def generate_page(self, **_kwargs):
        return self.image_bytes


class _StopAfterAcquireScheduler(WeightedApiScheduler):
    def __init__(self, runner: PdfImageTranslationRunner) -> None:
        super().__init__(1)
        self.runner = runner
        self.release_count = 0

    def acquire_lease(self, *args, **kwargs):
        lease = super().acquire_lease(*args, **kwargs)
        self.runner.stop()
        return lease

    def release(self, *args, **kwargs) -> None:
        self.release_count += 1
        super().release(*args, **kwargs)


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


def _page_review_settings(root: Path) -> AppSettings:
    """Minimal single-model PDF settings writing into a temporary output root."""
    settings = AppSettings(target_lang="en")
    settings.output.use_custom_output_dir = True
    settings.output.custom_output_dir = str(root / "out")
    settings.pdf.page_generation_concurrency = 1
    settings.image_model_role.source_role = SOURCE_INDEPENDENT
    settings.image_model_role.cloud_provider = "custom_openai"
    settings.image_model_role.cloud_model = "image-model"
    settings.image_model_role.cloud_base_url = "https://images.example/v1"
    return settings


class _PauseAwarePdfRunner(PdfImageTranslationRunner):
    """Signals the exact moment the run parks inside its pause loop."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.pause_wait_entered = threading.Event()

    def _wait_while_paused(self) -> bool:
        self.pause_wait_entered.set()
        return super()._wait_while_paused()


class _PauseOnCallImageClient(_FakeImageClient):
    """Pauses the runner from inside a page unit, optionally failing that page."""

    def __init__(
        self,
        image_bytes: bytes,
        *,
        pause_calls: set[int],
        fail_calls: set[int] | None = None,
        payloads: dict[int, bytes] | None = None,
    ) -> None:
        super().__init__(image_bytes)
        self.calls = 0
        self.runner: PdfImageTranslationRunner | None = None
        self.pause_calls = set(pause_calls)
        self.fail_calls = set(fail_calls or ())
        self.payloads = dict(payloads or {})

    def generate_page(self, **_kwargs):
        self.calls += 1
        call = self.calls
        if call in self.pause_calls and self.runner is not None:
            self.runner.pause()
        if call in self.fail_calls:
            raise RuntimeError("page generation failed")
        return self.payloads.get(call, self.image_bytes)


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


def _fake_pdfium_module(page_counts: dict[str, int] | None = None):
    resolved_page_counts = page_counts or {}

    class FakeBitmap:
        def to_pil(self):
            return Image.new("RGB", (1200, 1600), "white")

        def close(self) -> None:
            return

    class FakePage:
        def get_size(self) -> tuple[float, float]:
            return (288.0, 384.0)

        def render(self, **_kwargs):
            return FakeBitmap()

        def insert_obj(self, _image_object) -> None:
            return

        def gen_content(self) -> None:
            return

        def close(self) -> None:
            return

    class FakeInputDocument:
        def __init__(self, path: str):
            self.page_count = resolved_page_counts.get(Path(path).name, 1)

        def __len__(self) -> int:
            return self.page_count

        def get_page(self, _page_index: int) -> FakePage:
            return FakePage()

        def close(self) -> None:
            return

    class FakeOutputDocument:
        def new_page(self, _width: float, _height: float) -> FakePage:
            return FakePage()

        def save(self, path: Path) -> None:
            Path(path).write_bytes(b"%PDF-1.4\n% fake translated pdf\n")

        def close(self) -> None:
            return

    class FakePdfDocument:
        def __new__(cls, path: str):
            return FakeInputDocument(path)

        @staticmethod
        def new() -> FakeOutputDocument:
            return FakeOutputDocument()

    class FakePdfImage:
        @staticmethod
        def new(_document):
            return types.SimpleNamespace(
                load_jpeg=lambda *_args, **_kwargs: None,
                set_bitmap=lambda *_args, **_kwargs: None,
                set_matrix=lambda *_args, **_kwargs: None,
                close=lambda: None,
            )

    class FakePdfBitmap:
        @staticmethod
        def from_pil(_image) -> FakeBitmap:
            return FakeBitmap()

    class FakePdfMatrix:
        def scale(self, _width: float, _height: float):
            return self

    return types.SimpleNamespace(
        PdfDocument=FakePdfDocument,
        PdfImage=FakePdfImage,
        PdfBitmap=FakePdfBitmap,
        PdfMatrix=FakePdfMatrix,
    )


def _fake_pdfium_module_by_page_count(page_counts: dict[str, int]):
    return _fake_pdfium_module(page_counts)


# --------------------------------------------------------------------------
# 以下为 2026-08-07 代码审计（docs/CODE_AUDIT_2026-08-07.md 第 1.4/1.5/2.4/3.12/
# 4.13/4.14/4.15 节）确认缺陷的回归测试。所有图像模型调用都用假客户端，
# 任何一条用例都不会真的打到收费 API。
# --------------------------------------------------------------------------

# 300 DPI 下的实测页面像素尺寸（审计里量出 1% 容差必然失败的那几种幅面）。
_PAGE_PIXEL_SIZES: dict[str, tuple[int, int]] = {
    "A4": (2480, 3508),
    "Letter": (2550, 3300),
    "Legal": (2550, 4200),
    "A3": (3508, 4961),
    # 窄长页：gpt-image-2 的 16px 量化在 0.60 附近的比例误差超过 1%。
    "Narrow": (1200, 2000),
}


def _inked_png_bytes(width: int, height: int, *, ink_fraction: float = 0.25) -> bytes:
    """白底 + 一块黑色区域的 PNG：`_measure_page_ink` 会把它判成「有内容」。"""
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    ink_height = max(1, int(height * ink_fraction))
    draw.rectangle([0, 0, width - 1, ink_height - 1], fill="black")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _find_real_font_file() -> Path | None:
    for candidate in (
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    ):
        path = Path(candidate)
        if path.exists():
            return path
    return None


class _SizedImageClient:
    """按调用方要求的尺寸返回一张有内容的图，并记录调用次数。"""

    def __init__(self, width: int, height: int) -> None:
        self.image_bytes = _inked_png_bytes(width, height)
        self.calls = 0

    def generate_page(self, **_kwargs):
        self.calls += 1
        return self.image_bytes


class _BrokenPageRunner(PdfImageTranslationRunner):
    """让指定页的渲染抛异常，模拟 PDF 里单页对象损坏。"""

    broken_page_index = 1

    def _render_source_page(self, doc, *, page_index: int, **kwargs):
        if page_index == self.broken_page_index:
            raise RuntimeError("page object is corrupt")
        return super()._render_source_page(doc, page_index=page_index, **kwargs)


class PdfDefectRegressionTests(unittest.TestCase):
    """审计确认缺陷的回归测试（每条对应清单里的一项）。"""

    def _pdf_settings(self, root: Path, *, model: str = "image-model") -> AppSettings:
        settings = AppSettings(target_lang="en")
        settings.pdf.target_lang = "en"
        settings.output.use_custom_output_dir = True
        settings.output.custom_output_dir = str(root / "out")
        settings.pdf.page_generation_concurrency = 1
        settings.image_model_role.source_role = SOURCE_INDEPENDENT
        settings.image_model_role.cloud_provider = "custom_openai"
        settings.image_model_role.cloud_model = model
        settings.image_model_role.cloud_base_url = "https://images.example/v1"
        return settings

    # ---------------- 问题 1：质检只比宽高比 ----------------

    def test_blank_output_against_inked_source_is_flagged_but_still_shipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.png"
            source.write_bytes(_inked_png_bytes(1200, 1600))

            quality = check_page_quality(
                _png_bytes(1200, 1600),
                source_width=1200,
                source_height=1600,
                source_image_path=source,
            )

            # 关键契约：疑似空白页依然 ok=True——它照常入册、不消耗重试、
            # 不会被丢掉，只是把疑点挂到页面上让文件转 needs_review。
            self.assertTrue(quality.ok)
            self.assertEqual(quality.status, "ok")
            self.assertIn("blank_page", quality.review_reasons)
            self.assertIn("疑似空白页", quality.review_message)

    def test_blank_output_against_blank_source_is_not_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.png"
            source.write_bytes(_png_bytes(1200, 1600))

            quality = check_page_quality(
                _png_bytes(1200, 1600),
                source_width=1200,
                source_height=1600,
                source_image_path=source,
            )

            # 源页本来就是空白页，输出空白是正确结果，绝不能报警。
            self.assertTrue(quality.ok)
            self.assertEqual(quality.review_reasons, [])

    def test_blank_output_without_readable_source_is_not_flagged(self) -> None:
        # 源页读不出来 = 不确定，按「不报警」处理，宁可漏判也不误杀。
        quality = check_page_quality(
            _png_bytes(1200, 1600),
            source_width=1200,
            source_height=1600,
            source_image_path=None,
        )
        self.assertTrue(quality.ok)
        self.assertEqual(quality.review_reasons, [])

        missing = check_page_quality(
            _png_bytes(1200, 1600),
            source_width=1200,
            source_height=1600,
            source_image_path=Path("/nonexistent/source.png"),
        )
        self.assertTrue(missing.ok)
        self.assertEqual(missing.review_reasons, [])

    def test_low_resolution_output_is_flagged_but_fixed_size_model_output_is_not(self) -> None:
        tiny = check_page_quality(
            _inked_png_bytes(600, 800),
            source_width=2480,
            source_height=3307,
        )
        self.assertTrue(tiny.ok)
        self.assertIn("low_resolution", tiny.review_reasons)

        # 分辨率下限刻意压在 700×1000：gpt-image-1 的固定 1024×1536 必须干净
        # 通过，否则每一页都会被标复核，标记就没有信息量了。
        fixed_size = check_page_quality(
            _inked_png_bytes(1024, 1536),
            source_width=2048,
            source_height=3072,
        )
        self.assertTrue(fixed_size.ok)
        self.assertEqual(fixed_size.review_reasons, [])

    def test_decode_and_ratio_failures_still_fail_and_do_not_leak_flags(self) -> None:
        decode = check_page_quality(b"not an image", source_width=1600, source_height=1200)
        self.assertFalse(decode.ok)
        self.assertEqual(decode.status, "decode_error")
        self.assertEqual(decode.review_reasons, [])

        ratio = check_page_quality(
            _inked_png_bytes(1600, 1000),
            source_width=1600,
            source_height=1200,
        )
        self.assertFalse(ratio.ok)
        self.assertEqual(ratio.status, "ratio_error")

    def test_quality_flags_reach_task_status_and_file_result(self) -> None:
        record = PdfFileRecord(
            name="doc",
            source_path="/tmp/doc.pdf",
            relative_path="doc.pdf",
            status=PDF_OUTPUT_STATE_COMPLETED,
            page_count=3,
            translated_pdf_path="/tmp/out/doc_en.pdf",
            quality_flagged_page_count=1,
        )

        self.assertEqual(
            determine_pdf_task_status(stopped=False, file_records=[record]),
            PDF_OUTPUT_STATE_NEEDS_REVIEW,
        )
        result = _file_record_to_result(record)
        # 质检疑点不否定产物：文件仍然成功，但必须在结果里显式说出来。
        self.assertTrue(result["success"])
        self.assertEqual(result["quality_flagged_page_count"], 1)
        self.assertIn("内容质检待复核", result["detail"])

        issues = _summary_issues([record])
        self.assertEqual(issues, [])  # 页级疑点靠页记录汇报，这里没有页记录

    def test_quality_flagged_page_appears_in_summary_issues(self) -> None:
        record = PdfFileRecord(
            name="doc",
            source_path="/tmp/doc.pdf",
            relative_path="doc.pdf",
            status=PDF_OUTPUT_STATE_NEEDS_REVIEW,
            page_count=1,
            translated_pdf_path="/tmp/out/doc_en.pdf",
            quality_flagged_page_count=1,
            pages=[
                PdfPageRecord(
                    page_number=1,
                    source_image_path="/tmp/src/page_001.png",
                    file_name="doc",
                    status="success",
                    quality_flags=["blank_page"],
                    quality_message="译图非背景像素仅 0.000%，源页为 20.00%，疑似空白页或内容丢失",
                )
            ],
        )

        issues = _summary_issues([record])
        self.assertEqual(len(issues), 1)
        self.assertIn("疑似空白页", issues[0]["problem"])
        self.assertIn("内容质检待复核", issues[0]["status"])

    # ---------------- 问题 2：固定尺寸模型 × 1% 容差 = 必然失败 ----------------

    def test_fixed_size_models_get_a_tolerance_they_can_actually_meet(self) -> None:
        for model in ("gpt-image-1", GPT_IMAGE_2_MODEL):
            for label, (width, height) in _PAGE_PIXEL_SIZES.items():
                with self.subTest(model=model, page=label):
                    size = pdf_page_request_size(width, height, model)
                    self.assertIsNotNone(size)
                    tolerance = pdf_page_ratio_tolerance(
                        source_width=width,
                        source_height=height,
                        model=model,
                    )
                    source_ratio = width / height
                    requested_ratio = size[0] / size[1]
                    unavoidable = abs(source_ratio - requested_ratio) / source_ratio
                    # 我们自己请求的尺寸，必须能通过我们自己的那道门。
                    self.assertLessEqual(unavoidable, tolerance)
                    # 放宽之后仍然保留原本 1% 的质量余量，不是把门拆了。
                    self.assertAlmostEqual(tolerance - unavoidable, 0.01, places=9)

    def test_free_form_size_models_keep_the_strict_base_tolerance(self) -> None:
        # 不协商 size 的模型（/responses 路线）可以精确还原比例，容差不放宽。
        self.assertIsNone(pdf_page_request_size(2480, 3508, "gemini-2.5-flash-image"))
        self.assertAlmostEqual(
            pdf_page_ratio_tolerance(
                source_width=2480,
                source_height=3508,
                model="gemini-2.5-flash-image",
            ),
            0.01,
            places=9,
        )

    def test_fixed_size_model_pages_succeed_on_the_first_attempt(self) -> None:
        """问题 2 的核心断言：一次成功，不烧重试预算（每次重试都是真金白银）。"""
        for model in ("gpt-image-1", GPT_IMAGE_2_MODEL):
            for label, (width, height) in _PAGE_PIXEL_SIZES.items():
                with self.subTest(model=model, page=label):
                    with tempfile.TemporaryDirectory() as tmp:
                        root = Path(tmp)
                        requested = pdf_page_request_size(width, height, model)
                        self.assertIsNotNone(requested)
                        image_client = _SizedImageClient(*requested)
                        settings = self._pdf_settings(root, model=model)
                        runner = PdfImageTranslationRunner(
                            [],
                            settings,
                            source_root=root,
                            image_client=image_client,
                            task_logger_enabled=False,
                        )

                        source_path = root / "page_001.png"
                        source_path.write_bytes(_inked_png_bytes(width, height))
                        page_record = PdfPageRecord(
                            page_number=1,
                            source_image_path=str(source_path),
                            file_name="doc",
                            status="rendered",
                            source_width_px=width,
                            source_height_px=height,
                            page_width_pt=width * 72 / 300,
                            page_height_pt=height * 72 / 300,
                        )

                        with patch("core.model_roles.get_key", return_value="secret"):
                            model_config = resolve_effective_model_config(settings, ROLE_IMAGE)
                            runner._generate_page_with_retries(
                                page_record,
                                1,
                                root / "translated",
                                root / "candidates",
                                3,
                                WeightedApiScheduler(1),
                                WeightedApiScheduler(1),
                                model_config,
                                None,
                            )

                        self.assertEqual(page_record.status, "success")
                        self.assertEqual(page_record.attempts, 1)
                        self.assertEqual(image_client.calls, 1)
                        self.assertFalse(page_record.placeholder)
                        # 补边是这类模型的已知代价，不是「应急归一化」，
                        # 也不该把页面标成待复核。
                        self.assertFalse(page_record.emergency_ratio_normalized)
                        self.assertEqual(page_record.quality_flags, [])
                        self.assertTrue(Path(page_record.translated_image_path).exists())

                        source_ratio = width / height
                        requested_ratio = requested[0] / requested[1]
                        expect_letterbox = abs(source_ratio - requested_ratio) / source_ratio > 0.02
                        self.assertEqual(page_record.ratio_letterboxed, expect_letterbox)
                        if expect_letterbox:
                            # 补白边之后，入册尺寸回到源页比例，装配阶段不会拉伸。
                            self.assertAlmostEqual(
                                page_record.output_width_px / page_record.output_height_px,
                                source_ratio,
                                places=2,
                            )

    def test_letterboxing_alone_does_not_mark_a_file_needs_review(self) -> None:
        record = PdfFileRecord(
            name="doc",
            source_path="/tmp/doc.pdf",
            relative_path="doc.pdf",
            status=PDF_OUTPUT_STATE_COMPLETED,
            page_count=2,
            translated_pdf_path="/tmp/out/doc_en.pdf",
            ratio_letterboxed_page_count=2,
        )
        self.assertEqual(
            determine_pdf_task_status(stopped=False, file_records=[record]),
            PDF_OUTPUT_STATE_COMPLETED,
        )
        self.assertTrue(_file_record_to_result(record)["success"])

    # ---------------- 问题 3：「页图并发」设置从未被读取 ----------------

    def test_explicit_page_concurrency_setting_beats_the_auto_recommendation(self) -> None:
        settings = AppSettings(target_lang="en")
        settings.pdf.page_generation_concurrency = 5
        runner = PdfImageTranslationRunner([], settings, task_logger_enabled=False)
        self.assertEqual(runner._resolve_pdf_concurrency(2), 5)

        settings.pdf.page_generation_concurrency = None
        self.assertEqual(runner._resolve_pdf_concurrency(7), 7)
        self.assertEqual(runner._resolve_pdf_concurrency(None), 2)

        settings.pdf.page_generation_concurrency = 999
        self.assertEqual(runner._resolve_pdf_concurrency(2), 20)
        settings.pdf.page_generation_concurrency = 0
        self.assertEqual(runner._resolve_pdf_concurrency(2), 1)

    def test_run_honours_the_user_page_concurrency_over_model_throughput(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            names = ["a", "b", "c", "d"]
            for name in names:
                (root / f"{name}.pdf").write_bytes(b"%PDF-1.4\n")
            settings = self._pdf_settings(root)
            settings.pdf.page_generation_concurrency = 3
            image_client = _ConcurrentImageClient(_png_bytes(1200, 1600), sleep_seconds=0.05)
            runner = PdfImageTranslationRunner(
                [
                    PdfFileItem(path=root / f"{name}.pdf", name=name, size_kb=1.0, page_count=1)
                    for name in names
                ],
                settings,
                source_root=root,
                image_client=image_client,
                task_logger_enabled=False,
            )

            with patch.dict(
                sys.modules,
                {"pypdfium2": _fake_pdfium_module_by_page_count({f"{n}.pdf": 1 for n in names})},
            ), patch("core.model_roles.get_key", return_value="secret"), patch(
                "core.pdf_image_translation.get_model_throughput",
                return_value=EffectiveModelThroughput(
                    profile_key="test", batch_size=None, concurrency=1
                ),
            ):
                runner._run()

            messages = _drain_all_messages(runner)
            done = [item for item in messages if isinstance(item, DoneMsg)]
            self.assertTrue(done)
            # 吞吐档案建议 1，用户设置 3——跑起来必须是 3。
            self.assertEqual(image_client.max_active, 3)
            logs = [item.message for item in messages if isinstance(item, LogMsg)]
            self.assertTrue(any("页图并发：3（用户在设置中指定）" in line for line in logs))

    # ---------------- 问题 4：一页坏掉拖垮整份文件 ----------------

    def test_one_broken_page_becomes_a_placeholder_and_the_rest_still_assemble(self) -> None:
        import pypdfium2 as pdfium

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_pdf = root / "source.pdf"
            _write_multi_page_pdf(
                source_pdf,
                [{"media_box": (0, 0, _A4_W_PT, _A4_H_PT)} for _ in range(3)],
            )
            settings = self._pdf_settings(root)
            image_client = _RecordingImageClient(_png_bytes(2481, 3508))
            runner = _BrokenPageRunner(
                [PdfFileItem(path=source_pdf, name="source", size_kb=1.0, page_count=3)],
                settings,
                source_root=root,
                image_client=image_client,
                task_logger_enabled=False,
            )

            with patch("core.model_roles.get_key", return_value="secret"):
                prepared = runner._prepare_pdf_files(output_dir=root / "out", app_managed=True)[0]
                runner._total_page_count = 3
                runner._process_prepared_pages(
                    [prepared],
                    max_attempts=max_page_generation_attempts(0),
                    scheduler=WeightedApiScheduler(1),
                    review_scheduler=WeightedApiScheduler(1),
                    model_config=resolve_effective_model_config(settings, ROLE_IMAGE),
                    review_model_config=None,
                    concurrency=1,
                    total_pages=3,
                )
                runner._finalize_file_record(prepared, should_assemble=True)

            record = prepared.record
            # 坏页从来没送进模型，好页一个都不能少。
            self.assertEqual(len(image_client.calls), 2)
            self.assertNotEqual(record.status, PDF_OUTPUT_STATE_FAILED)
            self.assertEqual(record.status, PDF_OUTPUT_STATE_NEEDS_REVIEW)
            self.assertEqual(record.page_count, 3)
            self.assertEqual(record.placeholder_page_count, 1)

            pages = sorted(record.pages, key=lambda item: item.page_number)
            self.assertEqual([page.page_number for page in pages], [1, 2, 3])
            self.assertEqual(pages[0].status, "success")
            self.assertEqual(pages[1].status, "placeholder")
            self.assertTrue(pages[1].placeholder)
            self.assertIn("页面读取/渲染失败", pages[1].error)
            self.assertEqual(pages[2].status, "success")

            # 页序和页数都不能因为坏页而错位。
            output = Path(record.translated_pdf_path)
            self.assertTrue(output.exists())
            document = pdfium.PdfDocument(output)
            try:
                self.assertEqual(len(document), 3)
            finally:
                document.close()

            issues = _summary_issues([record])
            self.assertTrue(any(issue.get("location_label") == "第 2 页" for issue in issues))

    def test_broken_page_record_falls_back_to_a_plausible_page_size(self) -> None:
        class _NoSizeDoc:
            def get_page(self, _index):
                raise RuntimeError("page object is corrupt")

        runner = PdfImageTranslationRunner([], AppSettings(target_lang="en"), task_logger_enabled=False)
        page_record = runner._broken_page_record(
            _NoSizeDoc(),
            page_index=4,
            file_name="doc",
            error=RuntimeError("boom"),
        )

        self.assertEqual(page_record.page_number, 5)
        self.assertEqual(page_record.status, "placeholder_pending")
        self.assertTrue(page_record.placeholder)
        # 几何必须是可用的，否则装配阶段会画出一张 0×0 的页。
        self.assertGreater(page_record.page_width_pt, 0)
        self.assertGreater(page_record.page_height_pt, 0)
        self.assertGreater(page_record.source_width_px, 0)
        self.assertGreater(page_record.source_height_px, 0)

    # ---------------- 问题 5：渲染无像素上限 ----------------

    def test_render_scale_is_untouched_for_normal_pages_and_capped_for_huge_ones(self) -> None:
        scale, dpi = _pdf_render_scale_for_page(_A4_W_PT, _A4_H_PT)
        self.assertAlmostEqual(scale, PDF_RENDER_DPI_DEFAULT / 72.0, places=9)
        self.assertAlmostEqual(dpi, float(PDF_RENDER_DPI_DEFAULT), places=9)

        # A3 也在上限之内：常见工程幅面不许被降级。
        _, a3_dpi = _pdf_render_scale_for_page(_A3_W_PT, _A3_H_PT)
        self.assertAlmostEqual(a3_dpi, float(PDF_RENDER_DPI_DEFAULT), places=9)

        # A0 图纸：300 DPI 下约 1.4 亿像素，必须降档而不是拒绝。
        a0_w, a0_h = 2383.94, 3370.39
        a0_scale, a0_dpi = _pdf_render_scale_for_page(a0_w, a0_h)
        self.assertLess(a0_dpi, PDF_RENDER_DPI_DEFAULT)
        self.assertGreaterEqual(a0_dpi, PDF_PAGE_MIN_RENDER_DPI)
        self.assertLessEqual(
            (a0_w * a0_scale) * (a0_h * a0_scale),
            PDF_PAGE_MAX_RENDER_PIXELS * 1.001,
        )

        # 上限压到荒谬的小值时，DPI 停在 72 的地板上，不会掉到 0。
        floor_scale, floor_dpi = _pdf_render_scale_for_page(a0_w, a0_h, max_pixels=1000)
        self.assertAlmostEqual(floor_dpi, PDF_PAGE_MIN_RENDER_DPI, places=9)
        self.assertGreater(floor_scale, 0)

    def test_downscaled_render_is_recorded_on_the_page_and_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_pdf = root / "big.pdf"
            _write_multi_page_pdf(source_pdf, [{"media_box": (0, 0, _A4_W_PT, _A4_H_PT)}])
            settings = self._pdf_settings(root)
            runner = PdfImageTranslationRunner(
                [PdfFileItem(path=source_pdf, name="big", size_kb=1.0, page_count=1)],
                settings,
                source_root=root,
                image_client=_FakeImageClient(_png_bytes(1200, 1600)),
                task_logger_enabled=False,
            )

            source_pages_dir = root / "src"
            with patch("core.pdf_image_translation.PDF_PAGE_MAX_RENDER_PIXELS", 1_000_000):
                document = _open_pdf_document(source_pdf)
                try:
                    page_record = runner._render_source_page(
                        document,
                        page_index=0,
                        page_count=1,
                        source_pages_dir=source_pages_dir,
                        file_name="big",
                    )
                finally:
                    document.close()

            self.assertLess(page_record.render_dpi, PDF_RENDER_DPI_DEFAULT)
            self.assertGreaterEqual(page_record.render_dpi, PDF_PAGE_MIN_RENDER_DPI)
            self.assertLessEqual(
                page_record.source_width_px * page_record.source_height_px,
                1_010_000,
            )
            logs = [item.message for item in _drain_messages(runner, LogMsg)]
            self.assertTrue(any("渲染 DPI 已从 300 自动降到" in line for line in logs))

            record = PdfFileRecord(
                name="big",
                source_path=str(source_pdf),
                relative_path="big.pdf",
                page_count=1,
                pages=[page_record],
            )
            runner._refresh_file_record_counts(record)
            self.assertEqual(record.render_downscaled_page_count, 1)
            self.assertEqual(_file_record_to_result(record)["render_downscaled_page_count"], 1)

    # ---------------- 问题 6：Windows 占位页中文渲染成方框 ----------------

    def test_placeholder_font_prefers_windows_cjk_fonts_when_present(self) -> None:
        real_font = _find_real_font_file()
        if real_font is None:
            self.skipTest("本机没有可复制的真实字体文件")

        with tempfile.TemporaryDirectory() as tmp:
            windir = Path(tmp) / "Windows"
            fonts_dir = windir / "Fonts"
            fonts_dir.mkdir(parents=True)
            font_bytes = real_font.read_bytes()
            (fonts_dir / "simhei.ttf").write_bytes(font_bytes)

            with patch.dict(os.environ, {"WINDIR": str(windir)}):
                font = _load_placeholder_font(24)

            # Windows 候选必须排在 macOS/Linux 候选前面，否则在 Windows 上
            # 整页中文说明会掉到 load_default() 变成方框。
            self.assertEqual(Path(getattr(font, "path", "")), fonts_dir / "simhei.ttf")

            (fonts_dir / "msyh.ttc").write_bytes(font_bytes)
            (fonts_dir / "msyhbd.ttc").write_bytes(font_bytes)
            with patch.dict(os.environ, {"WINDIR": str(windir)}):
                regular = _load_placeholder_font(24)
                bold = _load_placeholder_font(24, bold=True)
            self.assertEqual(Path(getattr(regular, "path", "")).name, "msyh.ttc")
            self.assertEqual(Path(getattr(bold, "path", "")).name, "msyhbd.ttc")

    def test_placeholder_font_still_resolves_when_windows_fonts_are_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"WINDIR": str(Path(tmp) / "nowhere")}):
                self.assertIsNotNone(_load_placeholder_font(24))

    # ---------------- 问题 7：全部页面被跳过仍报「翻译完成」 ----------------

    def test_all_pages_skipped_oversize_is_not_reported_as_a_success(self) -> None:
        import pypdfium2 as pdfium

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_pdf = root / "drawings.pdf"
            _write_multi_page_pdf(
                source_pdf,
                [
                    {"media_box": (0, 0, _A3_W_PT, _A3_H_PT)},
                    {"media_box": (0, 0, _A3_H_PT, _A3_W_PT)},
                ],
            )
            settings = self._pdf_settings(root)
            settings.pdf.skip_oversized_pages = True
            image_client = _RecordingImageClient(_png_bytes(2481, 3508))
            runner = PdfImageTranslationRunner(
                [PdfFileItem(path=source_pdf, name="drawings", size_kb=1.0, page_count=2)],
                settings,
                source_root=root,
                image_client=image_client,
                task_logger_enabled=False,
            )

            with patch("core.model_roles.get_key", return_value="secret"):
                prepared = runner._prepare_pdf_files(output_dir=root / "out", app_managed=True)[0]
                runner._total_page_count = 2
                runner._process_prepared_pages(
                    [prepared],
                    max_attempts=max_page_generation_attempts(0),
                    scheduler=WeightedApiScheduler(1),
                    review_scheduler=WeightedApiScheduler(1),
                    model_config=resolve_effective_model_config(settings, ROLE_IMAGE),
                    review_model_config=None,
                    concurrency=1,
                    total_pages=2,
                )
                runner._finalize_file_record(prepared, should_assemble=True)

            record = prepared.record
            self.assertEqual(len(image_client.calls), 0)
            self.assertEqual(record.skipped_oversize_page_count, 2)
            self.assertEqual(record.status, PDF_OUTPUT_STATE_NEEDS_REVIEW)
            self.assertEqual(record.error, fully_skipped_oversize_message(record))
            self.assertEqual(
                determine_pdf_task_status(stopped=False, file_records=[record]),
                PDF_OUTPUT_STATE_NEEDS_REVIEW,
            )

            result = _file_record_to_result(record)
            # 产物只是源文件的逐页副本，一个字都没翻——不能算成功。
            self.assertFalse(result["success"])
            self.assertTrue(result["all_pages_skipped_oversize"])
            self.assertIn("全部页面因幅面过大被跳过，未翻译", result["detail"])
            self.assertIn("未产生任何翻译内容", result["error"])

            issues = _summary_issues([record])
            self.assertTrue(
                any(issue.get("location_label") == "整份文件" for issue in issues),
                issues,
            )

            # 已验证健康的行为不能被这次改动带偏：页序和矢量直传照旧。
            output = Path(record.translated_pdf_path)
            document = pdfium.PdfDocument(output)
            try:
                self.assertEqual(len(document), 2)
                first = document.get_page(0)
                try:
                    self.assertEqual(len(list(first.get_objects())), 0)
                    width, height = first.get_size()
                    self.assertAlmostEqual(width, _A3_W_PT, places=1)
                    self.assertAlmostEqual(height, _A3_H_PT, places=1)
                finally:
                    first.close()
                second = document.get_page(1)
                try:
                    width, height = second.get_size()
                    self.assertAlmostEqual(width, _A3_H_PT, places=1)
                    self.assertAlmostEqual(height, _A3_W_PT, places=1)
                finally:
                    second.close()
            finally:
                document.close()

    def test_partially_skipped_file_still_counts_as_a_success(self) -> None:
        # 只跳过一部分页面时行为不变：这是「翻译了一些内容」，仍算成功。
        record = PdfFileRecord(
            name="mixed",
            source_path="/tmp/mixed.pdf",
            relative_path="mixed.pdf",
            status=PDF_OUTPUT_STATE_COMPLETED,
            page_count=3,
            skipped_oversize_page_count=1,
            translated_pdf_path="/tmp/out/mixed_en.pdf",
        )
        result = _file_record_to_result(record)
        self.assertTrue(result["success"])
        self.assertFalse(result["all_pages_skipped_oversize"])
        self.assertEqual(
            determine_pdf_task_status(stopped=False, file_records=[record]),
            PDF_OUTPUT_STATE_COMPLETED,
        )

    def test_suspect_adopted_pages_never_double_count_placeholder_pages(self) -> None:
        # 完成小结要把「没生成译文的页」和「有疑点但仍然采用的页」相加，这两个数必须互斥。
        # 审核没通过的页有一半是直接退回占位页的，质检标记也留在退回前的最后一次尝试上，
        # 拿 placeholder + review_failed + quality_flagged 相加会把一页坏页说成三页。
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_pdf = root / "mixed.pdf"
            _write_multi_page_pdf(source_pdf, [{"media_box": (0, 0, _A4_W_PT, _A4_H_PT)}])
            settings = self._pdf_settings(root)
            settings.pdf.review_enabled = True
            runner = PdfImageTranslationRunner(
                [PdfFileItem(path=source_pdf, name="mixed", size_kb=1.0, page_count=3)],
                settings,
                source_root=root,
                image_client=_FakeImageClient(_png_bytes(600, 800)),
                task_logger_enabled=False,
            )
            pages = [
                # 审核没通过、彻底放弃 → 退回占位页；质检标记还留在最后一次尝试上。
                PdfPageRecord(
                    page_number=1,
                    source_image_path="",
                    status="placeholder_pending",
                    placeholder=True,
                    review_status="failed",
                    quality_flags=["near_blank"],
                ),
                # 审核请求本身出错 → 译图照样采用，这一页才是「有疑点但仍然采用」。
                PdfPageRecord(
                    page_number=2,
                    source_image_path="",
                    status="success",
                    review_status="failed",
                ),
                PdfPageRecord(
                    page_number=3,
                    source_image_path="",
                    status="success",
                    review_status="passed",
                ),
            ]
            record = PdfFileRecord(
                name="mixed",
                source_path=str(source_pdf),
                relative_path="mixed.pdf",
                page_count=3,
                pages=pages,
            )
            runner._refresh_file_record_counts(record)
            self.assertEqual(record.placeholder_page_count, 1)
            self.assertEqual(record.review_failed_page_count, 2)
            self.assertEqual(record.quality_flagged_page_count, 1)
            self.assertEqual(record.suspect_adopted_page_count, 1)

            result = _file_record_to_result(record)
            self.assertEqual(result["suspect_adopted_page_count"], 1)
            # 界面相加的就是这两个数：3 页里 2 页有问题，不是 4 页。
            self.assertEqual(
                result["placeholder_page_count"] + result["suspect_adopted_page_count"],
                2,
            )

            # 独立图片的失败分支把 status 置成 failed、placeholder 置成 False（没有占位图
            # 这回事），这一页连输出都没有，不许算进「有疑点仍采用」——否则任务中心的逐文件
            # 表写「未生成」，同一屏的指标格橙着一句「有疑点仍采用 1」。
            record.pages.append(
                PdfPageRecord(
                    page_number=4,
                    source_image_path="",
                    status=PDF_OUTPUT_STATE_FAILED,
                    placeholder=False,
                    review_status="failed",
                    quality_flags=["near_blank"],
                )
            )
            record.page_count = 4
            runner._refresh_file_record_counts(record)
            self.assertEqual(record.suspect_adopted_page_count, 1)
            self.assertEqual(record.review_failed_page_count, 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
