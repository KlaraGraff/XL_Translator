"""PDF image-layout translation pipeline and output package helpers."""

from __future__ import annotations

import json
import queue
import re
import shutil
import tempfile
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any

from loguru import logger
from PIL import Image, ImageDraw, ImageFont

from config import (
    CONCURRENCY_DEFAULT,
    PDF_ASPECT_RATIO_TOLERANCE,
    PDF_COMPRESSED_JPEG_QUALITY_DEFAULT,
    PDF_COMPRESSED_MAX_LONG_EDGE_PX,
    PDF_MIN_READABLE_LONG_EDGE_PX,
    PDF_MIN_READABLE_SHORT_EDGE_PX,
    PDF_PAGE_CONCURRENCY_SAFETY_CAP,
    PDF_PAGE_RENDER_AHEAD_COUNT,
    PDF_PAGE_RETRY_ATTEMPTS_DEFAULT,
    PDF_RENDER_DPI_DEFAULT,
)
from core import bilingual_writer
from core.api_concurrency_control import handle_api_concurrency_limit
from core.api_scheduler import WeightedApiScheduler
from core.image_generation import (
    ImageGenerationClient,
    ImageModelUnavailableError,
    OpenAICompatibleImageGenerationClient,
    is_model_unavailable_error,
)
from core.language_registry import get_target_lang_display
from core.model_roles import (
    ROLE_IMAGE,
    ROLE_PDF_REVIEW,
    image_model_signature,
    pdf_review_model_signature,
    provider_supports_capability,
    record_image_model_availability,
    record_pdf_review_model_availability,
    resolve_effective_model_config,
)
from core.pdf_review import (
    OpenAICompatiblePdfReviewClient,
    PdfPageReviewClient,
    PdfReviewIssue,
    PdfPageReviewResult,
    PdfReviewModelUnavailableError,
)
from core.task_logger import TaskLogger
from core.task_runner import (
    DoneMsg,
    ErrorMsg,
    LogMsg,
    PdfReviewStatusMsg,
    ProgressMsg,
    StoppedMsg,
)
from settings import AppSettings


SUPPORTED_PDF_SUFFIXES = {".pdf"}
PDF_PAGES_ROOT = "_pdf_pages"
SOURCE_PAGES_DIRNAME = "source_pages"
TRANSLATED_PAGES_DIRNAME = "translated_pages"
REVIEW_CANDIDATES_DIRNAME = "review_candidates"
PDF_REPORT_FILENAME = "pdf_translation_report.md"
PDF_MANIFEST_FILENAME = "pdf_translation_manifest.json"
PDF_OUTPUT_STATE_COMPLETED = "completed"
PDF_OUTPUT_STATE_NEEDS_REVIEW = "needs_review"
PDF_OUTPUT_STATE_STOPPED = "stopped"
PDF_OUTPUT_STATE_FAILED = "failed"

_INVALID_FILENAME_FRAGMENT_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


@dataclass
class PdfFileItem:
    path: Path
    name: str
    size_kb: float
    page_count: int = 0


@dataclass
class PageQualityResult:
    ok: bool
    status: str
    message: str = ""
    width: int = 0
    height: int = 0


@dataclass
class PdfPageRecord:
    page_number: int
    source_image_path: str
    translated_image_path: str = ""
    status: str = "pending"
    attempts: int = 0
    error: str = ""
    emergency_ratio_normalized: bool = False
    placeholder: bool = False
    failure_ordinal: str = ""
    source_width_px: int = 0
    source_height_px: int = 0
    output_width_px: int = 0
    output_height_px: int = 0
    page_width_pt: float = 0.0
    page_height_pt: float = 0.0
    review_enabled: bool = False
    review_status: str = "skipped"
    review_attempts: int = 0
    review_issues: list[dict[str, Any]] = field(default_factory=list)
    review_minor_suggestions: list[str] = field(default_factory=list)
    candidate_artifacts: list[dict[str, Any]] = field(default_factory=list)
    final_candidate_attempt: int = 0


@dataclass
class PdfFileRecord:
    name: str
    source_path: str
    relative_path: str
    source_copy_path: str = ""
    translated_pdf_path: str = ""
    compressed_pdf_path: str = ""
    status: str = "pending"
    page_count: int = 0
    generated_page_count: int = 0
    placeholder_page_count: int = 0
    emergency_ratio_normalized_count: int = 0
    retry_count: int = 0
    review_enabled: bool = False
    reviewed_page_count: int = 0
    review_passed_page_count: int = 0
    review_repaired_page_count: int = 0
    review_failed_page_count: int = 0
    review_retry_count: int = 0
    review_minor_suggestion_count: int = 0
    error: str = ""
    compression_error: str = ""
    source_pdf_size_bytes: int = 0
    high_quality_pdf_size_bytes: int = 0
    compressed_pdf_size_bytes: int = 0
    pages: list[PdfPageRecord] = field(default_factory=list)


@dataclass
class PdfTaskSummary:
    status: str
    output_dir: str
    target_lang: str
    target_lang_label: str
    started_at: str
    completed_at: str
    elapsed_sec: float
    file_count: int
    total_page_count: int
    generated_pdf_count: int
    placeholder_page_count: int
    emergency_ratio_normalized_count: int
    retry_count: int
    compressed_pdf_enabled: bool = False
    compressed_pdf_count: int = 0
    compression_quality: int = PDF_COMPRESSED_JPEG_QUALITY_DEFAULT
    compression_max_long_edge_px: int = PDF_COMPRESSED_MAX_LONG_EDGE_PX
    review_enabled: bool = False
    reviewed_page_count: int = 0
    review_passed_page_count: int = 0
    review_repaired_page_count: int = 0
    review_failed_page_count: int = 0
    review_retry_count: int = 0
    review_minor_suggestion_count: int = 0
    rate_limit_reduction_count: int = 0
    partial_artifacts_available: bool = False
    image_model_signature: str = ""
    pdf_review_model_signature: str = ""
    stopped: bool = False
    files: list[PdfFileRecord] = field(default_factory=list)


def is_supported_pdf_file(path: str | Path) -> bool:
    path = Path(path)
    return (
        path.is_file()
        and path.suffix.lower() in SUPPORTED_PDF_SUFFIXES
        and not path.name.startswith(("~", "."))
    )


def scan_pdf_path(path: str | Path) -> list[PdfFileItem]:
    path = Path(path).expanduser()
    if not path.exists():
        logger.warning(f"路径不存在：{path}")
        return []
    if path.is_file():
        if not is_supported_pdf_file(path):
            return []
        return [_build_pdf_file_item(path)]
    items: list[PdfFileItem] = []
    for pdf_path in path.rglob("*.pdf"):
        rel = pdf_path.relative_to(path)
        if _should_skip_scanned_pdf(rel):
            continue
        try:
            items.append(_build_pdf_file_item(pdf_path))
        except Exception as exc:  # noqa: BLE001 - scanning should continue.
            logger.warning(f"扫描 PDF 失败 {pdf_path.name}：{exc}")
    items.sort(key=lambda item: item.path)
    return items


def page_image_name(page_number: int, total_pages: int, *, failed: bool = False) -> str:
    width = max(3, len(str(max(1, int(total_pages or 1)))))
    suffix = "_failed" if failed else ""
    return f"page_{int(page_number):0{width}d}{suffix}.png"


def check_page_quality(
    image_bytes: bytes,
    *,
    source_width: int,
    source_height: int,
    ratio_tolerance: float = PDF_ASPECT_RATIO_TOLERANCE,
    min_short_edge: int = PDF_MIN_READABLE_SHORT_EDGE_PX,
    min_long_edge: int = PDF_MIN_READABLE_LONG_EDGE_PX,
) -> PageQualityResult:
    try:
        with Image.open(BytesIO(image_bytes)) as image:
            image.load()
            width, height = image.size
    except Exception as exc:  # noqa: BLE001 - user-facing QC result.
        return PageQualityResult(False, "decode_error", f"图片无法解码：{exc}")

    source_ratio = _safe_ratio(source_width, source_height)
    output_ratio = _safe_ratio(width, height)
    if source_ratio <= 0 or output_ratio <= 0:
        return PageQualityResult(False, "ratio_error", "页面比例无法计算", width, height)
    if abs(source_ratio - output_ratio) / source_ratio > ratio_tolerance:
        return PageQualityResult(
            False,
            "ratio_error",
            f"页面比例超出 {ratio_tolerance:.0%} 容差",
            width,
            height,
        )

    short_edge = min(width, height)
    long_edge = max(width, height)
    if short_edge < min_short_edge or long_edge < min_long_edge:
        return PageQualityResult(
            False,
            "low_resolution",
            f"分辨率过低：{width}x{height}",
            width,
            height,
        )

    return PageQualityResult(True, "ok", "", width, height)


def normalize_image_to_source_ratio(
    image_bytes: bytes,
    *,
    source_width: int,
    source_height: int,
    output_path: Path,
) -> tuple[int, int]:
    with Image.open(BytesIO(image_bytes)) as source:
        image = source.convert("RGB")
    canvas = Image.new("RGB", (int(source_width), int(source_height)), "white")
    image.thumbnail((int(source_width), int(source_height)), Image.Resampling.LANCZOS)
    x = (canvas.width - image.width) // 2
    y = (canvas.height - image.height) // 2
    canvas.paste(image, (x, y))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, format="PNG")
    return canvas.size


def create_failure_placeholder_page(
    *,
    page_number: int,
    failure_ordinal: str,
    error_summary: str,
    source_image_path: Path | str,
    placeholder_path: Path | str,
    width: int,
    height: int,
) -> Path:
    placeholder = Path(placeholder_path)
    placeholder.parent.mkdir(parents=True, exist_ok=True)
    canvas = Image.new("RGB", (max(800, int(width)), max(1000, int(height))), "white")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()
    title_font = ImageFont.load_default(size=28)
    bar_height = 92
    draw.rectangle((0, 0, canvas.width, bar_height), fill=(178, 34, 34))
    draw.rectangle((18, 18, canvas.width - 18, canvas.height - 18), outline=(178, 34, 34), width=6)
    draw.text(
        (72, 30),
        "PDF PAGE TRANSLATION NEEDS REVIEW",
        fill="white",
        font=title_font,
    )
    x = 72
    y = bar_height + 42
    lines = [
        f"Page: {page_number}",
        f"Failure: {failure_ordinal}",
        f"Error: {error_summary or 'Unknown error'}",
        f"Source page image: {source_image_path}",
        f"Placeholder page: {placeholder}",
    ]
    for line in lines:
        for wrapped in _wrap_line(str(line), max_chars=96):
            draw.text((x, y), wrapped, fill="black", font=font)
            y += 28
        y += 10
    canvas.save(placeholder, format="PNG")
    return placeholder


def is_app_managed_pdf_output_dir(output_dir: str | Path) -> bool:
    path = Path(output_dir)
    return (
        (path / PDF_MANIFEST_FILENAME).exists()
        or "_翻译输出_" in path.name
        or (path / PDF_PAGES_ROOT).exists()
    )


def translated_pdf_base_name(
    source_pdf_name: str,
    target_lang: str,
    settings: AppSettings,
    *,
    variant_label: str | None = None,
) -> str:
    label = _sanitize_filename_fragment(
        get_target_lang_display(
            target_lang,
            settings.custom_target_langs,
            include_optional=True,
        )
    )
    source_path = Path(source_pdf_name)
    suffix = f"_{_sanitize_filename_fragment(variant_label)}" if variant_label else ""
    return f"译文({label})_{source_path.stem}{suffix}{source_path.suffix}"


def resolve_translated_pdf_path(
    target_dir: Path,
    source_pdf_name: str,
    target_lang: str,
    settings: AppSettings,
    *,
    app_managed: bool,
    variant_label: str | None = None,
) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    base_name = translated_pdf_base_name(
        source_pdf_name,
        target_lang,
        settings,
        variant_label=variant_label,
    )
    base_path = target_dir / base_name
    stem = base_path.stem
    suffix = base_path.suffix
    if app_managed and _has_revision_files(target_dir, stem, suffix) and not base_path.exists():
        next_revision = _next_revision_number(target_dir, stem, suffix)
        return target_dir / f"{stem}_R{next_revision}{suffix}"
    if not base_path.exists():
        return base_path

    if app_managed:
        r1_path = target_dir / f"{stem}_R1{suffix}"
        if not r1_path.exists():
            base_path.rename(r1_path)
        next_revision = _next_revision_number(target_dir, stem, suffix)
        return target_dir / f"{stem}_R{next_revision}{suffix}"

    next_revision = _next_revision_number(target_dir, stem, suffix)
    return target_dir / f"{stem}_R{next_revision}{suffix}"


def resolve_translated_pdf_variant_paths(
    target_dir: Path,
    source_pdf_name: str,
    target_lang: str,
    settings: AppSettings,
    *,
    app_managed: bool,
) -> tuple[Path, Path]:
    """Resolve matched high-quality and compressed PDF output paths."""
    target_dir.mkdir(parents=True, exist_ok=True)
    high_base = target_dir / translated_pdf_base_name(
        source_pdf_name,
        target_lang,
        settings,
        variant_label="高清",
    )
    compressed_base = target_dir / translated_pdf_base_name(
        source_pdf_name,
        target_lang,
        settings,
        variant_label="压缩",
    )
    stems = (high_base.stem, compressed_base.stem)
    suffix = high_base.suffix

    if app_managed:
        for base_path in (high_base, compressed_base):
            if base_path.exists():
                r1_path = base_path.with_name(f"{base_path.stem}_R1{base_path.suffix}")
                if not r1_path.exists():
                    base_path.rename(r1_path)

    if not app_managed and not high_base.exists() and not compressed_base.exists():
        has_revisions = any(_has_revision_files(target_dir, stem, suffix) for stem in stems)
        if not has_revisions:
            return high_base, compressed_base

    if app_managed:
        has_revisions = any(_has_revision_files(target_dir, stem, suffix) for stem in stems)
        if not has_revisions and not high_base.exists() and not compressed_base.exists():
            return high_base, compressed_base

    next_revision = max(_next_revision_number(target_dir, stem, suffix) for stem in stems)
    return (
        high_base.with_name(f"{high_base.stem}_R{next_revision}{high_base.suffix}"),
        compressed_base.with_name(
            f"{compressed_base.stem}_R{next_revision}{compressed_base.suffix}"
        ),
    )


def determine_pdf_task_status(
    *,
    stopped: bool,
    file_records: list[PdfFileRecord],
    fatal_error: str = "",
) -> str:
    if stopped:
        return PDF_OUTPUT_STATE_STOPPED
    if fatal_error and not any(record.translated_pdf_path for record in file_records):
        return PDF_OUTPUT_STATE_FAILED
    if not file_records or any(record.status == PDF_OUTPUT_STATE_FAILED for record in file_records):
        return PDF_OUTPUT_STATE_FAILED
    if any(
        record.placeholder_page_count
        or record.emergency_ratio_normalized_count
        or record.review_failed_page_count
        for record in file_records
    ):
        return PDF_OUTPUT_STATE_NEEDS_REVIEW
    return PDF_OUTPUT_STATE_COMPLETED


def build_pdf_output_dir(
    source_root: str | Path,
    custom_output_dir: str | Path | None = None,
) -> Path:
    return bilingual_writer.build_output_dir(source_root, custom_output_dir)


def resolve_pdf_page_archive_dirs(output_dir: Path, relative_pdf: Path) -> tuple[Path, Path]:
    archive_stem = _page_archive_stem(relative_pdf)
    return (
        output_dir / PDF_PAGES_ROOT / SOURCE_PAGES_DIRNAME / archive_stem,
        output_dir / PDF_PAGES_ROOT / TRANSLATED_PAGES_DIRNAME / archive_stem,
    )


def resolve_pdf_review_candidates_dir(output_dir: Path, relative_pdf: Path) -> Path:
    return output_dir / PDF_PAGES_ROOT / REVIEW_CANDIDATES_DIRNAME / _page_archive_stem(relative_pdf)


def write_pdf_manifest_and_report(summary: PdfTaskSummary) -> tuple[Path, Path]:
    output_dir = Path(summary.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / PDF_MANIFEST_FILENAME
    report_path = output_dir / PDF_REPORT_FILENAME
    manifest_path.write_text(
        json.dumps(_summary_to_manifest(summary), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    report_path.write_text(_summary_to_report(summary), encoding="utf-8")
    return manifest_path, report_path


class PdfImageTranslationRunner:
    """Background runner for PDF image-layout translation."""

    def __init__(
        self,
        file_items: list[PdfFileItem],
        settings: AppSettings,
        *,
        source_root: Path | str | None = None,
        image_client: ImageGenerationClient | None = None,
        review_client: PdfPageReviewClient | None = None,
        task_logger_enabled: bool = True,
    ) -> None:
        self._files = file_items
        self._settings = settings
        self._source_root = Path(source_root) if source_root else None
        self._image_client = image_client or OpenAICompatibleImageGenerationClient()
        self._review_client = review_client or OpenAICompatiblePdfReviewClient()
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._task_logger = TaskLogger(enabled=task_logger_enabled)
        self._rate_limit_reduction_count = 0
        self._api_call_count = 0
        self._review_api_call_count = 0
        self._fatal_model_error = ""
        self._fatal_review_model_error = ""
        self._review_lock = threading.Lock()
        self._review_processing_count = 0
        self._review_passed_count = 0
        self._review_failed_count = 0
        self._latest_review_round = 0
        self._review_total = 0

    @property
    def task_id(self) -> str:
        return self._task_logger.task_id

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def stop_requested(self) -> bool:
        return self._stop_event.is_set()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def needs_poll(self) -> bool:
        return self.is_running() or not self._queue.empty()

    def get_message(self, timeout: float = 0.05):
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def _log(self, level: str, message: str) -> None:
        self._queue.put(LogMsg(level=level, message=message))
        logger.info(f"[PDF][{level}] {message}")

    def _run(self) -> None:
        started = datetime.now()
        output_dir: Path | None = None
        file_records: list[PdfFileRecord] = []
        fatal_error = ""
        stopped = False
        self._fatal_model_error = ""
        self._fatal_review_model_error = ""
        model_signature = _safe_image_model_signature(self._settings)
        review_model_config = None
        review_model_signature = _safe_pdf_review_model_signature(self._settings)
        review_enabled = bool(self._settings.pdf.review_enabled)

        try:
            model_config = resolve_effective_model_config(self._settings, ROLE_IMAGE)
            model_signature = image_model_signature(self._settings)
            if not provider_supports_capability(model_config.provider, "image"):
                raise ImageModelUnavailableError(
                    f"当前图像生成模型服务商不支持图像生成能力：{model_config.provider}"
                )
            if not model_config.model:
                raise ImageModelUnavailableError("图像生成模型名称不能为空")
        except ImageModelUnavailableError as exc:
            message = str(exc)
            record_image_model_availability(
                self._settings,
                ok=False,
                message=message,
                signature=model_signature,
                checked_at=datetime.now().isoformat(timespec="seconds"),
            )
            self._queue.put(ErrorMsg(message=f"图像生成模型配置不可用：{message}"))
            return
        except Exception as exc:  # noqa: BLE001 - init converted to UI error.
            self._queue.put(ErrorMsg(message=f"图像生成模型配置不可用：{exc}"))
            return

        if review_enabled:
            try:
                review_model_config = resolve_effective_model_config(
                    self._settings,
                    ROLE_PDF_REVIEW,
                )
                review_model_signature = pdf_review_model_signature(self._settings)
                if not provider_supports_capability(review_model_config.provider, "vision_text"):
                    raise PdfReviewModelUnavailableError(
                        f"当前 PDF 翻译审核模型服务商不支持图像理解审核能力：{review_model_config.provider}"
                    )
                if not review_model_config.model:
                    raise PdfReviewModelUnavailableError("PDF 翻译审核模型名称不能为空")
            except PdfReviewModelUnavailableError as exc:
                message = str(exc)
                record_pdf_review_model_availability(
                    self._settings,
                    ok=False,
                    message=message,
                    signature=review_model_signature,
                    checked_at=datetime.now().isoformat(timespec="seconds"),
                )
                self._queue.put(ErrorMsg(message=f"PDF 翻译审核模型配置不可用：{message}"))
                return
            except Exception as exc:  # noqa: BLE001 - init converted to UI error.
                self._queue.put(ErrorMsg(message=f"PDF 翻译审核模型配置不可用：{exc}"))
                return

        root_for_output = self._source_root if self._source_root else self._files[0].path.parent
        custom_output_dir = (
            self._settings.output.custom_output_dir
            if self._settings.output.use_custom_output_dir
            else None
        )

        try:
            output_dir = build_pdf_output_dir(root_for_output, custom_output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
            app_managed = is_app_managed_pdf_output_dir(output_dir)
            retry_attempts = max(
                1,
                int(
                    self._settings.pdf.page_retry_attempts
                    or PDF_PAGE_RETRY_ATTEMPTS_DEFAULT
                ),
            )
            concurrency = self._resolve_pdf_concurrency()
            self._review_total = retry_attempts
            scheduler = WeightedApiScheduler(concurrency)
            self._log("INFO", f"扫描到 {len(self._files)} 个 PDF 文件")
            self._log("INFO", f"输出目录：{output_dir}")
            self._log("INFO", f"PDF 页固定以 {PDF_RENDER_DPI_DEFAULT} DPI 渲染为 PNG")
            if self._settings.pdf.generate_compressed_pdf:
                self._log(
                    "INFO",
                    f"将同时生成高清 PDF 和压缩 PDF（JPEG quality={PDF_COMPRESSED_JPEG_QUALITY_DEFAULT}）",
                )
            else:
                self._log("INFO", "将只生成高清 PDF")
            if review_enabled:
                self._log("INFO", "已启用 PDF 翻译审核：候选译图会经审核模型判断后再采用。")
                self._emit_review_status()

            total_pages = sum(max(0, int(item.page_count or 0)) for item in self._files)
            if total_pages <= 0:
                total_pages = len(self._files)
            processed_pages = 0

            for file_index, item in enumerate(self._files, start=1):
                if self._stop_event.is_set():
                    stopped = True
                    break
                self._queue.put(
                    ProgressMsg(1, 3, "准备 PDF", file_index - 1, len(self._files))
                )
                record = self._process_file(
                    item,
                    output_dir=output_dir,
                    app_managed=app_managed,
                    retry_attempts=retry_attempts,
                    scheduler=scheduler,
                    model_config=model_config,
                    review_model_config=review_model_config,
                    concurrency=concurrency,
                    processed_page_offset=processed_pages,
                    total_pages=total_pages,
                )
                file_records.append(record)
                processed_pages += max(1, record.page_count)
                if self._fatal_model_error:
                    fatal_error = self._fatal_model_error
                    if self._fatal_review_model_error:
                        record_pdf_review_model_availability(
                            self._settings,
                            ok=False,
                            message=self._fatal_review_model_error,
                            signature=review_model_signature,
                            checked_at=datetime.now().isoformat(timespec="seconds"),
                        )
                    break
                if self._stop_event.is_set():
                    stopped = True
                    break

            if stopped and not fatal_error:
                self._log("WARN", "任务已中止：不再提交新页，也不合成最终 PDF。")
        except ImageModelUnavailableError as exc:
            fatal_error = str(exc)
            record_image_model_availability(
                self._settings,
                ok=False,
                message=fatal_error,
                signature=model_signature,
                checked_at=datetime.now().isoformat(timespec="seconds"),
            )
            self._log("ERROR", fatal_error)
        except PdfReviewModelUnavailableError as exc:
            fatal_error = str(exc)
            record_pdf_review_model_availability(
                self._settings,
                ok=False,
                message=fatal_error,
                signature=review_model_signature,
                checked_at=datetime.now().isoformat(timespec="seconds"),
            )
            self._log("ERROR", fatal_error)
        except Exception as exc:  # noqa: BLE001 - task-level failure.
            fatal_error = str(exc)
            self._log("ERROR", f"PDF 翻译任务失败：{fatal_error}")

        if output_dir is not None:
            summary = self._build_summary(
                output_dir=output_dir,
                started=started,
                file_records=file_records,
                stopped=stopped,
                fatal_error=fatal_error,
            )
            manifest_path, report_path = write_pdf_manifest_and_report(summary)
            self._log("INFO", f"已写入 PDF 翻译清单：{manifest_path.name}")
            self._log("INFO", f"已写入 PDF 翻译报告：{report_path.name}")

            if stopped:
                self._queue.put(
                    StoppedMsg(
                        message=f"PDF 翻译已中止，已保留页面素材和报告：{output_dir}",
                        output_dir=str(output_dir),
                        report_path=str(report_path),
                        manifest_path=str(manifest_path),
                    )
                )
                return
            if fatal_error and summary.status == PDF_OUTPUT_STATE_FAILED:
                self._queue.put(
                    ErrorMsg(
                        message=fatal_error,
                        output_dir=str(output_dir),
                        report_path=str(report_path),
                        manifest_path=str(manifest_path),
                    )
                )
                return
            self._queue.put(
                DoneMsg(
                    output_dir=str(output_dir),
                    file_results=[_file_record_to_result(record) for record in file_records],
                    elapsed_sec=summary.elapsed_sec,
                    tm_hit_count=0,
                    api_call_count=self._api_call_count,
                    issues=_summary_issues(file_records),
                    report_path=str(report_path),
                )
            )
            return

        if fatal_error:
            self._queue.put(ErrorMsg(message=fatal_error))

    def _process_file(
        self,
        item: PdfFileItem,
        *,
        output_dir: Path,
        app_managed: bool,
        retry_attempts: int,
        scheduler: WeightedApiScheduler,
        model_config,
        review_model_config,
        concurrency: int,
        processed_page_offset: int,
        total_pages: int,
    ) -> PdfFileRecord:
        relative_pdf = _relative_pdf_path(item.path, self._source_root)
        source_copy_path = output_dir / relative_pdf
        source_copy_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item.path, source_copy_path)
        record = PdfFileRecord(
            name=item.path.name,
            source_path=str(item.path),
            relative_path=str(relative_pdf),
            source_copy_path=str(source_copy_path),
            source_pdf_size_bytes=_safe_file_size(item.path),
        )
        self._log("INFO", f"[{item.path.name}] 已复制源 PDF 到输出目录")

        try:
            import fitz  # type: ignore
        except Exception as exc:  # noqa: BLE001 - dependency may be absent in dev env.
            record.status = PDF_OUTPUT_STATE_FAILED
            record.error = f"PyMuPDF 未安装或不可用：{exc}"
            return record

        source_pages_dir, translated_pages_dir = resolve_pdf_page_archive_dirs(
            output_dir,
            relative_pdf,
        )
        review_candidates_dir = resolve_pdf_review_candidates_dir(output_dir, relative_pdf)
        source_pages_dir.mkdir(parents=True, exist_ok=True)
        translated_pages_dir.mkdir(parents=True, exist_ok=True)
        if self._settings.pdf.review_enabled:
            review_candidates_dir.mkdir(parents=True, exist_ok=True)
        translated_pdf_path, compressed_pdf_path = resolve_translated_pdf_variant_paths(
            source_copy_path.parent,
            item.path.name,
            self._settings.target_lang,
            self._settings,
            app_managed=app_managed,
        )

        try:
            with fitz.open(str(item.path)) as doc:
                if getattr(doc, "needs_pass", False):
                    record.status = PDF_OUTPUT_STATE_FAILED
                    record.error = "受保护 PDF 暂不在本轮支持范围内。"
                    return record
                page_count = int(doc.page_count)
                record.page_count = page_count
                futures: dict[Any, PdfPageRecord] = {}
                max_pending = max(1, concurrency + PDF_PAGE_RENDER_AHEAD_COUNT)
                next_page_index = 0
                with ThreadPoolExecutor(max_workers=concurrency) as executor:
                    while next_page_index < page_count or futures:
                        while (
                            next_page_index < page_count
                            and len(futures) < max_pending
                            and not self._stop_event.is_set()
                        ):
                            page_number = next_page_index + 1
                            page_record = self._render_source_page(
                                doc,
                                page_index=next_page_index,
                                page_count=page_count,
                                source_pages_dir=source_pages_dir,
                            )
                            record.pages.append(page_record)
                            self._log("INFO", f"[{item.path.name}] 第 {page_number} 页已渲染")
                            future = executor.submit(
                                self._generate_page_with_retries,
                                page_record,
                                page_count,
                                translated_pages_dir,
                                review_candidates_dir,
                                retry_attempts,
                                scheduler,
                                model_config,
                                review_model_config,
                            )
                            futures[future] = page_record
                            self._log("INFO", f"[{item.path.name}] 第 {page_number} 页已提交图像生成")
                            next_page_index += 1

                        if not futures:
                            break
                        done, _ = wait(list(futures.keys()), timeout=0.2, return_when=FIRST_COMPLETED)
                        for future in done:
                            page_record = futures.pop(future)
                            try:
                                updated = future.result()
                                _copy_page_record(updated, page_record)
                            except ImageModelUnavailableError as exc:
                                page_record.status = PDF_OUTPUT_STATE_FAILED
                                page_record.error = str(exc)
                                page_record.attempts = max(1, page_record.attempts)
                                self._stop_event.set()
                                raise
                            except PdfReviewModelUnavailableError as exc:
                                page_record.status = PDF_OUTPUT_STATE_FAILED
                                page_record.error = str(exc)
                                page_record.attempts = max(1, page_record.attempts)
                                self._stop_event.set()
                                raise
                            except Exception as exc:  # noqa: BLE001 - page-level unknown failure.
                                page_record.status = "placeholder_pending"
                                page_record.error = str(exc)
                                page_record.attempts = retry_attempts
                            processed = processed_page_offset + min(
                                page_count,
                                max(next_page_index - len(futures), 0),
                            )
                            self._queue.put(ProgressMsg(2, 3, "生成 PDF 页", processed, total_pages))

                        if self._stop_event.is_set() and next_page_index < page_count:
                            self._log("WARN", f"[{item.path.name}] 已收到中止请求，不再提交新页")
                            wait(list(futures.keys()), timeout=20)
                            break
        except ImageModelUnavailableError as exc:
            self._fatal_model_error = str(exc)
            record.status = PDF_OUTPUT_STATE_FAILED
            record.error = self._fatal_model_error
            return record
        except PdfReviewModelUnavailableError as exc:
            self._fatal_model_error = str(exc)
            self._fatal_review_model_error = self._fatal_model_error
            record.status = PDF_OUTPUT_STATE_FAILED
            record.error = self._fatal_model_error
            return record

        if self._stop_event.is_set():
            record.status = PDF_OUTPUT_STATE_STOPPED
            return record

        self._finalize_placeholders(record, translated_pages_dir)
        record.generated_page_count = sum(
            1 for page in record.pages if page.status in {"success", "emergency_normalized", "placeholder"}
        )
        record.placeholder_page_count = sum(1 for page in record.pages if page.placeholder)
        record.emergency_ratio_normalized_count = sum(
            1 for page in record.pages if page.emergency_ratio_normalized
        )
        record.retry_count = sum(max(0, page.attempts - 1) for page in record.pages)
        record.review_enabled = bool(self._settings.pdf.review_enabled)
        record.reviewed_page_count = sum(
            1 for page in record.pages if page.review_status in {"passed", "failed"}
        )
        record.review_passed_page_count = sum(
            1 for page in record.pages if page.review_status == "passed"
        )
        record.review_repaired_page_count = sum(
            1
            for page in record.pages
            if page.review_status == "passed" and page.final_candidate_attempt > 1
        )
        record.review_failed_page_count = sum(
            1 for page in record.pages if page.review_status == "failed"
        )
        record.review_retry_count = sum(max(0, page.review_attempts - 1) for page in record.pages)
        record.review_minor_suggestion_count = sum(
            len(page.review_minor_suggestions) for page in record.pages
        )

        try:
            self._queue.put(ProgressMsg(3, 3, "合成 PDF", 0, 1))
            self._assemble_translated_pdf(record, Path(translated_pdf_path))
            record.translated_pdf_path = str(translated_pdf_path)
            record.high_quality_pdf_size_bytes = _safe_file_size(translated_pdf_path)
            if self._settings.pdf.generate_compressed_pdf:
                try:
                    self._assemble_translated_pdf(
                        record,
                        Path(compressed_pdf_path),
                        compressed=True,
                    )
                    record.compressed_pdf_path = str(compressed_pdf_path)
                    record.compressed_pdf_size_bytes = _safe_file_size(compressed_pdf_path)
                    self._log("OK", f"[{item.path.name}] 已生成压缩 PDF：{compressed_pdf_path.name}")
                except Exception as exc:  # noqa: BLE001 - compressed output is optional.
                    record.compression_error = str(exc)
                    self._log(
                        "WARN",
                        f"[{item.path.name}] 压缩 PDF 生成失败，已保留高清版：{exc}",
                    )
            record.status = (
                PDF_OUTPUT_STATE_NEEDS_REVIEW
                if (
                    record.placeholder_page_count
                    or record.emergency_ratio_normalized_count
                    or record.review_failed_page_count
                )
                else PDF_OUTPUT_STATE_COMPLETED
            )
            self._log("OK", f"[{item.path.name}] 已生成高清 PDF：{translated_pdf_path.name}")
            self._queue.put(ProgressMsg(3, 3, "合成 PDF", 1, 1))
        except Exception as exc:  # noqa: BLE001 - file-level failure.
            record.status = PDF_OUTPUT_STATE_FAILED
            record.error = f"PDF 合成失败：{exc}"
            self._log("ERROR", f"[{item.path.name}] {record.error}")
        return record

    def _render_source_page(
        self,
        doc,
        *,
        page_index: int,
        page_count: int,
        source_pages_dir: Path,
    ) -> PdfPageRecord:
        page = doc.load_page(page_index)
        page_number = page_index + 1
        matrix = _render_matrix()
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        source_path = source_pages_dir / page_image_name(page_number, page_count)
        pix.save(str(source_path))
        rect = page.rect
        return PdfPageRecord(
            page_number=page_number,
            source_image_path=str(source_path),
            status="rendered",
            source_width_px=int(pix.width),
            source_height_px=int(pix.height),
            page_width_pt=float(rect.width),
            page_height_pt=float(rect.height),
        )

    def _generate_page_with_retries(
        self,
        page_record: PdfPageRecord,
        page_count: int,
        translated_pages_dir: Path,
        review_candidates_dir: Path,
        retry_attempts: int,
        scheduler: WeightedApiScheduler,
        model_config,
        review_model_config,
    ) -> PdfPageRecord:
        output_path = translated_pages_dir / page_image_name(page_record.page_number, page_count)
        last_error = ""
        last_quality: PageQualityResult | None = None
        last_image_bytes: bytes = b""
        last_review_failed = False
        review_feedback = ""
        target_language = get_target_lang_display(
            self._settings.target_lang,
            self._settings.custom_target_langs,
            include_optional=True,
        )
        review_enabled = bool(self._settings.pdf.review_enabled and review_model_config is not None)
        page_record.review_enabled = review_enabled
        attempt = 1
        while attempt <= retry_attempts:
            if self._stop_event.is_set():
                page_record.status = PDF_OUTPUT_STATE_STOPPED
                return page_record
            lease = scheduler.acquire_lease(1)
            try:
                self._api_call_count += 1
                image_bytes = self._image_client.generate_page(
                    source_image_path=Path(page_record.source_image_path),
                    target_language=target_language,
                    model_config=model_config,
                    review_feedback=review_feedback or None,
                )
            except Exception as exc:  # noqa: BLE001 - page/model classification.
                scheduler.release(lease)
                if isinstance(exc, ImageModelUnavailableError) or is_model_unavailable_error(exc):
                    raise ImageModelUnavailableError(str(exc)) from exc
                decision = handle_api_concurrency_limit(
                    exc,
                    scheduler=scheduler,
                    request_generation=lease.generation,
                    context_label=f"PDF 第 {page_record.page_number} 页",
                    error_callback=lambda message: self._record_rate_limit_reduction(message),
                )
                if decision is not None and decision.should_retry:
                    time.sleep(0.2)
                    continue
                last_error = str(exc)
                self._log(
                    "WARN",
                    f"第 {page_record.page_number} 页第 {attempt}/{retry_attempts} 次生成失败：{last_error}",
                )
                page_record.attempts = attempt
                attempt += 1
                continue
            else:
                scheduler.release(lease)

            quality = check_page_quality(
                image_bytes,
                source_width=page_record.source_width_px,
                source_height=page_record.source_height_px,
            )
            page_record.attempts = attempt
            last_quality = quality
            last_image_bytes = image_bytes
            candidate_artifact: dict[str, Any] | None = None
            candidate_path: Path | None = None
            if review_enabled:
                candidate_dir = (
                    review_candidates_dir
                    / page_image_name(page_record.page_number, page_count).removesuffix(".png")
                )
                candidate_path = candidate_dir / f"attempt_{attempt:02d}.png"
                _write_image_candidate(image_bytes, candidate_path)
                candidate_artifact = {
                    "attempt": attempt,
                    "candidate_image_path": str(candidate_path),
                    "review_path": "",
                    "quality_status": quality.status,
                    "review_status": "skipped",
                    "summary": "",
                }
                page_record.candidate_artifacts.append(candidate_artifact)
            if quality.ok:
                if review_enabled and candidate_path is not None and candidate_artifact is not None:
                    page_record.review_attempts = attempt
                    self._begin_page_review(attempt)
                    try:
                        self._review_api_call_count += 1
                        review_result = self._review_client.review_page(
                            source_image_path=Path(page_record.source_image_path),
                            translated_image_path=candidate_path,
                            target_language=target_language,
                            model_config=review_model_config,
                        )
                    except Exception as exc:  # noqa: BLE001 - review participates in page recovery.
                        self._finish_page_review()
                        if isinstance(exc, PdfReviewModelUnavailableError) or is_model_unavailable_error(exc):
                            raise PdfReviewModelUnavailableError(str(exc)) from exc
                        review_result = PdfPageReviewResult(
                            passed=False,
                            blocking_issues=[
                                PdfReviewIssue(
                                    type="review_request_failed",
                                    problem=str(exc),
                                    suggestion="请重试审核或更换 PDF 翻译审核模型。",
                                )
                            ],
                            summary=f"审核请求失败：{exc}",
                        )
                    else:
                        self._finish_page_review()

                    review_path = candidate_path.with_name(f"attempt_{attempt:02d}_review.json")
                    _write_review_json(review_path, review_result)
                    self._mark_pdf_review_model_success()
                    candidate_artifact["review_path"] = str(review_path)
                    candidate_artifact["review_status"] = (
                        "passed" if review_result.passed else "failed"
                    )
                    candidate_artifact["summary"] = review_result.summary
                    page_record.review_minor_suggestions.extend(review_result.minor_suggestions)
                    if review_result.minor_suggestions:
                        self._log(
                            "INFO",
                            f"第 {page_record.page_number} 页审核轻微建议："
                            + "；".join(review_result.minor_suggestions[:3]),
                        )
                    if review_result.passed:
                        output_path.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(candidate_path, output_path)
                        self._record_page_review_passed()
                        page_record.status = "success"
                        page_record.review_status = "passed"
                        page_record.translated_image_path = str(output_path)
                        page_record.output_width_px = quality.width
                        page_record.output_height_px = quality.height
                        page_record.final_candidate_attempt = attempt
                        self._mark_image_model_success()
                        self._log(
                            "OK",
                            f"第 {page_record.page_number} 页翻译审核通过（第 {attempt}/{retry_attempts} 轮）",
                        )
                        return page_record

                    last_review_failed = True
                    page_record.review_status = "retrying"
                    page_record.review_issues = [
                        issue.__dict__ for issue in review_result.blocking_issues
                    ]
                    last_error = _review_failure_summary(review_result)
                    review_feedback = _review_feedback_text(review_result)
                    self._log(
                        "WARN",
                        f"第 {page_record.page_number} 页第 {attempt}/{retry_attempts} 轮审核未通过：{last_error}",
                    )
                    attempt += 1
                    continue

                output_path.parent.mkdir(parents=True, exist_ok=True)
                with Image.open(BytesIO(image_bytes)) as image:
                    image.save(output_path, format="PNG")
                self._mark_image_model_success()
                page_record.status = "success"
                page_record.translated_image_path = str(output_path)
                page_record.output_width_px = quality.width
                page_record.output_height_px = quality.height
                return page_record

            last_review_failed = False
            last_error = quality.message
            self._log(
                "WARN",
                f"第 {page_record.page_number} 页第 {attempt}/{retry_attempts} 次质检未通过：{quality.message}",
            )
            attempt += 1

        if (
            not last_review_failed
            and last_quality is not None
            and last_quality.status == "ratio_error"
            and last_image_bytes
        ):
            width, height = normalize_image_to_source_ratio(
                last_image_bytes,
                source_width=page_record.source_width_px,
                source_height=page_record.source_height_px,
                output_path=output_path,
            )
            page_record.status = "emergency_normalized"
            page_record.emergency_ratio_normalized = True
            page_record.translated_image_path = str(output_path)
            page_record.output_width_px = width
            page_record.output_height_px = height
            page_record.error = last_error
            self._log("WARN", f"第 {page_record.page_number} 页已执行应急比例归一化")
            return page_record

        page_record.status = "placeholder_pending"
        page_record.placeholder = True
        if last_review_failed:
            page_record.review_status = "failed"
            self._record_page_review_failed()
        page_record.translated_image_path = str(
            translated_pages_dir / page_image_name(page_record.page_number, page_count, failed=True)
        )
        page_record.error = last_error or "图像生成失败"
        return page_record

    def _mark_image_model_success(self) -> None:
        if self._settings.image_model_role.availability_status == "available":
            return
        record_image_model_availability(
            self._settings,
            ok=True,
            message="PDF 页图像生成调用成功。",
            checked_at=datetime.now().isoformat(timespec="seconds"),
        )

    def _mark_pdf_review_model_success(self) -> None:
        if self._settings.pdf_review_model_role.availability_status == "available":
            return
        record_pdf_review_model_availability(
            self._settings,
            ok=True,
            message="PDF 页翻译审核调用成功。",
            checked_at=datetime.now().isoformat(timespec="seconds"),
        )

    def _begin_page_review(self, attempt: int) -> None:
        with self._review_lock:
            self._latest_review_round = max(self._latest_review_round, attempt)
            self._review_processing_count += 1
            self._emit_review_status_locked()

    def _finish_page_review(self) -> None:
        with self._review_lock:
            self._review_processing_count = max(0, self._review_processing_count - 1)
            self._emit_review_status_locked()

    def _record_page_review_passed(self) -> None:
        with self._review_lock:
            self._review_passed_count += 1
            self._emit_review_status_locked()

    def _record_page_review_failed(self) -> None:
        with self._review_lock:
            self._review_failed_count += 1
            self._emit_review_status_locked()

    def _emit_review_status(self) -> None:
        with self._review_lock:
            self._emit_review_status_locked()

    def _emit_review_status_locked(self) -> None:
        self._queue.put(
            PdfReviewStatusMsg(
                enabled=bool(self._settings.pdf.review_enabled),
                review_round=self._latest_review_round,
                review_total=self._review_total,
                review_processing_count=self._review_processing_count,
                review_passed_count=self._review_passed_count,
                review_failed_count=self._review_failed_count,
            )
        )

    def _record_rate_limit_reduction(self, message: str) -> None:
        self._rate_limit_reduction_count += 1
        self._log("WARN", message)

    def _finalize_placeholders(
        self,
        record: PdfFileRecord,
        translated_pages_dir: Path,
    ) -> None:
        failed_pages = [
            page
            for page in sorted(record.pages, key=lambda item: item.page_number)
            if page.status == "placeholder_pending"
        ]
        total_failed = len(failed_pages)
        for index, page in enumerate(failed_pages, start=1):
            failure_ordinal = f"{index}/{total_failed}"
            placeholder_path = translated_pages_dir / page_image_name(
                page.page_number,
                record.page_count,
                failed=True,
            )
            create_failure_placeholder_page(
                page_number=page.page_number,
                failure_ordinal=failure_ordinal,
                error_summary=page.error,
                source_image_path=page.source_image_path,
                placeholder_path=placeholder_path,
                width=page.source_width_px,
                height=page.source_height_px,
            )
            page.status = "placeholder"
            page.placeholder = True
            page.failure_ordinal = failure_ordinal
            page.translated_image_path = str(placeholder_path)
            page.output_width_px = page.source_width_px
            page.output_height_px = page.source_height_px
            self._log("WARN", f"[{record.name}] 第 {page.page_number} 页已生成失败占位页 {failure_ordinal}")

    def _assemble_translated_pdf(
        self,
        record: PdfFileRecord,
        output_pdf: Path,
        *,
        compressed: bool = False,
    ) -> None:
        try:
            import fitz  # type: ignore
        except Exception as exc:  # noqa: BLE001 - dependency may be absent in dev env.
            raise RuntimeError(f"PyMuPDF 未安装或不可用：{exc}") from exc

        output_pdf.parent.mkdir(parents=True, exist_ok=True)
        out_doc = fitz.open()
        try:
            with tempfile.TemporaryDirectory(prefix="xl-translator-pdf-") as tmp_dir:
                temp_dir = Path(tmp_dir)
                for page in sorted(record.pages, key=lambda item: item.page_number):
                    if not page.translated_image_path:
                        raise RuntimeError(f"第 {page.page_number} 页缺少译后页图像")
                    image_path = Path(page.translated_image_path)
                    if compressed:
                        try:
                            image_path = _write_compressed_page_jpeg(
                                image_path,
                                temp_dir / f"page_{page.page_number:04d}.jpg",
                            )
                        except Exception as exc:  # noqa: BLE001 - fall back per page.
                            self._log(
                                "WARN",
                                f"第 {page.page_number} 页压缩失败，已回退高清页：{exc}",
                            )
                            image_path = Path(page.translated_image_path)
                    pdf_page = out_doc.new_page(
                        width=page.page_width_pt,
                        height=page.page_height_pt,
                    )
                    rect = fitz.Rect(0, 0, page.page_width_pt, page.page_height_pt)
                    pdf_page.insert_image(rect, filename=str(image_path))
                _save_pdf_document(out_doc, output_pdf)
        finally:
            out_doc.close()

    def _resolve_pdf_concurrency(self) -> int:
        raw = self._settings.pdf.page_generation_concurrency
        if raw is None:
            raw = self._settings.engine.concurrency or CONCURRENCY_DEFAULT
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = CONCURRENCY_DEFAULT
        return max(1, min(PDF_PAGE_CONCURRENCY_SAFETY_CAP, value))

    def _build_summary(
        self,
        *,
        output_dir: Path,
        started: datetime,
        file_records: list[PdfFileRecord],
        stopped: bool,
        fatal_error: str,
    ) -> PdfTaskSummary:
        completed = datetime.now()
        status = determine_pdf_task_status(
            stopped=stopped,
            file_records=file_records,
            fatal_error=fatal_error,
        )
        total_pages = sum(record.page_count for record in file_records)
        generated_pdfs = sum(1 for record in file_records if record.translated_pdf_path)
        compressed_pdfs = sum(1 for record in file_records if record.compressed_pdf_path)
        placeholder_count = sum(record.placeholder_page_count for record in file_records)
        emergency_count = sum(record.emergency_ratio_normalized_count for record in file_records)
        retry_count = sum(record.retry_count for record in file_records)
        review_enabled = bool(self._settings.pdf.review_enabled)
        reviewed_page_count = sum(record.reviewed_page_count for record in file_records)
        review_passed_page_count = sum(record.review_passed_page_count for record in file_records)
        review_repaired_page_count = sum(record.review_repaired_page_count for record in file_records)
        review_failed_page_count = sum(record.review_failed_page_count for record in file_records)
        review_retry_count = sum(record.review_retry_count for record in file_records)
        review_minor_suggestion_count = sum(
            record.review_minor_suggestion_count for record in file_records
        )
        partial_artifacts = any(record.pages or record.source_copy_path for record in file_records)
        return PdfTaskSummary(
            status=status,
            output_dir=str(output_dir),
            target_lang=self._settings.target_lang,
            target_lang_label=get_target_lang_display(
                self._settings.target_lang,
                self._settings.custom_target_langs,
                include_optional=True,
            ),
            started_at=started.isoformat(timespec="seconds"),
            completed_at=completed.isoformat(timespec="seconds"),
            elapsed_sec=(completed - started).total_seconds(),
            file_count=len(file_records),
            total_page_count=total_pages,
            generated_pdf_count=generated_pdfs,
            placeholder_page_count=placeholder_count,
            emergency_ratio_normalized_count=emergency_count,
            retry_count=retry_count,
            compressed_pdf_enabled=bool(self._settings.pdf.generate_compressed_pdf),
            compressed_pdf_count=compressed_pdfs,
            compression_quality=PDF_COMPRESSED_JPEG_QUALITY_DEFAULT,
            compression_max_long_edge_px=PDF_COMPRESSED_MAX_LONG_EDGE_PX,
            review_enabled=review_enabled,
            reviewed_page_count=reviewed_page_count,
            review_passed_page_count=review_passed_page_count,
            review_repaired_page_count=review_repaired_page_count,
            review_failed_page_count=review_failed_page_count,
            review_retry_count=review_retry_count,
            review_minor_suggestion_count=review_minor_suggestion_count,
            rate_limit_reduction_count=self._rate_limit_reduction_count,
            partial_artifacts_available=partial_artifacts,
            image_model_signature=_safe_image_model_signature(self._settings),
            pdf_review_model_signature=(
                _safe_pdf_review_model_signature(self._settings)
                if review_enabled
                else ""
            ),
            stopped=stopped,
            files=file_records,
        )


def _build_pdf_file_item(path: Path) -> PdfFileItem:
    return PdfFileItem(
        path=path,
        name=path.stem,
        size_kb=round(path.stat().st_size / 1024, 1),
        page_count=_read_pdf_page_count(path),
    )


def _read_pdf_page_count(path: Path) -> int:
    try:
        import fitz  # type: ignore
    except Exception:
        return 0
    with fitz.open(str(path)) as doc:
        if getattr(doc, "needs_pass", False):
            return 0
        return int(doc.page_count)


def _should_skip_scanned_pdf(relative_path: Path) -> bool:
    parts = relative_path.parts
    if any(part.startswith(".") for part in parts):
        return True
    if PDF_PAGES_ROOT in parts:
        return True
    if any("_翻译输出_" in part for part in parts):
        return True
    name = relative_path.name
    if name in {PDF_REPORT_FILENAME, PDF_MANIFEST_FILENAME}:
        return True
    return name.startswith("译文(")


def _relative_pdf_path(path: Path, source_root: Path | None) -> Path:
    if source_root is None:
        return Path(path.name)
    try:
        if source_root.is_file():
            return Path(path.name)
        return path.relative_to(source_root)
    except ValueError:
        return Path(path.name)


def _page_archive_stem(relative_pdf: Path) -> Path:
    return relative_pdf.with_suffix("")


def _render_matrix():
    import fitz  # type: ignore

    zoom = PDF_RENDER_DPI_DEFAULT / 72.0
    return fitz.Matrix(zoom, zoom)


def _safe_ratio(width: int, height: int) -> float:
    try:
        width_value = float(width)
        height_value = float(height)
    except (TypeError, ValueError):
        return 0.0
    if width_value <= 0 or height_value <= 0:
        return 0.0
    return width_value / height_value


def _sanitize_filename_fragment(value: str) -> str:
    cleaned = _INVALID_FILENAME_FRAGMENT_RE.sub("_", str(value or "")).strip().rstrip(". ")
    return cleaned or "目标语言"


def _next_revision_number(target_dir: Path, stem: str, suffix: str) -> int:
    revision = 1
    pattern = re.compile(rf"^{re.escape(stem)}_R(\d+){re.escape(suffix)}$")
    for path in target_dir.glob(f"{stem}_R*{suffix}"):
        match = pattern.match(path.name)
        if match:
            revision = max(revision, int(match.group(1)) + 1)
    return revision


def _has_revision_files(target_dir: Path, stem: str, suffix: str) -> bool:
    pattern = re.compile(rf"^{re.escape(stem)}_R(\d+){re.escape(suffix)}$")
    return any(pattern.match(path.name) for path in target_dir.glob(f"{stem}_R*{suffix}"))


def _safe_file_size(path: str | Path) -> int:
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


def _format_file_size(size_bytes: int) -> str:
    if size_bytes <= 0:
        return "未知"
    size = float(size_bytes)
    for unit in ("B", "KB", "MB"):
        if size < 1024 or unit == "MB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} MB"


def _compression_ratio_label(source_bytes: int, target_bytes: int) -> str:
    if source_bytes <= 0 or target_bytes <= 0:
        return ""
    saved = max(0.0, 1.0 - (target_bytes / source_bytes))
    return f"节省 {saved:.0%}"


def _write_compressed_page_jpeg(source_path: Path, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source_path) as source:
        image = _to_white_rgb(source)
        long_edge = max(image.size)
        if long_edge > PDF_COMPRESSED_MAX_LONG_EDGE_PX:
            scale = PDF_COMPRESSED_MAX_LONG_EDGE_PX / float(long_edge)
            target_size = (
                max(1, int(round(image.width * scale))),
                max(1, int(round(image.height * scale))),
            )
            image = image.resize(target_size, Image.Resampling.LANCZOS)
        image.save(
            output_path,
            format="JPEG",
            quality=PDF_COMPRESSED_JPEG_QUALITY_DEFAULT,
            optimize=True,
            subsampling=0,
        )
    return output_path


def _to_white_rgb(image: Image.Image) -> Image.Image:
    if image.mode in {"RGBA", "LA"} or (
        image.mode == "P" and "transparency" in image.info
    ):
        rgba = image.convert("RGBA")
        canvas = Image.new("RGBA", rgba.size, "white")
        canvas.alpha_composite(rgba)
        return canvas.convert("RGB")
    return image.convert("RGB")


def _save_pdf_document(doc, output_pdf: Path) -> None:
    try:
        doc.save(
            str(output_pdf),
            garbage=4,
            deflate=True,
            deflate_images=True,
            deflate_fonts=True,
            use_objstms=1,
        )
    except TypeError:
        doc.save(str(output_pdf))


def _wrap_line(text: str, *, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    return [text[index : index + max_chars] for index in range(0, len(text), max_chars)]


def _copy_page_record(source: PdfPageRecord, target: PdfPageRecord) -> None:
    for key, value in asdict(source).items():
        setattr(target, key, value)


def _write_image_candidate(image_bytes: bytes, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with Image.open(BytesIO(image_bytes)) as image:
            image.save(output_path, format="PNG")
    except Exception:
        output_path.write_bytes(image_bytes)


def _write_review_json(output_path: Path, result: PdfPageReviewResult) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result.to_manifest(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _review_failure_summary(result: PdfPageReviewResult) -> str:
    if result.summary:
        return result.summary
    if result.blocking_issues:
        issue = result.blocking_issues[0]
        parts = [issue.location, issue.problem, issue.suggestion]
        return "；".join(part for part in parts if part) or "审核未通过"
    return "审核未通过"


def _review_feedback_text(result: PdfPageReviewResult) -> str:
    lines: list[str] = []
    for index, issue in enumerate(result.blocking_issues, start=1):
        parts = [
            f"type={issue.type}",
            f"location={issue.location}" if issue.location else "",
            f"problem={issue.problem}" if issue.problem else "",
            f"suggestion={issue.suggestion}" if issue.suggestion else "",
        ]
        lines.append(f"{index}. " + "; ".join(part for part in parts if part))
    return "\n".join(lines) or result.summary or "The previous candidate failed review."


def _summary_to_manifest(summary: PdfTaskSummary) -> dict[str, Any]:
    payload = asdict(summary)
    payload["route"] = "pdf_image_layout_translation"
    payload["render_dpi"] = PDF_RENDER_DPI_DEFAULT
    payload["image_format"] = "png"
    return payload


def _safe_image_model_signature(settings: AppSettings) -> str:
    try:
        return image_model_signature(settings)
    except Exception:
        return ""


def _safe_pdf_review_model_signature(settings: AppSettings) -> str:
    try:
        return pdf_review_model_signature(settings)
    except Exception:
        return ""


def _summary_to_report(summary: PdfTaskSummary) -> str:
    lines = [
        "# PDF 翻译报告",
        "",
        f"- 状态：{summary.status}",
        f"- 目标语言：{summary.target_lang_label}",
        f"- 输出目录：{summary.output_dir}",
        f"- 文件数：{summary.file_count}",
        f"- 总页数：{summary.total_page_count}",
        f"- 已生成高清 PDF：{summary.generated_pdf_count}",
        f"- 已生成压缩 PDF：{summary.compressed_pdf_count}",
        f"- 压缩输出：{'开启' if summary.compressed_pdf_enabled else '关闭'}",
        f"- 压缩 JPEG 质量：{summary.compression_quality}",
        f"- 压缩最大长边：{summary.compression_max_long_edge_px}px",
        f"- 失败占位页：{summary.placeholder_page_count}",
        f"- 应急比例归一化页：{summary.emergency_ratio_normalized_count}",
        f"- 页级重试次数：{summary.retry_count}",
        f"- 翻译审核：{'开启' if summary.review_enabled else '关闭'}",
        f"- 审核通过页：{summary.review_passed_page_count}",
        f"- 审核修复页：{summary.review_repaired_page_count}",
        f"- 审核未通过页：{summary.review_failed_page_count}",
        f"- 审核重试次数：{summary.review_retry_count}",
        f"- 审核轻微建议：{summary.review_minor_suggestion_count}",
        f"- 限流降并发次数：{summary.rate_limit_reduction_count}",
        f"- 保留部分素材：{'是' if summary.partial_artifacts_available else '否'}",
        "",
        "## 文件明细",
        "",
    ]
    for file_record in summary.files:
        lines.extend(
            [
                f"### {file_record.name}",
                "",
                f"- 状态：{file_record.status}",
                f"- 源 PDF：{file_record.source_path}",
                f"- 高清 PDF：{file_record.translated_pdf_path or '未生成'}",
                f"- 压缩 PDF：{file_record.compressed_pdf_path or '未生成'}",
                f"- 源文件体积：{_format_file_size(file_record.source_pdf_size_bytes)}",
                f"- 高清版体积：{_format_file_size(file_record.high_quality_pdf_size_bytes)}",
                f"- 压缩版体积：{_format_file_size(file_record.compressed_pdf_size_bytes)}",
                f"- 页数：{file_record.page_count}",
                f"- 失败占位页：{file_record.placeholder_page_count}",
                f"- 应急比例归一化页：{file_record.emergency_ratio_normalized_count}",
                f"- 翻译审核：{'开启' if file_record.review_enabled else '关闭'}",
                f"- 审核通过页：{file_record.review_passed_page_count}",
                f"- 审核修复页：{file_record.review_repaired_page_count}",
                f"- 审核未通过页：{file_record.review_failed_page_count}",
                f"- 审核轻微建议：{file_record.review_minor_suggestion_count}",
            ]
        )
        if file_record.error:
            lines.append(f"- 错误：{file_record.error}")
        if file_record.compression_error:
            lines.append(f"- 压缩提示：{file_record.compression_error}")
        review_pages = [
            page
            for page in file_record.pages
            if page.placeholder
            or page.emergency_ratio_normalized
            or page.error
            or page.review_issues
            or page.review_minor_suggestions
        ]
        if review_pages:
            lines.extend(["", "| 页码 | 状态 | 审核 | 失败序号 | 摘要 | 原始页图像 | 译后页图像 |", "| --- | --- | --- | --- | --- | --- | --- |"])
            for page in review_pages:
                review_note = page.review_status
                if page.review_minor_suggestions:
                    review_note += "；轻微建议：" + "；".join(page.review_minor_suggestions[:2])
                lines.append(
                    "| "
                    + " | ".join(
                        [
                            str(page.page_number),
                            page.status,
                            _escape_table(review_note),
                            page.failure_ordinal,
                            _escape_table(page.error or _page_review_issue_text(page)),
                            _escape_table(page.source_image_path),
                            _escape_table(page.translated_image_path),
                        ]
                    )
                    + " |"
                )
        lines.append("")
    return "\n".join(lines)


def _escape_table(value: str) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")


def _page_review_issue_text(page: PdfPageRecord) -> str:
    issues = []
    for issue in page.review_issues[:3]:
        if isinstance(issue, dict):
            parts = [
                str(issue.get("location") or "").strip(),
                str(issue.get("problem") or "").strip(),
                str(issue.get("suggestion") or "").strip(),
            ]
            issues.append("；".join(part for part in parts if part))
    return " / ".join(issue for issue in issues if issue)


def _file_record_to_result(record: PdfFileRecord) -> dict[str, Any]:
    success = bool(record.translated_pdf_path)
    detail_parts = []
    if record.translated_pdf_path:
        detail_parts.append(f"高清 {_format_file_size(record.high_quality_pdf_size_bytes)}")
    if record.compressed_pdf_path:
        ratio = _compression_ratio_label(
            record.high_quality_pdf_size_bytes,
            record.compressed_pdf_size_bytes,
        )
        compressed_detail = f"压缩 {_format_file_size(record.compressed_pdf_size_bytes)}"
        if ratio:
            compressed_detail += f"（{ratio}）"
        detail_parts.append(compressed_detail)
    if record.compression_error:
        detail_parts.append("压缩版生成失败")
    if record.placeholder_page_count:
        detail_parts.append(f"失败占位 {record.placeholder_page_count} 页")
    if record.emergency_ratio_normalized_count:
        detail_parts.append(f"应急归一化 {record.emergency_ratio_normalized_count} 页")
    if record.review_failed_page_count:
        detail_parts.append(f"审核未通过 {record.review_failed_page_count} 页")
    if record.review_repaired_page_count:
        detail_parts.append(f"审核修复 {record.review_repaired_page_count} 页")
    return {
        "name": record.name,
        "success": success,
        "status": record.status,
        "detail": "，".join(detail_parts),
        "error": record.error,
        "output": record.translated_pdf_path,
        "compressed_output": record.compressed_pdf_path,
        "page_count": record.page_count,
        "placeholder_page_count": record.placeholder_page_count,
        "emergency_ratio_normalized_count": record.emergency_ratio_normalized_count,
        "review_enabled": record.review_enabled,
        "reviewed_page_count": record.reviewed_page_count,
        "review_passed_page_count": record.review_passed_page_count,
        "review_repaired_page_count": record.review_repaired_page_count,
        "review_failed_page_count": record.review_failed_page_count,
        "review_retry_count": record.review_retry_count,
        "review_minor_suggestion_count": record.review_minor_suggestion_count,
        "high_quality_pdf_size_bytes": record.high_quality_pdf_size_bytes,
        "compressed_pdf_size_bytes": record.compressed_pdf_size_bytes,
    }


def _summary_issues(file_records: list[PdfFileRecord]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for file_record in file_records:
        for page in file_record.pages:
            if not (
                page.placeholder
                or page.emergency_ratio_normalized
                or page.error
                or page.review_status == "failed"
            ):
                continue
            issues.append(
                {
                    "file": file_record.name,
                    "location_label": f"第 {page.page_number} 页",
                    "severity": "needs_review",
                    "problem": page.error or _page_review_issue_text(page) or page.status,
                    "status": (
                        "审核未通过，占位待人工复核"
                        if page.review_status == "failed"
                        else "失败占位页"
                        if page.placeholder
                        else "应急比例归一化"
                        if page.emergency_ratio_normalized
                        else page.status
                    ),
                    "source_image_path": page.source_image_path,
                    "translated_image_path": page.translated_image_path,
                }
            )
    return issues
