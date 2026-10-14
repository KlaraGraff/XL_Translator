"""PDF image-layout translation pipeline and output package helpers."""

from __future__ import annotations

import copy
import json
import os
import queue
import re
import shutil
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any

from loguru import logger
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageStat

from config import (
    PDF_A4_LONG_EDGE_PT,
    PDF_A4_SHORT_EDGE_PT,
    PDF_ASPECT_RATIO_TOLERANCE,
    PDF_COMPRESSED_JPEG_QUALITY_DEFAULT,
    PDF_COMPRESSED_MAX_LONG_EDGE_PX,
    PDF_OVERSIZED_PAGE_EDGE_FACTOR,
    PDF_PAGE_CONCURRENCY_DEFAULT,
    PDF_PAGE_CONCURRENCY_SAFETY_CAP,
    PDF_PAGE_RENDER_AHEAD_COUNT,
    PDF_PAGE_RETRY_ATTEMPTS_DEFAULT,
    PDF_RENDER_DPI_DEFAULT,
    normalize_cloud_base_url,
)
from core import bilingual_writer
from core.runner_base import BaseTaskRunner
from core.output_name_translation import translate_output_stem
from core.file_progress import FileProgressReporter
from core.request_log import PDF_PAGE_WAIT_TEMPLATE, RequestWaitNotice
from core.request_progress import (
    BATCH_EVENT_DONE,
    BATCH_EVENT_PLAN,
    BATCH_EVENT_SENT,
    BatchRequestEvent,
    InFlightRequestTracker,
)
from core.api_concurrency_control import handle_api_concurrency_limit
from core.concurrent_calls import wait_result
from core.api_scheduler import (
    API_REQUEST_CATEGORY_RECOVERY,
    ApiSchedulerAcquireCancelled,
    WeightedApiScheduler,
)
from core.image_generation import (
    ImageGenerationClient,
    ImageModelUnavailableError,
    OpenAICompatibleImageGenerationClient,
    is_model_unavailable_error,
    pdf_page_ratio_tolerance,
)
from core.language_registry import get_supported_languages, get_target_lang_display
from core.model_roles import (
    ROLE_IMAGE,
    ROLE_PDF_REVIEW,
    image_model_signature,
    pdf_review_model_signature,
    provider_supports_capability,
    unsupported_capability_message,
    record_image_model_availability,
    record_pdf_review_model_availability,
    resolve_effective_model_config,
)
from core.surface_rate import resolve_pdf_page_concurrency, resolve_pdf_review_concurrency
from core.pdf_review import (
    OpenAICompatiblePdfReviewClient,
    PdfPageReviewClient,
    PdfReviewIssue,
    PdfPageReviewResult,
    PdfReviewModelUnavailableError,
)
from core.task_logger import TaskLogger, redact_absolute_paths
from core.task_runner import (
    DoneMsg,
    ErrorMsg,
    LogMsg,
    PdfPageRecoveryStatusMsg,
    PdfReviewStatusMsg,
    ProgressMsg,
    StatusMsg,
    StoppedMsg,
    user_facing_reason,
)
from core.process_artifacts import ProcessArtifacts, find_process_dirs
from settings import AppSettings, provider_key_overrides

# 数据模型与结果整理已拆到独立模块，这里原样再导出，保持旧的导入路径可用。
from core.pdf_routes import (
    KIND_BLANK,
    KIND_DRAWING,
    KIND_MIXED,
    KIND_SCAN,
    KIND_TEXT,
    ROUTE_IMAGE,
    ROUTE_INPLACE,
    ROUTE_SKIP,
    ROUTE_WORD,
    classify_document,
    fallback_route,
    page_route,
)
from core.pdf_models import (  # noqa: F401
    SOURCE_TYPE_PDF,
    SOURCE_TYPE_IMAGE,
    SUPPORTED_PDF_SUFFIXES,
    SUPPORTED_IMAGE_SUFFIXES,
    UNSUPPORTED_IMAGE_SUFFIXES,
    SKIP_KIND_IMAGES_DISABLED,
    STOP_WAIT_STATUS_REFRESH_SECONDS,
    IMAGES_DISABLED_SKIP_REASON,
    PDF_PAGES_ROOT,
    SOURCE_PAGES_DIRNAME,
    TRANSLATED_PAGES_DIRNAME,
    REVIEW_CANDIDATES_DIRNAME,
    PDF_REPORT_FILENAME,
    PDF_MANIFEST_FILENAME,
    PDF_OUTPUT_STATE_COMPLETED,
    PDF_OUTPUT_STATE_NEEDS_REVIEW,
    PDF_OUTPUT_STATE_STOPPED,
    PDF_OUTPUT_STATE_FAILED,
    PDF_PAGE_ACTION_REGENERATE,
    PDF_PAGE_ACTION_SKIP,
    PDF_PAGE_IMAGE_STASH_PREFIX,
    PDF_PAGE_IMAGE_PREVIOUS_PREFIX,
    PDF_PAGE_IMAGE_SWAP_PREFIX,
    PDF_PAGE_IMAGE_KIND_SOURCE,
    PDF_PAGE_IMAGE_KIND_TRANSLATED,
    PDF_PAGE_IMAGE_KIND_PREVIOUS,
    PDF_PAGE_STATUS_SKIPPED_OVERSIZE,
    PDF_PAGE_STATUS_STOPPED_UNSTARTED,
    _PDF_PAGE_REGENERABLE_STATUSES,
    _PDF_PAGE_SKIPPABLE_STATUSES,
    PdfFileItem,
    PdfScanSkippedItem,
    PdfScanResult,
    PageQualityResult,
    PdfPageRecord,
    PdfFileRecord,
    PdfTaskSummary,
    _PreparedPdfFile,
)
from core.pdf_task_report import (  # noqa: F401
    _record_is_fully_skipped_oversize,
    _record_translated_nothing,
    fully_skipped_oversize_message,
    translated_nothing_message,
    _page_archive_stem,
    _format_file_size,
    _review_failure_summary,
    _review_feedback_text,
    _completed_pdf_file_count,
    _unfinished_pdf_file_count,
    _completed_image_file_count,
    _unfinished_image_file_count,
    _summary_terminal_reason,
    _summary_terminal_reason_label,
    _pdf_status_label,
    _pdf_file_status_label,
    _pdf_artifact_label,
    _pdf_file_material_paths,
    _record_has_usable_translated_pages,
    _no_usable_translated_pages_error,
    _finished_page_numbers,
    _finished_page_count,
    _last_contiguous_finished_page,
    _page_progress_label,
    _real_generated_page_count,
    _generated_progress_label,
    _unstarted_page_count,
    _last_finished_page_label,
    _summary_to_manifest,
    _summary_to_report,
    _escape_table,
    _page_review_issue_text,
    _file_record_to_result,
    _done_kpi,
    _summary_issues,
)


# --- 页面内容质检的阈值 ------------------------------------------------------
# 这些判据只用来「标记待复核」，永远不会丢页、也不会触发重试，所以全部按
# 「宁可漏判也不误杀」定：命中的必须是任何人一眼都会说「这页坏了」的输出。
#
# 分辨率下限刻意压在 1000×700 而不是 config 里那对 1600×1200 的目标值——
# gpt-image-1 的固定尺寸就是 1024×1536，用目标值当下限会把它每一页都标红。
PDF_QC_LOW_RESOLUTION_LONG_EDGE_PX = 1000
PDF_QC_LOW_RESOLUTION_SHORT_EDGE_PX = 700
# 非背景像素占比：正文页通常 3%~8%，只有插图或标题的稀疏页也有 0.2%~0.5%，
# 0.1% 已经是「整页几乎什么都没有」。再叠加一条亮度标准差 < 3（近乎纯色）
# 才判定，两个条件都满足的正常页实际上不存在。
PDF_QC_BLANK_INK_RATIO = 0.001
PDF_QC_BLANK_STDDEV = 3.0
# 只有源页本身有内容（≥0.5% 非背景像素）时，空白输出才算异常。
PDF_QC_SOURCE_INK_RATIO_FLOOR = 0.005
PDF_QC_INK_LUMA_DELTA = 24
PDF_QC_SAMPLE_LONG_EDGE_PX = 512
# 输出比例与源页差到这个程度就补白边而不是直接拉伸铺满页面。低于它的偏差
# （典型是 gpt-image-2 的 16px 量化，最多约 1.3%）拉伸后肉眼不可见，补边
# 反而多一次重采样。
PDF_RATIO_LETTERBOX_THRESHOLD = 0.02

# 单页渲染的总像素封顶。A4@300dpi 是 8.7MP、A3@300dpi 是 17.4MP，都不受影响；
# A0 图纸@300dpi 是 139MP（pdfium 位图约 558MB + PIL 副本约 418MB），会被自动
# 降到约 160dpi。降级只发生在超限页上，并且会记进日志和页记录。
PDF_PAGE_MAX_RENDER_PIXELS = 40_000_000
PDF_PAGE_MIN_RENDER_DPI = 72.0

_INVALID_FILENAME_FRAGMENT_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


class PdfPageActionError(ValueError):
    """Raised when a single-page review action cannot be accepted."""


def _with_sentence_end(text: str) -> str:
    """给要往后拼安抚句的上游消息补齐句末标点。

    humanize_error 没有模式命中时会原样透传服务商消息（内置成句都带「。」，
    透传的不一定带），直接拼会粘成「余额不足已保留上一版…」这种粘连句。
    """
    if text and text[-1] not in "。！？.!?；;":
        return text + "。"
    return text


@dataclass(frozen=True)
class _PdfPageAction:
    kind: str
    relative_path: str
    page_number: int


def _api_group_signature_from_config(config: Any) -> tuple[str, str, str] | None:
    mode = str(getattr(config, "mode", "") or "").strip()
    if mode != "cloud":
        return None
    provider = str(getattr(config, "provider", "") or "").strip()
    base_url = normalize_cloud_base_url(
        provider,
        str(getattr(config, "base_url", "") or ""),
    ).rstrip("/")
    api_key = str(getattr(config, "api_key", "") or "").strip()
    return ("cloud", base_url, api_key)


def is_supported_pdf_file(path: str | Path) -> bool:
    path = Path(path)
    return (
        path.is_file()
        and path.suffix.lower() in SUPPORTED_PDF_SUFFIXES
        and not path.name.startswith(("~", "."))
    )


def is_supported_image_file(path: str | Path) -> bool:
    path = Path(path)
    return (
        path.is_file()
        and path.suffix.lower() in SUPPORTED_IMAGE_SUFFIXES
        and not path.name.startswith(("~", "."))
    )


def is_supported_pdf_or_image_file(path: str | Path, *, include_images: bool = False) -> bool:
    return is_supported_pdf_file(path) or (include_images and is_supported_image_file(path))


def scan_pdf_path(path: str | Path, *, include_images: bool = False) -> list[PdfFileItem]:
    """Compatibility entry point returning only selectable PDF/image inputs."""
    return scan_pdf_sources(path, include_images=include_images).items


def scan_pdf_sources(path: str | Path, *, include_images: bool = False) -> PdfScanResult:
    """Scan PDF and optional standalone images without hiding bad inputs.

    Generated output and operating-system temporary files are deliberately
    excluded before the source contract.  Files that look selectable but are
    malformed, animated, empty or unsupported remain in ``skipped`` with a
    reason so a valid neighbour never gets blocked by them.
    """
    source = Path(path).expanduser()
    root = source if source.is_dir() else source.parent
    result = PdfScanResult(root=root, include_images=bool(include_images))
    if not source.exists():
        reason = f"路径不存在：{source}"
        logger.warning(reason)
        result.skipped.append(PdfScanSkippedItem(source, source.name, reason))
        return result

    if source.is_file():
        if not _should_skip_scanned_input(Path(source.name)):
            _scan_one_pdf_source(source, source.parent, result)
    elif source.is_dir():
        for candidate in sorted(source.rglob("*")):
            if not candidate.is_file():
                continue
            relative = _relative_scan_path(candidate, source)
            if _should_skip_scanned_input(Path(relative)):
                continue
            suffix = candidate.suffix.lower()
            if suffix in SUPPORTED_PDF_SUFFIXES:
                _scan_one_pdf_source(candidate, source, result)
                continue
            if suffix in SUPPORTED_IMAGE_SUFFIXES or suffix in UNSUPPORTED_IMAGE_SUFFIXES:
                if not include_images:
                    result.skipped.append(
                        PdfScanSkippedItem(
                            candidate,
                            relative,
                            IMAGES_DISABLED_SKIP_REASON,
                            suffix.lstrip("."),
                            SOURCE_TYPE_IMAGE,
                            SKIP_KIND_IMAGES_DISABLED,
                        )
                    )
                    continue
                if suffix in SUPPORTED_IMAGE_SUFFIXES:
                    _scan_one_pdf_source(candidate, source, result)
                    continue
                result.skipped.append(
                    PdfScanSkippedItem(
                        candidate,
                        relative,
                        "不支持的图片格式；可选择 PNG、JPG/JPEG、WebP、BMP、TIF/TIFF。",
                        suffix.lstrip("."),
                        SOURCE_TYPE_IMAGE,
                    )
                )
                continue
    else:
        result.skipped.append(
            PdfScanSkippedItem(source, source.name, f"路径既不是文件也不是目录：{source}")
        )

    result.items.sort(key=lambda item: str(item.path))
    result.skipped.sort(key=lambda item: str(item.path))
    logger.info(
        "PDF/图片扫描完成：{}，可选 {}，跳过 {}，图片输入 {}".format(
            source,
            len(result.items),
            len(result.skipped),
            "开启" if include_images else "关闭",
        )
    )
    return result


def _scan_one_pdf_source(path: Path, root: Path, result: PdfScanResult) -> None:
    relative = _relative_scan_path(path, root)
    suffix = path.suffix.lower()
    source_type = SOURCE_TYPE_PDF if suffix in SUPPORTED_PDF_SUFFIXES else SOURCE_TYPE_IMAGE
    supported = is_supported_pdf_file(path) or (
        result.include_images and is_supported_image_file(path)
    )
    if (
        not supported
        and source_type == SOURCE_TYPE_IMAGE
        and not result.include_images
        and (suffix in SUPPORTED_IMAGE_SUFFIXES or suffix in UNSUPPORTED_IMAGE_SUFFIXES)
    ):
        # 直接选中一个图片文件、而开关关着：原因是开关，不是「文件不受支持」。
        result.skipped.append(
            PdfScanSkippedItem(
                path,
                relative,
                IMAGES_DISABLED_SKIP_REASON,
                suffix.lstrip("."),
                source_type,
                SKIP_KIND_IMAGES_DISABLED,
            )
        )
        return
    if not supported:
        result.skipped.append(
            PdfScanSkippedItem(
                path,
                relative,
                "不是受支持的 PDF 或图片文件，或为系统临时文件。",
                suffix.lstrip("."),
                source_type,
            )
        )
        return
    try:
        item = (
            _build_pdf_file_item(path, root=root)
            if source_type == SOURCE_TYPE_PDF
            else _build_image_file_item(path, root=root)
        )
        result.items.append(item)
    except Exception as exc:  # noqa: BLE001 - one broken input must not block its peers.
        reason = f"读取失败：{user_facing_reason(exc, fallback='无法打开此文件。')}"
        logger.warning(f"扫描 PDF/图片失败 {path.name}：{exc}")
        result.skipped.append(
            PdfScanSkippedItem(path, relative, reason, suffix.lstrip("."), source_type)
        )


def _relative_scan_path(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return path.name


def page_image_name(page_number: int, total_pages: int, *, failed: bool = False) -> str:
    width = max(3, len(str(max(1, int(total_pages or 1)))))
    suffix = "_failed" if failed else ""
    return f"page_{int(page_number):0{width}d}{suffix}.png"


def is_oversized_page(page_width_pt: float, page_height_pt: float) -> bool:
    """判断一页是否是「跳过大幅面页」要抓的超大页（A3 及以上）。

    这是纯尺寸判定，跟页面内容是不是图纸无关——只是工程 PDF 里超出 A4 的页
    绝大多数确实是 CAD 图纸，这也是设这道尺寸门槛的原始动机。

    比较规则是长边比长边、短边比短边，绝不能直接比宽比高——横版 A4
    （842×595pt）宽度比竖版 A4 的高度还大，直接比宽会把横版 A4 正文页
    误判成超大页。

    传入的 page_width_pt / page_height_pt 应该来自 pypdfium2 的
    page.get_size()：实测过它已经把 /Rotate 和 CropBox 都换算进去了
    （旋转 90°的 A3 会直接返回宽高互换后的尺寸），这里不需要再单独查
    /Rotate 归一化一次。
    """
    long_edge_pt = max(page_width_pt, page_height_pt)
    short_edge_pt = min(page_width_pt, page_height_pt)
    return (
        long_edge_pt > PDF_A4_LONG_EDGE_PT * PDF_OVERSIZED_PAGE_EDGE_FACTOR
        and short_edge_pt > PDF_A4_SHORT_EDGE_PT * PDF_OVERSIZED_PAGE_EDGE_FACTOR
    )


# 「从多大算大幅面」：A3 沿用原来的判定（长短边都超出 A4 约 15%），更大的档按
# 纸张标准尺寸的九成算，扫描白边、裁切误差不会把 A1 判成 A2。
_PAPER_EDGES_MM = {"A2": (420.0, 594.0), "A1": (594.0, 841.0), "A0": (841.0, 1189.0)}


def is_oversized_for_threshold(threshold: str, page_width_pt: float, page_height_pt: float) -> bool:
    if threshold not in _PAPER_EDGES_MM:
        return is_oversized_page(page_width_pt, page_height_pt)
    short_mm, long_mm = _PAPER_EDGES_MM[threshold]
    long_pt = max(page_width_pt, page_height_pt)
    short_pt = min(page_width_pt, page_height_pt)
    return long_pt >= long_mm / 25.4 * 72 * 0.9 and short_pt >= short_mm / 25.4 * 72 * 0.9


def _page_has_text_layer(page, source_pdf: str = "", page_index: int = -1) -> bool:
    """A page whose words can be read directly (at least a few visible letters).

    A scanner's "searchable PDF" carries an invisible text layer over the scan
    image; those words cannot be replaced in place (the visible ones are
    pixels), so invisible text (render mode 3) does not count.
    """
    try:
        textpage = page.get_textpage()
        try:
            text = textpage.get_text_range() or ""
        finally:
            textpage.close()
    except Exception:  # noqa: BLE001 - unreadable text layer counts as none
        return False
    if sum(1 for ch in text if ch.isalpha()) < 20:
        return False
    if not source_pdf or page_index < 0:
        return True
    try:
        from core.pdf_component import require_fitz

        fitz = require_fitz()

        with fitz.open(source_pdf) as doc:
            visible = sum(len(span.get("chars", ())) for span in doc[page_index].get_texttrace()
                          if span.get("type") != 3 and span.get("opacity", 1) > 0)
    except Exception:  # noqa: BLE001 - cannot tell: let the in-place QA decide
        return True
    return visible >= 20


def _pt_to_mm(value_pt: float) -> float:
    return value_pt / 72.0 * 25.4


def check_page_quality(
    image_bytes: bytes,
    *,
    source_width: int,
    source_height: int,
    ratio_tolerance: float = PDF_ASPECT_RATIO_TOLERANCE,
    source_image_path: Path | str | None = None,
) -> PageQualityResult:
    """Gate one model-returned page image.

    Two very different kinds of verdict come out of here:

    * ``ok=False`` — the image is structurally unusable (undecodable, or a
      shape the model was never asked for).  The caller retries.
    * ``ok=True`` with ``review_reasons`` — the image is usable but suspicious
      on content (near-blank, or far below any readable resolution).  These are
      heuristics, so they must never trigger a retry or drop the page: the page
      ships and the file is marked needs-review.

    ``ratio_tolerance`` must come from
    :func:`core.image_generation.pdf_page_ratio_tolerance` for the model in
    play — a fixed tolerance and a fixed request size are how you build a gate
    that nothing can pass.
    """
    try:
        with Image.open(BytesIO(image_bytes)) as image:
            image.load()
            width, height = image.size
            ink_ratio, luma_stddev = _measure_page_ink(image)
    except Exception as exc:  # noqa: BLE001 - user-facing QC result.
        logger.debug(f"[PDF] 译图解码失败原始错误：{exc!r}")
        return PageQualityResult(
            False,
            "decode_error",
            "服务返回的页面不是有效图片，无法解码。",
        )

    source_ratio = _safe_ratio(source_width, source_height)
    output_ratio = _safe_ratio(width, height)
    if source_ratio <= 0 or output_ratio <= 0:
        return PageQualityResult(False, "ratio_error", "无法计算译图的长宽比", width, height)
    if abs(source_ratio - output_ratio) / source_ratio > ratio_tolerance:
        return PageQualityResult(
            False,
            "ratio_error",
            f"译图与原页的长宽比相差过大（超过 {ratio_tolerance:.1%}）",
            width,
            height,
        )

    reasons: list[str] = []
    messages: list[str] = []
    if (
        min(width, height) < PDF_QC_LOW_RESOLUTION_SHORT_EDGE_PX
        or max(width, height) < PDF_QC_LOW_RESOLUTION_LONG_EDGE_PX
    ):
        reasons.append("low_resolution")
        messages.append(
            f"译图尺寸过小（{width}×{height} 像素），文字可能无法辨认"
        )

    if ink_ratio < PDF_QC_BLANK_INK_RATIO and luma_stddev < PDF_QC_BLANK_STDDEV:
        source_ink = _measure_source_ink_ratio(source_image_path)
        # 源页本身就是空白页时，输出空白是正确结果，绝不能报警。只有在源页
        # 确实有内容、输出却几乎全空时才判「疑似空白」。源页不可读（取不到
        # 比例）时按「不确定」处理，同样不报警。
        if source_ink is not None and source_ink >= PDF_QC_SOURCE_INK_RATIO_FLOOR:
            reasons.append("blank_page")
            messages.append(
                "译图几乎为空白，而原页有内容，可能存在内容丢失"
            )

    return PageQualityResult(
        True,
        "ok",
        "",
        width,
        height,
        review_reasons=reasons,
        review_message="；".join(messages),
    )


def _measure_page_ink(image: Image.Image) -> tuple[float, float]:
    """Return (non-background pixel ratio, luminance stddev) of one page image.

    Downsampled first: this runs on every returned page and the answer only
    needs to separate "a page with text on it" from "a blank sheet".
    """
    try:
        gray = image.convert("L")
        gray.thumbnail(
            (PDF_QC_SAMPLE_LONG_EDGE_PX, PDF_QC_SAMPLE_LONG_EDGE_PX),
            Image.Resampling.BILINEAR,
        )
        histogram = gray.histogram()[:256]
        total = sum(histogram)
        if total <= 0:
            return 1.0, 255.0
        background_level = histogram.index(max(histogram))
        ink = sum(
            count
            for level, count in enumerate(histogram)
            if abs(level - background_level) > PDF_QC_INK_LUMA_DELTA
        )
        stddev = ImageStat.Stat(gray).stddev[0]
        return ink / total, float(stddev)
    except Exception:  # noqa: BLE001 - QC must never fail the page on its own.
        # 量不出来就当作「有内容」，宁可漏判也不误杀。
        return 1.0, 255.0


def _measure_source_ink_ratio(source_image_path: Path | str | None) -> float | None:
    if not source_image_path:
        return None
    try:
        with Image.open(Path(source_image_path)) as image:
            image.load()
            ink_ratio, _ = _measure_page_ink(image)
        return ink_ratio
    except Exception:  # noqa: BLE001 - missing source page just means "unknown".
        return None


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
    red = (178, 34, 34)
    dark = (15, 23, 42)
    muted = (71, 85, 105)
    scale = max(1.0, min(canvas.width / 1200, canvas.height / 1600))
    title_font = _load_placeholder_font(int(max(42, min(72, 42 * scale))), bold=True)
    subtitle_font = _load_placeholder_font(int(max(26, min(40, 26 * scale))))
    label_font = _load_placeholder_font(int(max(28, min(46, 28 * scale))), bold=True)
    body_font = _load_placeholder_font(int(max(26, min(44, 26 * scale))))
    path_font = _load_placeholder_font(int(max(22, min(36, 22 * scale))))
    margin = int(max(72, min(150, canvas.width * 0.055)))
    border = int(max(6, min(12, canvas.width * 0.004)))
    bar_height = int(max(128, min(220, canvas.height * 0.055)))
    draw.rectangle((0, 0, canvas.width, bar_height), fill=red)
    draw.rectangle(
        (margin // 2, margin // 2, canvas.width - margin // 2, canvas.height - margin // 2),
        outline=red,
        width=border,
    )
    title_block_height = _font_line_height(title_font) + _font_line_height(subtitle_font) + 8
    title_y = max(26, (bar_height - title_block_height) // 2)
    draw.text((margin, title_y), "PDF 页面翻译需复核", fill="white", font=title_font)
    draw.text(
        (margin, title_y + _font_line_height(title_font) + 8),
        "本页未能生成可用译图，系统已放置失败占位页。",
        fill=(255, 241, 242),
        font=subtitle_font,
    )

    x = margin
    y = bar_height + int(36 * scale)
    max_text_width = canvas.width - margin * 2
    fields = [
        ("页码", f"第 {page_number} 页", body_font),
        ("失败页编号", failure_ordinal, body_font),
        ("问题描述", _localized_pdf_placeholder_problem(error_summary), body_font),
        (
            "处理建议",
            "请稍后重试这一页，或检查 PDF 与图片翻译模型的 API 密钥、接口地址和服务商状态。",
            body_font,
        ),
        ("源页图像", str(source_image_path), path_font),
        ("占位页图像", str(placeholder), path_font),
    ]
    for label, value, value_font in fields:
        draw.text((x, y), f"{label}：", fill=red, font=label_font)
        y += _font_line_height(label_font) + int(8 * scale)
        for wrapped in _wrap_text_to_width(str(value), max_width=max_text_width, font=value_font, draw=draw):
            draw.text((x, y), wrapped, fill=dark if value_font is body_font else muted, font=value_font)
            y += _font_line_height(value_font) + int(4 * scale)
        y += int(18 * scale)
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
    source_stem = _sanitize_filename_fragment(source_path.stem)
    suffix = f"_{_sanitize_filename_fragment(variant_label)}" if variant_label else ""
    # 命名从前缀式「译文(语言)_原文名」改成后缀式「原文名_语言」：前缀式会让
    # 所有译文在 Finder / 资源管理器按名排序时统统排到最后，跟各自的原文完全
    # 脱节；语言标签挪到原文名主干后面，译文才能紧挨着原文排在一起，方便配对。
    return f"{source_stem}_{label}{suffix}{source_path.suffix}"


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
    # 高清/主产物使用无变体后缀的稳定名称；旧版本的 `_高清` 文件仍由
    # 续译检测兼容识别，但新生成结果不再额外增加“高清”后缀。
    high_base = target_dir / translated_pdf_base_name(
        source_pdf_name,
        target_lang,
        settings,
        variant_label=None,
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
        # Migrate the former primary name once: keep the old `_高清` artifact as
        # a revision, then let the new suffix-free primary name be created.
        legacy_high = target_dir / translated_pdf_base_name(
            source_pdf_name, target_lang, settings, variant_label="高清"
        )
        if legacy_high.exists() and not high_base.exists():
            legacy_r1 = legacy_high.with_name(f"{legacy_high.stem}_R1{legacy_high.suffix}")
            if not legacy_r1.exists():
                legacy_high.rename(legacy_r1)
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


def translated_image_base_name(
    source_image_name: str,
    target_lang: str,
    settings: AppSettings,
    *,
    output_suffix: str,
) -> str:
    label = _sanitize_filename_fragment(
        get_target_lang_display(
            target_lang,
            settings.custom_target_langs,
            include_optional=True,
        )
    )
    source_path = Path(source_image_name)
    source_stem = _sanitize_filename_fragment(source_path.stem)
    suffix = output_suffix if output_suffix.startswith(".") else f".{output_suffix}"
    # 同 translated_pdf_base_name：改成后缀式，让译文在文件管理器里紧挨着原文排序。
    return f"{source_stem}_{label}{suffix}"


def resolve_translated_image_path(
    target_dir: Path,
    source_image_name: str,
    target_lang: str,
    settings: AppSettings,
    *,
    app_managed: bool,
    output_suffix: str,
) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    base_name = translated_image_base_name(
        source_image_name,
        target_lang,
        settings,
        output_suffix=output_suffix,
    )
    base_path = target_dir / base_name
    stem = base_path.stem
    suffix = base_path.suffix
    if app_managed and base_path.exists():
        r1_path = target_dir / f"{stem}_R1{suffix}"
        if not r1_path.exists():
            base_path.rename(r1_path)
    if not base_path.exists() and not _has_revision_files(target_dir, stem, suffix):
        return base_path
    next_revision = _next_revision_number(target_dir, stem, suffix)
    return target_dir / f"{stem}_R{next_revision}{suffix}"


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
    if any(_record_needs_review(record) for record in file_records):
        return PDF_OUTPUT_STATE_NEEDS_REVIEW
    return PDF_OUTPUT_STATE_COMPLETED


def _inplace_nothing_written_message(result: Any, *, ocr_missing: bool = False) -> str:
    """原位替换一处都没写：照实说认出了什么、为什么没换，不能笼统说「没有找到文字」。"""
    items = [item for item in (getattr(result, "review_items", None) or []) if item.get("kind") != "term"]
    kept = int(getattr(result, "kept", 0) or 0)
    if items:
        counts = Counter(str(item.get("reason") or "未能确认") for item in items)
        if len(counts) == 1:
            reasons = next(iter(counts))
        else:
            reasons = "、".join(reason for reason, _count in counts.most_common())
        message = f"识别到 {len(items)} 处文字，均未替换（{reasons}）"
        if kept:
            message += f"；另有 {kept} 处数字和编号等不需要翻译"
        return message + "，这一页保留原样"
    if kept:
        return f"识别到的 {kept} 处都是数字和编号等不需要翻译的内容，这一页保留原样"
    if ocr_missing:
        return "没有找到可以替换的文字（没有安装文字识别组件，画成线条的字未识别），这一页保留原样"
    return "没有找到可以替换的文字，这一页保留原样"


def _record_needs_review(record: PdfFileRecord) -> bool:
    return bool(
        record.placeholder_page_count
        or record.emergency_ratio_normalized_count
        or record.review_failed_page_count
        or record.quality_flagged_page_count
        or _record_is_fully_skipped_oversize(record)
        or _record_translated_nothing(record)
        # 原位替换没成功（出错、自检不过、中途停止）的页，输出里是原页，要提醒复核。
        or any(page.inplace_requested and page.inplace_status in {"kept_original", "stopped"}
               for page in record.pages)
        # 原位替换的页里有接口放弃、没拿到译文的文字（与 CAD「未获得译文」一样要复核）。
        or any(page.inplace_untranslated for page in record.pages)
        # 要整页图像翻译、但没有设置图像模型而保留原样的页
        or any(page.kept_reason for page in record.pages)
        # 转 Word 翻译后 Word 流程报出的待复核处
        or record.word_issue_count
        # 首选方式用不了、改用了保底方式的页
        or any(page.route_note for page in record.pages)
    )


def _with_stop(translate, should_stop):
    """Hand the task's stop signal to the text engine when the hook accepts it."""
    if translate is None:
        return None

    def wrapped(texts, prompt, **extra):
        try:
            return translate(texts, prompt, should_stop=should_stop, **extra)
        except TypeError:
            return translate(texts, prompt, **extra)

    return wrapped


_DOCUMENT_KIND_LABELS = {
    KIND_TEXT: "文字文档",
    KIND_DRAWING: "图纸",
    KIND_SCAN: "扫描件",
    KIND_MIXED: "图文混合文件",
}


def max_page_generation_attempts(retry_count: int | None) -> int:
    """Convert the user-facing retry count into total attempts for one page."""
    try:
        retries = int(retry_count or 0)
    except (TypeError, ValueError):
        retries = PDF_PAGE_RETRY_ATTEMPTS_DEFAULT
    return max(1, retries + 1)


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


def write_pdf_manifest_and_report(
    summary: PdfTaskSummary,
    *,
    artifact_dir: str | Path | None = None,
) -> tuple[Path, Path]:
    """Write run metadata outside the user's delivery directory when requested."""
    output_dir = Path(artifact_dir) if artifact_dir is not None else Path(summary.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / PDF_MANIFEST_FILENAME
    report_path = output_dir / PDF_REPORT_FILENAME
    manifest_path.write_text(
        json.dumps(_summary_to_manifest(summary), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    report_path.write_text(_summary_to_report(summary), encoding="utf-8")
    return manifest_path, report_path


# --- 接着上次继续：读上一次输出目录的页存档 -------------------------------------------
# 只认「明确成功」这一个状态。中止时已入队未开跑的页会被烧成「图像生成失败」占位页
# （零模型调用就进了上一份产物），复用它们等于把空白页永久固化成译文；同理，本地质检
# 有标记、审核判失败、走了应急比例归一化的页虽然进得了上一份交付，也一律重新生成——
# 它们值不值得原样搬进这一份是另一回事，宁可多花一次调用。
PDF_RESUME_REUSABLE_PAGE_STATUSES = frozenset({"success"})


@dataclass(frozen=True)
class ResumePageSource:
    """One page of a previous run that may be adopted without re-translating."""

    page_number: int
    translated_image_path: Path
    source_image_path: Path | None
    page_width_pt: float
    page_height_pt: float
    source_width_px: int
    source_height_px: int
    output_width_px: int
    output_height_px: int
    render_dpi: float
    ratio_letterboxed: bool


def _resume_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _resume_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def read_pdf_resume_manifest(resume_dir: str | Path) -> dict[str, Any] | None:
    """Read the manifest of a previous output package, or ``None`` if unusable.

    Every caller degrades to a normal full translation on ``None``: a resume
    directory that was moved, emptied or half-written is a reason to translate
    everything again, never a reason to fail the task.
    """
    try:
        path = Path(resume_dir)
    except TypeError:
        return None
    if not path.is_dir():
        return None
    manifest_dir = path
    # New runs keep metadata and page archives in the process log directory;
    # retain the legacy direct-child lookup for older packages.
    manifest_path = manifest_dir / PDF_MANIFEST_FILENAME
    if not manifest_path.is_file():
        try:
            candidates = find_process_dirs(path, kind="pdf")
        except Exception:  # pragma: no cover - discovery is best effort
            candidates = []
        for candidate in candidates:
            if (candidate / PDF_MANIFEST_FILENAME).is_file():
                manifest_dir = candidate
                manifest_path = candidate / PDF_MANIFEST_FILENAME
                break
    if not manifest_path.is_file():
        return None
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - an unreadable manifest is just "no resume".
        logger.debug(f"[PDF] 读取上次输出清单失败原始错误：{path}：{exc!r}")
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("files"), list):
        return None
    payload["_resume_process_dir"] = str(manifest_dir)
    return payload


def _resume_manifest_files(manifest: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Index a manifest's file records by POSIX relative path."""
    files: dict[str, dict[str, Any]] = {}
    if not isinstance(manifest, dict):
        return files
    for entry in manifest.get("files") or []:
        if not isinstance(entry, dict):
            continue
        relative = str(entry.get("relative_path") or "").strip()
        if not relative:
            continue
        files[Path(relative).as_posix()] = entry
    return files


def _resume_page_is_reusable(page: dict[str, Any]) -> bool:
    return bool(
        isinstance(page, dict)
        and page.get("status") in PDF_RESUME_REUSABLE_PAGE_STATUSES
        and not page.get("placeholder")
        and not page.get("skipped_oversize")
        and page.get("review_status") != "failed"
        and not page.get("quality_flags")
    )


def _resume_page_image_candidates(
    archive_dir: Path,
    recorded_path: str,
    page_number: int,
    page_count: int,
) -> list[Path]:
    """Page images to try, all constrained to the previous archive folder.

    The manifest's own path is tried first but only counts while it still points
    inside that folder — a manifest is user-writable data, and a moved package
    leaves stale absolute paths behind either way.
    """
    candidates: list[Path] = []
    recorded = str(recorded_path or "").strip()
    if recorded:
        candidates.append(Path(recorded))
    base_name = page_image_name(page_number, page_count)
    candidates.append(archive_dir / base_name)
    if archive_dir.is_dir():
        candidates.extend(sorted(archive_dir.glob(f"{base_name.removesuffix('.png')}.*")))
    seen: set[Path] = set()
    usable: list[Path] = []
    for candidate in candidates:
        if candidate in seen or "_failed" in candidate.name:
            continue
        seen.add(candidate)
        if _path_is_within(candidate, archive_dir):
            usable.append(candidate)
    return usable


def _resume_pick_page_image(
    candidates: list[Path],
    *,
    verify: bool,
) -> tuple[Path, int, int] | None:
    """Return the first candidate that really is a readable image, with its size."""
    for candidate in candidates:
        try:
            if not candidate.is_file() or candidate.stat().st_size <= 0:
                continue
            with Image.open(candidate) as image:
                width, height = image.size
                if verify:
                    # 截断/损坏的 PNG 在 open() 时不会报错，只有真正读完才会。
                    image.verify()
            return candidate, int(width), int(height)
        except Exception as exc:  # noqa: BLE001 - a broken page image is just not reusable.
            logger.debug(f"[PDF] 上次输出的页图不可用：{candidate}：{exc!r}")
    return None


def reusable_pdf_pages(
    manifest: dict[str, Any] | None,
    resume_dir: str | Path,
    *,
    verify_images: bool = True,
) -> dict[str, list[ResumePageSource]]:
    """Per-file pages of a previous run that may be adopted as-is.

    Keys are POSIX relative paths as recorded in the manifest.  Anything that
    cannot be verified — a page image that is missing, unreadable or outside the
    archive folder, missing page geometry — drops that one page, never the file.
    ``verify_images=False`` skips the full-decode check for callers that only
    need a count (scan-time detection) rather than pages they are about to bind.
    """
    resume_path = (
        Path(str(manifest.get("_resume_process_dir") or resume_dir))
        if isinstance(manifest, dict)
        else Path(resume_dir)
    )
    reusable: dict[str, list[ResumePageSource]] = {}
    for relative, entry in _resume_manifest_files(manifest).items():
        page_count = _resume_int(entry.get("page_count"))
        if page_count <= 0:
            continue
        is_image = str(entry.get("source_type") or SOURCE_TYPE_PDF) == SOURCE_TYPE_IMAGE
        source_dir, translated_dir = resolve_pdf_page_archive_dirs(resume_path, Path(relative))
        pages: list[ResumePageSource] = []
        seen_numbers: set[int] = set()
        for page in entry.get("pages") or []:
            if not _resume_page_is_reusable(page):
                continue
            page_number = _resume_int(page.get("page_number"))
            if page_number < 1 or page_number > page_count or page_number in seen_numbers:
                continue
            width_pt = _resume_float(page.get("page_width_pt"))
            height_pt = _resume_float(page.get("page_height_pt"))
            if not is_image and (width_pt <= 0 or height_pt <= 0):
                # 装订按 pt 尺寸建页，没有尺寸的页复用出来就是一张 0×0 的废页。
                continue
            picked = _resume_pick_page_image(
                _resume_page_image_candidates(
                    translated_dir,
                    str(page.get("translated_image_path") or ""),
                    page_number,
                    page_count,
                ),
                verify=verify_images,
            )
            if picked is None:
                continue
            image_path, image_width, image_height = picked
            source_image = next(
                (
                    candidate
                    for candidate in _resume_page_image_candidates(
                        source_dir,
                        str(page.get("source_image_path") or ""),
                        page_number,
                        page_count,
                    )
                    if candidate.is_file()
                ),
                None,
            )
            pages.append(
                ResumePageSource(
                    page_number=page_number,
                    translated_image_path=image_path,
                    source_image_path=source_image,
                    page_width_pt=width_pt,
                    page_height_pt=height_pt,
                    source_width_px=_resume_int(page.get("source_width_px")),
                    source_height_px=_resume_int(page.get("source_height_px")),
                    output_width_px=_resume_int(page.get("output_width_px")) or image_width,
                    output_height_px=_resume_int(page.get("output_height_px")) or image_height,
                    render_dpi=_resume_float(page.get("render_dpi")),
                    ratio_letterboxed=bool(page.get("ratio_letterboxed")),
                )
            )
            seen_numbers.add(page_number)
        if pages:
            reusable[relative] = sorted(pages, key=lambda item: item.page_number)
    return reusable


class PdfImageTranslationRunner(BaseTaskRunner):
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
        key_overrides: dict[str, str] | None = None,
        api_scheduler: WeightedApiScheduler | None = None,
        review_api_scheduler: WeightedApiScheduler | None = None,
        resume_output_dir: str | None = None,
        profile_requester: Callable[[list[str], Callable[[str, str], None]], str] | None = None,
        profile_concurrency: int = 1,
        inplace_hooks: Any = None,
    ) -> None:
        self._files = file_items
        self._settings = settings
        # 「原位替换文字（Beta）」用到的翻译回调、Tesseract 路径；不开这个功能时为 None。
        self._inplace_hooks = inplace_hooks
        self._pdf_component_ok: bool | None = None
        # 转 Word 翻译的各段正文的「记忆库」结果；收尾时和原位替换页的合并（_terms_result）。
        self._word_terms: list[dict[str, Any]] = []
        # 「自动识别场景」：读 PDF 文字层、请模型写一段文档说明的回调（见 core.document_profile）。
        # 不给就不做；每个文件的说明按源路径存在 _document_contexts 里，逐页带进翻译要求。
        self._profile_requester = profile_requester
        # 各文件的文档说明互不依赖，按文档翻译模型的并发设置同时请求。
        self._profile_concurrency = max(1, int(profile_concurrency or 1))
        self._document_contexts: dict[str, str] = {}
        self._source_root = Path(source_root) if source_root else None
        # 「接着上次继续」选中的历史输出目录。只读：本次任务写的永远是自己的新目录。
        self._resume_output_dir = str(resume_output_dir).strip() if resume_output_dir else ""
        self._image_client = image_client or OpenAICompatibleImageGenerationClient()
        self._review_client = review_client or OpenAICompatiblePdfReviewClient()
        self._key_overrides = dict(key_overrides or {})
        self._api_scheduler_override = api_scheduler
        self._review_api_scheduler_override = review_api_scheduler
        self._queue: queue.Queue = queue.Queue()
        self._file_progress = FileProgressReporter(
            self._queue,
            self._files,
            self._source_root,
        )
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._stop_reason = ""
        self._task_logger = TaskLogger(enabled=task_logger_enabled)
        # 这三个计数器由每个页面工作线程自增，`+=` 不是原子操作（读—加—写三步），
        # 并发跑页时丢计数是必然的，报告里的「接口调用次数」会比实际少。
        self._counter_lock = threading.Lock()
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
        self._page_status_lock = threading.Lock()
        # 每页从第一次真正发出请求算起的用时（写进这一页的结果日志），以及久等提示用的
        # 在途记账。和 Excel、Word、CAD 的逐批日志同一套（core.request_log）。
        self._page_requests = InFlightRequestTracker()
        self._page_started: dict[str, tuple[int, float]] = {}
        self._page_request_seq = 0
        self._total_page_count = 0
        self._completed_page_count = 0
        self._submitted_page_count = 0
        self._retrying_pages: set[str] = set()
        self._retried_pages: set[str] = set()
        self._recovered_pages: set[str] = set()
        self._placeholder_pages: set[str] = set()
        # 「安全停止」之后的等待计时。在飞的那几页要等上游返回或读超时，实测能到四分钟；
        # 状态行必须自己往前走，否则一句不动的话看起来像卡死。
        self._stop_wait_started_at: float | None = None
        self._stop_wait_last_emit: float = 0.0
        # Per-page review panel state.  The prepared files are published here
        # so a reader thread can build a snapshot without touching ``_run``
        # locals, and page actions are queued here until the single-threaded
        # pause boundary applies them.
        self._page_action_lock = threading.Lock()
        self._prepared_files: list[_PreparedPdfFile] = []
        self._pending_page_actions: list[_PdfPageAction] = []
        self._user_skipped_pages: set[tuple[str, int]] = set()
        # Single-page rerun of a task that already ended (see ``rerun_page``).
        # ``_run`` is over by then, so the rerun needs its own thread and its
        # own copy of everything that used to be a ``_run`` local.
        self._rerun_lock = threading.Lock()
        self._rerun_thread: threading.Thread | None = None
        self._rerun_page_key: tuple[str, int] | None = None
        self._rerun_error = ""
        # 「换回上一版」占的是同一把忙锁（同一份输出文件不能被两件事同时重装），
        # 但单独记一个键：它不调用模型、也没有重生成的进度可报，混进
        # ``page_rerun_state`` 会让界面写出「第 N 页正在重新生成」。
        self._restore_page_key: tuple[str, int] | None = None
        self._finished_output_dir: Path | None = None
        self._finished_started_at: datetime | None = None
        self._finished_elapsed_sec = 0.0
        self._finished_stopped = False
        self._finished_stop_reason = ""
        self._finished_fatal_error = ""
        self._process_artifacts: ProcessArtifacts | None = None
        self._process_work_dir: Path | None = None
        self._result_patch: dict[str, Any] = {}
        self._output_name_engine = None
        self._output_name_engine_failed = False
        self._translated_pdf_names: dict[str, str] = {}
        # 翻译方式（Beta 分支，见 core.pdf_routes）：每份 PDF 读文件时的判断，以及
        # 「没有设置图像翻译模型」——翻译方式不是整页图像翻译时任务照样能跑，只是
        # 需要整页图像翻译的页保留原样。
        self._document_kinds: dict[str, Any] = {}
        self._image_model_missing = ""
        # 本机把 Word 转回 PDF 用的程序（"word" / "libreoffice" / ""）；None 表示还没查。
        self._office_pdf_engine: str | None = None
        self._office_pdf_message = ""
        # 自动组合里转 Word 的正文页：(源文件路径, 页码) → {segment, status, message}。
        # 在页面翻译开始前就转好（见 _run_word_segments），失败的页随后按保底方式处理。
        self._word_pages: dict[tuple[str, int], dict[str, Any]] = {}

    @property
    def task_id(self) -> str:
        return self._task_logger.task_id

    def _before_start(self) -> None:
        self._pause_event.clear()
        self._stop_reason = ""

    def stop(self) -> None:
        """Request a normal terminal stop after currently submitted pages settle."""
        if not self._stop_reason:
            self._stop_reason = "user_stopped"
        self._stop_event.set()

    def end_paused(self) -> None:
        """End a paused task while preserving its partial output package."""
        if not self._pause_event.is_set() or self._stop_event.is_set():
            return
        self._stop_reason = "end_paused"
        self._pause_event.clear()
        self._stop_event.set()

    def pause(self) -> None:
        """Stop adding new pages while completed submissions settle safely."""
        if self._stop_event.is_set():
            return
        self._pause_event.set()
        self._log("INFO", "正在暂停：不再开始新的页面，正在等待处理中的页面完成。")

    def _wait_while_paused(self) -> bool:
        """Return false when pause turned into a terminal stop request."""
        if not self._pause_event.is_set():
            return not self._stop_event.is_set()
        self._queue.put(StatusMsg("状态：已暂停，可以继续翻译或结束任务。"))
        while self._pause_event.is_set() and not self._stop_event.is_set():
            time.sleep(0.05)
        return not self._stop_event.is_set()

    def resume(self) -> None:
        if self._stop_event.is_set() and self._stop_reason != "end_paused":
            # Preserve the runner-level soft-stop/resume behavior used by
            # headless callers. API end-paused is terminal and cannot take
            # this branch.
            self._stop_event.clear()
            self._pause_event.clear()
            self._stop_reason = ""
            self._log("INFO", "已继续翻译剩余页面。")
        elif self._pause_event.is_set() and not self._stop_event.is_set():
            self._pause_event.clear()
            self._log("INFO", "已继续翻译剩余页面。")

    # ---- per-page review panel -------------------------------------------------
    #
    # The panel needs a page-level view that would bloat the SSE event log if it
    # were pushed, so it is pulled instead.  Reads are lock-guarded snapshots and
    # never hand out filesystem paths; page actions are queued and applied at the
    # pause boundary in ``_run``, which is the only moment no page future exists.

    def pdf_page_snapshot(self) -> dict[str, Any]:
        """Return a JSON-safe per-file, per-page snapshot for the review panel."""
        with self._page_action_lock:
            prepared_files = list(self._prepared_files)
            pending = {
                (action.relative_path, action.page_number): action.kind
                for action in self._pending_page_actions
            }
            skipped = set(self._user_skipped_pages)
        files: list[dict[str, Any]] = []
        for prepared in prepared_files:
            record = prepared.record
            by_number = {page.page_number: page for page in list(record.pages)}
            pages: list[dict[str, Any]] = []
            for page_number in range(1, max(0, int(record.page_count)) + 1):
                page = by_number.get(page_number)
                key = (record.relative_path, page_number)
                pages.append(
                    self._page_snapshot_entry(
                        prepared,
                        page_number=page_number,
                        page=page,
                        pending_action=pending.get(key, ""),
                        user_skipped=key in skipped,
                    )
                )
            files.append(
                {
                    "name": record.name,
                    "relative_path": record.relative_path,
                    "source_type": record.source_type,
                    "status": record.status,
                    "error": _short_review_text(record.error),
                    "page_count": int(record.page_count),
                    "document_kind": record.document_kind,
                    "translated_docx_path": record.translated_docx_path,
                    "pages": pages,
                }
            )
        # 审核开关要跟着快照一起交出去：关着的时候，逐页审核卡片那句「审核模型逐页检查版式
        # 与译文完整性」跟同一张表里 14 行「未审核」是自相矛盾的，界面得知道该改口。
        return {
            "files": files,
            "review_enabled": bool(self._settings.pdf.review_enabled),
            # 终态任务的单页重生成没有 SSE 可用（事件流在终态就结束了），这份快照
            # 就是界面唯一的进度来源：哪一页在重跑、上一次为什么失败都从这里读。
            "rerun": self.page_rerun_state(),
            "can_rerun": self.can_rerun_pages(),
        }

    def resolve_page_image_path(
        self,
        *,
        relative_path: str,
        page_number: int,
        kind: str,
    ) -> Path | None:
        """Resolve one page image strictly inside this task's archive folders."""
        normalized_kind = str(kind or "").strip().lower()
        if normalized_kind not in {
            PDF_PAGE_IMAGE_KIND_SOURCE,
            PDF_PAGE_IMAGE_KIND_TRANSLATED,
            PDF_PAGE_IMAGE_KIND_PREVIOUS,
        }:
            raise PdfPageActionError("页图类型只能是 source、translated 或 previous。")
        with self._page_action_lock:
            prepared = self._find_prepared_file(relative_path)
        if prepared is None:
            return None
        record = prepared.record
        try:
            page_number = int(page_number)
        except (TypeError, ValueError):
            return None
        if page_number < 1 or (record.page_count and page_number > record.page_count):
            return None
        page = next(
            (item for item in list(record.pages) if item.page_number == page_number),
            None,
        )
        return self._page_image_file(prepared, page_number, page, normalized_kind)

    def request_page_regenerate(self, *, relative_path: str, page_number: int) -> dict[str, Any]:
        """Queue one finished page for a full re-run on the next resume."""
        return self._queue_page_action(
            PDF_PAGE_ACTION_REGENERATE,
            relative_path=relative_path,
            page_number=page_number,
        )

    def request_page_skip(self, *, relative_path: str, page_number: int) -> dict[str, Any]:
        """Queue one failed page to be accepted as a placeholder page."""
        return self._queue_page_action(
            PDF_PAGE_ACTION_SKIP,
            relative_path=relative_path,
            page_number=page_number,
        )

    # ---- single-page rerun after the task ended --------------------------------
    #
    # The paused-task actions above only queue: ``_run`` is still parked inside
    # its pause loop and applies them when it wakes up.  Once the task is over
    # that thread is gone, so a rerun has to redo, on a thread of its own, the
    # exact tail of ``_run`` this page touches: regenerate the page, re-assemble
    # the file it belongs to, and rewrite the manifest and the report.

    def page_rerun_state(self) -> dict[str, Any]:
        """Report which page, if any, is being re-run after the task ended."""
        with self._rerun_lock:
            key = self._rerun_page_key
            error = self._rerun_error
        return {
            "active": key is not None,
            "relative_path": key[0] if key is not None else "",
            "page_number": key[1] if key is not None else 0,
            "error": _short_review_text(error),
        }

    def can_rerun_pages(self) -> bool:
        """True when a single-page rerun could be started right now."""
        if self._finished_output_dir is None or self.is_running():
            return False
        with self._rerun_lock:
            return self._rerun_page_key is None and self._restore_page_key is None

    def _raise_if_rerun_active(self, *, locked: bool = False) -> None:
        """Refuse a second rerun; one page at a time keeps the output coherent."""
        if locked:
            active = self._rerun_page_key
            restoring = self._restore_page_key
        else:
            with self._rerun_lock:
                active = self._rerun_page_key
                restoring = self._restore_page_key
        if active is not None:
            raise PdfPageActionError(
                f"第 {active[1]} 页正在重新生成，完成后才能操作其他页。"
            )
        if restoring is not None:
            raise PdfPageActionError(
                f"第 {restoring[1]} 页正在换回上一版，完成后才能操作其他页。"
            )

    def result_patch(self) -> dict[str, Any]:
        """Return refreshed task-result fields produced by the last rerun."""
        return dict(self._result_patch)

    def rerun_page(
        self,
        *,
        relative_path: str,
        page_number: int,
        api_scheduler: WeightedApiScheduler | None = None,
        review_api_scheduler: WeightedApiScheduler | None = None,
    ) -> dict[str, Any]:
        """Start re-running one page of a finished task; overwrites its output."""
        try:
            page_number = int(page_number)
        except (TypeError, ValueError) as exc:
            raise PdfPageActionError("页码必须是整数。") from exc
        if self.is_running():
            raise PdfPageActionError("任务还在运行，请暂停后再做单页操作。")
        if self._finished_output_dir is None:
            raise PdfPageActionError("此任务没有保留输出目录，无法重新生成单页。")
        # Checked before the per-page checks below, not only at the claim: while
        # a rerun is in flight the page it owns has left ``record.pages``, so the
        # eligibility checks would otherwise answer "this page has no result to
        # regenerate" to someone who simply clicked a second page.
        self._raise_if_rerun_active()
        with self._page_action_lock:
            prepared = self._find_prepared_file(relative_path)
        if prepared is None:
            raise PdfPageActionError("该任务没有这个文件的逐页记录。")
        record = prepared.record
        if page_number < 1 or page_number > max(0, int(record.page_count)):
            raise PdfPageActionError(
                f"页码超出范围：{record.name} 共 {record.page_count} 页。"
            )
        # Regenerating drops the existing output file before it rebuilds one.
        # A file that never produced an output has nothing to rebuild from, so
        # the rerun would only delete evidence and then fail the same way.
        if record.status not in {PDF_OUTPUT_STATE_COMPLETED, PDF_OUTPUT_STATE_NEEDS_REVIEW}:
            raise PdfPageActionError(
                f"{record.name} 本次未生成输出文件，无法通过重新生成单页解决；请新建任务。"
            )
        page = next(
            (item for item in list(record.pages) if item.page_number == page_number),
            None,
        )
        if page is None or page.status not in _PDF_PAGE_REGENERABLE_STATUSES:
            raise PdfPageActionError(
                f"{record.name} 第 {page_number} 页没有可以重新生成的结果。"
            )
        with self._rerun_lock:
            self._raise_if_rerun_active(locked=True)
            self._rerun_page_key = (record.relative_path, page_number)
            self._rerun_error = ""
            thread = threading.Thread(
                target=self._run_page_rerun,
                args=(prepared, page_number, api_scheduler, review_api_scheduler),
                daemon=True,
                name=f"pdf-page-rerun-{page_number}",
            )
            self._rerun_thread = thread
        thread.start()
        return {
            "action": PDF_PAGE_ACTION_REGENERATE,
            "relative_path": record.relative_path,
            "name": record.name,
            "page_number": page_number,
            "applies_on": "now",
        }

    def _run_page_rerun(
        self,
        prepared: _PreparedPdfFile,
        page_number: int,
        api_scheduler: WeightedApiScheduler | None,
        review_api_scheduler: WeightedApiScheduler | None,
    ) -> None:
        record = prepared.record
        error = ""
        try:
            with provider_key_overrides(self._key_overrides):
                self._execute_page_rerun(
                    prepared,
                    page_number,
                    api_scheduler,
                    review_api_scheduler,
                )
        except PdfPageActionError as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001 - rerun failure becomes a panel message.
            logger.exception("PDF 单页重新生成失败")
            error = user_facing_reason(
                exc,
                fallback="重新生成这一页时出现了未预期的问题，输出文件未更新。",
            )
        if error:
            self._log(
                "ERROR",
                f"[{record.name}] 第 {page_number} 页重新生成失败：{error}",
            )
        with self._rerun_lock:
            self._rerun_page_key = None
            self._rerun_error = error
            self._rerun_thread = None

    def _execute_page_rerun(
        self,
        prepared: _PreparedPdfFile,
        page_number: int,
        api_scheduler: WeightedApiScheduler | None,
        review_api_scheduler: WeightedApiScheduler | None,
    ) -> None:
        record = prepared.record
        rerun_started = time.monotonic()
        # A finished run may have left the stop flag set (user stop / end-paused).
        # Leaving it set would make ``_process_prepared_pages`` refuse to submit
        # the very page this rerun exists to produce.
        self._stop_event.clear()
        self._pause_event.clear()
        self._stop_reason = ""
        self._fatal_model_error = ""
        self._fatal_review_model_error = ""
        self._stop_wait_started_at = None
        self._stop_wait_last_emit = 0.0

        # Resolved fresh rather than cached from the run: settings may have been
        # fixed since (a corrected API key is the common case), and a retained
        # terminal runner should not sit on key material it does not need.
        try:
            model_config = resolve_effective_model_config(self._settings, ROLE_IMAGE)
        except Exception as exc:  # noqa: BLE001 - init converted to a panel message.
            logger.debug(f"[PDF] 单页重生成解析翻译模型配置失败原始错误：{exc!r}")
            raise PdfPageActionError(
                "PDF 与图片翻译模型的设置不可用："
                + user_facing_reason(exc, fallback="请在设置中检查 PDF 与图片翻译模型的连接。")
            ) from exc
        if not provider_supports_capability(model_config.provider, "image"):
            raise PdfPageActionError(
                unsupported_capability_message(
                    "PDF 与图片翻译模型", model_config.provider, "image"
                )
            )
        if not model_config.model:
            raise PdfPageActionError("PDF 与图片翻译模型的名称不能为空。")

        review_model_config = None
        review_concurrency = 1
        if self._settings.pdf.review_enabled:
            try:
                review_model_config = resolve_effective_model_config(
                    self._settings,
                    ROLE_PDF_REVIEW,
                )
                review_concurrency = resolve_pdf_review_concurrency(
                    self._settings,
                    mode=getattr(review_model_config, "mode", None),
                )
            except Exception as exc:  # noqa: BLE001 - init converted to a panel message.
                logger.debug(f"[PDF] 单页重生成解析审核模型配置失败原始错误：{exc!r}")
                raise PdfPageActionError(
                    "PDF 与图片审核模型的设置不可用："
                    + user_facing_reason(
                        exc,
                        fallback="请在设置中检查 PDF 与图片审核模型的连接。",
                    )
                ) from exc
            if not provider_supports_capability(review_model_config.provider, "vision_text"):
                raise PdfPageActionError(
                    unsupported_capability_message(
                        "PDF 与图片审核模型", review_model_config.provider, "vision_text"
                    )
                )
            if not review_model_config.model:
                raise PdfPageActionError("PDF 与图片审核模型的名称不能为空。")

        max_attempts = max_page_generation_attempts(self._settings.pdf.page_retry_attempts)
        self._review_total = max_attempts
        scheduler = api_scheduler or WeightedApiScheduler(1)
        same_review_group = (
            review_model_config is not None
            and _api_group_signature_from_config(model_config)
            == _api_group_signature_from_config(review_model_config)
        )
        review_scheduler = review_api_scheduler or (
            scheduler if same_review_group else WeightedApiScheduler(review_concurrency)
        )

        page = next(
            (item for item in record.pages if item.page_number == page_number),
            None,
        )
        if page is None or page.status not in _PDF_PAGE_REGENERABLE_STATUSES:
            raise PdfPageActionError(
                f"{record.name} 第 {page_number} 页的结果已发生变化，已取消本次重新生成。"
            )
        self._log(
            "INFO",
            f"[{record.name}] 开始重新生成第 {page_number} 页：仅在成功后替换译文页和输出文件。",
        )
        # 事务边界。这一步之前先把「回滚需要的一切」拍下来：页记录本身、这份文件
        # 的产物字段和状态、逐页计数。重生成失败（模型 Key 失效是最常见的一种）时
        # 全部还原，磁盘上的高清/压缩 PDF 也从未被动过——旧版本从头到尾还在。
        page_backup = copy.deepcopy(page)
        artifact_backup = _file_artifact_snapshot(record)
        counters_backup = self._page_counters_snapshot()
        committed = False
        stashed = self._apply_page_regenerate(prepared, page, transactional=True)
        try:
            try:
                self._process_prepared_pages(
                    [prepared],
                    max_attempts=max_attempts,
                    scheduler=scheduler,
                    review_scheduler=review_scheduler,
                    model_config=model_config,
                    review_model_config=review_model_config,
                    concurrency=1,
                    total_pages=max(1, int(record.page_count)),
                )
            except (ImageModelUnavailableError, PdfReviewModelUnavailableError):
                # _process_prepared_pages 探测到模型/审核模型致命不可用时是直接把异常
                # raise 出来的（它自己在 finally 里说了「致命错误直接 raise 出去，收敛
                # 循环再也回不来」），不是正常返回后再让调用方去看 self._fatal_model_error
                # 标志位——下面 `if self._fatal_model_error:` 那行如果没有这个 except
                # 接住异常，永远走不到：能把这个标志位设成真的两处（本文件内）都紧跟着
                # raise。这里必须真的接住它，用户看到的 toast 才带得上「旧产物已保留」。
                pass
            if self._fatal_model_error:
                # finally 块里会把这一页和文件记录整体回滚（见下方 committed 分支），
                # 旧的译文页、高清/压缩 PDF 全都原样保留——异常消息本身要把这句话
                # 说出来，用户看到的是 toast，不是这段代码的回滚逻辑（低-旧产物提示）。
                raise PdfPageActionError(
                    f"{_with_sentence_end(self._fatal_model_error)}"
                    "已保留上一版译文页和输出文件，本次改动未生效。"
                )
            self._finalize_file_record(prepared, should_assemble=True)
            if record.status == PDF_OUTPUT_STATE_FAILED:
                # 装配失败：旧产物还在磁盘上（新产物是先写临时文件再原子替换的），
                # 所以这里同样要整体回滚，而不是把这份文件留在「失败」上——同样要
                # 把「旧产物没丢」说进异常消息（低-旧产物提示），不能只在 record.error
                # 缺失时走默认文案才带这句话。
                reason = _with_sentence_end(
                    record.error or "页面重新生成后，未能重新生成输出文件。"
                )
                raise PdfPageActionError(f"{reason}已保留上一版，输出文件未改动。")
            committed = True
        finally:
            if committed:
                self._commit_page_image_stash(prepared, stashed, page_backup=page_backup)
            else:
                self._restore_page_image_stash(stashed)
                self._restore_page_after_failed_rerun(
                    prepared,
                    page_number=page_number,
                    page_backup=page_backup,
                    artifact_backup=artifact_backup,
                    counters_backup=counters_backup,
                )
        self._rewrite_finished_report(extra_elapsed_sec=time.monotonic() - rerun_started)

    def _restore_page_after_failed_rerun(
        self,
        prepared: _PreparedPdfFile,
        *,
        page_number: int,
        page_backup: PdfPageRecord,
        artifact_backup: dict[str, Any],
        counters_backup: tuple,
    ) -> None:
        """单页重生成失败后把这份文件恢复到点「重新生成」之前的样子。"""
        record = prepared.record
        record.pages = [page for page in record.pages if page.page_number != page_number]
        record.pages.append(copy.deepcopy(page_backup))
        record.pages.sort(key=lambda item: item.page_number)
        _restore_file_artifacts(record, artifact_backup)
        self._refresh_file_record_counts(record)
        self._restore_page_counters(counters_backup)
        self._log(
            "WARN",
            f"[{record.name}] 第 {page_number} 页重新生成失败，已保留上一版译文页和输出文件，"
            "任务结果未改动。",
        )

    # ---- swapping one page back to its previous version -------------------------
    #
    # 换回不调用模型、不计费：这一页的上一版页图还在页面存档里（重生成成功时留的），
    # 输出文件本来就是从全部页图整份重装的，所以「换回」＝把两版页图对调一次，再走
    # 和重生成成功后完全相同的重装配路径。同步做完，没有后台线程。

    def restore_previous_page(self, *, relative_path: str, page_number: int) -> dict[str, Any]:
        """把一页换回上一版译文，并按新的页图重新生成输出文件。"""
        try:
            page_number = int(page_number)
        except (TypeError, ValueError) as exc:
            raise PdfPageActionError("页码必须是整数。") from exc
        if self.is_running():
            raise PdfPageActionError("任务还在运行，请暂停后再做单页操作。")
        if self._finished_output_dir is None:
            raise PdfPageActionError("此任务没有保留输出目录，无法换回上一版。")
        self._raise_if_rerun_active()
        with self._page_action_lock:
            prepared = self._find_prepared_file(relative_path)
        if prepared is None:
            raise PdfPageActionError("该任务没有这个文件的逐页记录。")
        record = prepared.record
        if page_number < 1 or page_number > max(0, int(record.page_count)):
            raise PdfPageActionError(
                f"页码超出范围：{record.name} 共 {record.page_count} 页。"
            )
        if record.status not in {PDF_OUTPUT_STATE_COMPLETED, PDF_OUTPUT_STATE_NEEDS_REVIEW}:
            raise PdfPageActionError(
                f"{record.name} 本次未生成输出文件，无法通过换回上一版解决；请新建任务。"
            )
        page = next(
            (item for item in record.pages if item.page_number == page_number),
            None,
        )
        if page is None or not self._page_image_file(
            prepared,
            page_number,
            page,
            PDF_PAGE_IMAGE_KIND_PREVIOUS,
        ):
            raise PdfPageActionError(
                f"{record.name} 第 {page_number} 页没有上一版译文，无法换回。"
            )
        if not page.translated_image_path or not Path(page.translated_image_path).is_file():
            raise PdfPageActionError(
                f"{record.name} 第 {page_number} 页的当前译文页图已丢失，无法互换；请重新生成此页。"
            )
        with self._rerun_lock:
            self._raise_if_rerun_active(locked=True)
            self._restore_page_key = (record.relative_path, page_number)
        try:
            self._execute_restore_previous(prepared, page)
        finally:
            with self._rerun_lock:
                self._restore_page_key = None
        return {
            "action": "restore_previous",
            "relative_path": record.relative_path,
            "name": record.name,
            "page_number": page_number,
        }

    def _execute_restore_previous(self, prepared: _PreparedPdfFile, page: PdfPageRecord) -> None:
        record = prepared.record
        page_number = int(page.page_number)
        started = time.monotonic()
        self._log(
            "INFO",
            f"[{record.name}] 第 {page_number} 页开始换回上一版：重新生成输出文件，不调用模型。",
        )
        # 和单页重生成同一条事务纪律：先把「回滚需要的一切」拍下来。区别只在于
        # 这里没有模型调用，失败的成因只剩装配本身。
        page_backup = copy.deepcopy(page)
        artifact_backup = _file_artifact_snapshot(record)
        counters_backup = self._page_counters_snapshot()
        # 成功路径故意不动 runner 的全局逐页计数器（_completed_page_count 等）：
        # 任务已终态，进度消息不再发，终态界面读的是 record.pages 重算出的记录级
        # 计数（_refresh_file_record_counts），口径以 record 为准。失败路径回滚
        # 计数器只是把快照原样放回，不是「成功也该调」的另一半。
        moves = self._swap_page_versions(prepared, page)
        committed = False
        try:
            self._finalize_file_record(prepared, should_assemble=True)
            if record.status == PDF_OUTPUT_STATE_FAILED:
                reason = _with_sentence_end(
                    record.error or "换回上一版后，未能重新生成输出文件。"
                )
                raise PdfPageActionError(f"{reason}已保留当前版本，本次换回未生效。")
            committed = True
        except PdfPageActionError:
            raise
        except Exception as exc:  # noqa: BLE001 - 失败变成一条面板消息。
            logger.exception("PDF 单页换回上一版失败")
            reason = _with_sentence_end(
                user_facing_reason(exc, fallback="换回上一版时出现了未预期的问题。")
            )
            raise PdfPageActionError(f"{reason}已保留当前版本，本次换回未生效。") from exc
        finally:
            if not committed:
                self._undo_page_version_swap(moves)
                _copy_page_record(page_backup, page)
                _restore_file_artifacts(record, artifact_backup)
                self._refresh_file_record_counts(record)
                self._restore_page_counters(counters_backup)
                self._log(
                    "WARN",
                    f"[{record.name}] 第 {page_number} 页换回上一版失败，已保留当前版本，输出文件未改动。",
                )
        self._rewrite_finished_report(extra_elapsed_sec=time.monotonic() - started)
        self._log(
            "OK",
            f"[{record.name}] 第 {page_number} 页已换回上一版，输出文件已重新装配。",
        )

    def _swap_page_versions(
        self,
        prepared: _PreparedPdfFile,
        page: PdfPageRecord,
    ) -> list[tuple[Path, Path]]:
        """三步互换这一页的当前版与上一版（文件与页记录一起），返回已完成的改名。

        三步都是同目录 os.replace：当前版→中转名、上一版→当前版的位置、中转名→
        上一版的位置。中途失败按相反顺序还原，磁盘上不会留下半套。
        """
        base_dir = prepared.translated_pages_dir
        current = Path(page.translated_image_path)
        previous = Path(page.previous_image_path)
        # 上一版的文件名就是「当初那一版的页图名」加了前缀，去掉前缀正好换回它原来
        # 的名字——模型换过输出格式时后缀跟着变，这条也照样成立。
        restored_current = base_dir / previous.name.removeprefix(PDF_PAGE_IMAGE_PREVIOUS_PREFIX)
        swapped_previous = base_dir / f"{PDF_PAGE_IMAGE_PREVIOUS_PREFIX}{current.name}"
        temp = base_dir / f"{PDF_PAGE_IMAGE_SWAP_PREFIX}{current.name}"
        for path in (current, previous, restored_current, swapped_previous, temp):
            if not _path_is_within(path, base_dir):
                raise PdfPageActionError("此页的译文页图不在任务的页面存档中，已取消换回。")
        done: list[tuple[Path, Path]] = []
        try:
            temp.unlink(missing_ok=True)
            for src, dst in ((current, temp), (previous, restored_current), (temp, swapped_previous)):
                os.replace(src, dst)
                done.append((src, dst))
        except Exception as exc:  # noqa: BLE001 - 半套互换比换不成危险得多。
            self._undo_page_version_swap(done)
            raise PdfPageActionError(
                "换回上一版时页图文件改名失败："
                + user_facing_reason(exc, fallback="无法修改页面存档中的文件。")
                + "已保留当前版本，本次换回未生效。"
            ) from exc
        previous_state = dict(page.previous_page_state or {})
        current_state = _page_version_state(page)
        if previous_state:
            _apply_page_version_state(page, previous_state)
        page.translated_image_path = str(restored_current)
        page.previous_image_path = str(swapped_previous)
        page.previous_page_state = current_state
        # 尺寸以磁盘上的那张图为准：更早的记录里可能根本没有这一版的结论
        # （previous_page_state 为空），装配和报告都要拿真实尺寸说话。
        measured = _resume_pick_page_image([restored_current], verify=False)
        if measured is not None:
            _, page.output_width_px, page.output_height_px = measured
        return done

    def _undo_page_version_swap(self, moves: list[tuple[Path, Path]]) -> None:
        """按相反顺序把互换过的页图搬回去。"""
        for src, dst in reversed(moves):
            try:
                os.replace(dst, src)
            except Exception as exc:  # noqa: BLE001 - 已经在失败路径上，不再抛新错。
                logger.debug(f"[PDF] 换回上一版回滚页图失败：{dst} → {src}：{exc!r}")

    def _page_counters_snapshot(self) -> tuple:
        with self._page_status_lock:
            return (
                self._completed_page_count,
                self._submitted_page_count,
                set(self._retrying_pages),
                set(self._retried_pages),
                set(self._recovered_pages),
                set(self._placeholder_pages),
            )

    def _restore_page_counters(self, snapshot: tuple) -> None:
        with self._page_status_lock:
            (
                self._completed_page_count,
                self._submitted_page_count,
                retrying,
                retried,
                recovered,
                placeholder,
            ) = snapshot
            self._retrying_pages = set(retrying)
            self._retried_pages = set(retried)
            self._recovered_pages = set(recovered)
            self._placeholder_pages = set(placeholder)
            self._emit_page_status_locked()

    def _remember_finished_run(
        self,
        *,
        output_dir: Path,
        started: datetime,
        summary: PdfTaskSummary,
        stopped: bool,
        fatal_error: str,
    ) -> None:
        """Keep what ``_run`` knew, so a later single-page rerun can redo its tail."""
        self._finished_output_dir = output_dir
        self._finished_started_at = started
        self._finished_elapsed_sec = float(summary.elapsed_sec)
        self._finished_stopped = bool(stopped)
        self._finished_stop_reason = summary.stop_reason
        self._finished_fatal_error = fatal_error

    def _rewrite_finished_report(self, *, extra_elapsed_sec: float) -> None:
        """Rebuild the manifest, the report and the task-result payload."""
        output_dir = self._finished_output_dir
        if output_dir is None:
            return
        with self._page_action_lock:
            file_records = [prepared.record for prepared in self._prepared_files]
        # Wall-clock between the run ending and the user clicking 重新生成 is not
        # time this task spent working; only the rerun itself is added.
        self._finished_elapsed_sec += max(0.0, float(extra_elapsed_sec))
        summary = self._build_summary(
            output_dir=output_dir,
            started=self._finished_started_at or datetime.now(),
            file_records=file_records,
            stopped=self._finished_stopped,
            fatal_error=self._finished_fatal_error,
            elapsed_sec=self._finished_elapsed_sec,
            stop_reason=self._finished_stop_reason,
        )
        manifest_path, report_path = write_pdf_manifest_and_report(
            summary, artifact_dir=self._process_work_dir
        )
        logger.info(f"[PDF] 已更新进度记录：{manifest_path.name}")
        self._log("INFO", f"已更新翻译报告：{report_path.name}")
        self._result_patch = {
            "output_dir": str(output_dir),
            "report_path": str(report_path),
            "manifest_path": str(manifest_path),
            "file_results": [_file_record_to_result(record) for record in file_records],
            "elapsed_sec": summary.elapsed_sec,
            "api_call_count": self._api_call_count,
            "issues": _summary_issues(file_records),
            "kpi": _done_kpi(summary),
        }

    def _queue_page_action(
        self,
        kind: str,
        *,
        relative_path: str,
        page_number: int,
    ) -> dict[str, Any]:
        try:
            page_number = int(page_number)
        except (TypeError, ValueError) as exc:
            raise PdfPageActionError("页码必须是整数。") from exc
        with self._page_action_lock:
            prepared = self._find_prepared_file(relative_path)
            if prepared is None:
                raise PdfPageActionError("该任务没有这个文件的逐页记录。")
            record = prepared.record
            if page_number < 1 or page_number > max(0, int(record.page_count)):
                raise PdfPageActionError(
                    f"页码超出范围：{record.name} 共 {record.page_count} 页。"
                )
            if record.status == PDF_OUTPUT_STATE_FAILED:
                raise PdfPageActionError(
                    f"{record.name} 整体处理失败，无法进行单页操作；请新建任务。"
                )
            page = next(
                (item for item in list(record.pages) if item.page_number == page_number),
                None,
            )
            if page is None or page.status not in _PDF_PAGE_REGENERABLE_STATUSES:
                raise PdfPageActionError(
                    f"{record.name} 第 {page_number} 页尚未处理完成，暂时无法操作。"
                )
            if kind == PDF_PAGE_ACTION_SKIP:
                if record.source_type == SOURCE_TYPE_IMAGE:
                    raise PdfPageActionError("图片任务不生成占位页，无法跳过。")
                if page.status not in _PDF_PAGE_SKIPPABLE_STATUSES:
                    raise PdfPageActionError(
                        f"{record.name} 第 {page_number} 页未失败，无法跳过；如需修改，请选择「重新生成」。"
                    )
            self._pending_page_actions = [
                action
                for action in self._pending_page_actions
                if not (
                    action.relative_path == record.relative_path
                    and action.page_number == page_number
                )
            ]
            self._pending_page_actions.append(
                _PdfPageAction(
                    kind=kind,
                    relative_path=record.relative_path,
                    page_number=page_number,
                )
            )
            file_name = record.name
        return {
            "action": kind,
            "relative_path": relative_path,
            "name": file_name,
            "page_number": page_number,
            "applies_on": "resume",
        }

    def _find_prepared_file(self, relative_path: str) -> _PreparedPdfFile | None:
        """Match a caller-supplied key against known records; never join paths."""
        wanted = str(relative_path or "").strip()
        if not wanted:
            return None
        normalized = wanted.replace("\\", "/")
        for prepared in self._prepared_files:
            known = str(prepared.record.relative_path)
            if known == wanted or known.replace("\\", "/") == normalized:
                return prepared
        return None

    def _page_snapshot_entry(
        self,
        prepared: _PreparedPdfFile,
        *,
        page_number: int,
        page: PdfPageRecord | None,
        pending_action: str,
        user_skipped: bool,
    ) -> dict[str, Any]:
        status = page.status if page is not None else "pending"
        review_status = page.review_status if page is not None else "skipped"
        entry = {
            "page_number": page_number,
            "status": status,
            "review_status": review_status,
            "attempts": int(page.attempts) if page is not None else 0,
            "placeholder": bool(page.placeholder) if page is not None else False,
            # 「跳过大幅面页」命中的页面：前端要能区分「未翻译，原样保留」和普通
            # 失败/占位页，不能只靠 status 字符串。
            "skipped_oversize": bool(page.skipped_oversize) if page is not None else False,
            "inplace_requested": bool(page.inplace_requested) if page is not None else False,
            "inplace_status": page.inplace_status if page is not None else "",
            "inplace_written": int(page.inplace_written) if page is not None else 0,
            "inplace_kept": int(page.inplace_kept) if page is not None else 0,
            "inplace_review": int(page.inplace_review) if page is not None else 0,
            "inplace_untranslated": int(page.inplace_untranslated) if page is not None else 0,
            "inplace_term_review": int(page.inplace_term_review) if page is not None else 0,
            "inplace_review_items": list(page.inplace_review_items[:50]) if page is not None else [],
            "inplace_message": page.inplace_message if page is not None else "",
            "kept_reason": page.kept_reason if page is not None else "",
            "word_requested": bool(page.word_requested) if page is not None else False,
            "word_status": page.word_status if page is not None else "",
            "word_message": page.word_message if page is not None else "",
            "route_note": page.route_note if page is not None else "",
            "error": _short_review_text(page.error if page is not None else ""),
            # 本地质检的标记要单独给前端。它跟审核结论是两件事：质检在送审之前就跑
            # （_apply_quality_flags），审核通过并不会清掉它。不把它送出去的话，一页被
            # 小结算进「有疑点仍采用」的页，在逐页表格里却显示成绿色的「通过 · 版式一致，
            # 文本完整」——同一屏两句话互相打脸，而用户根本找不到是哪一页。
            "quality_flagged": bool(page.quality_flags) if page is not None else False,
            "quality_message": _short_review_text(page.quality_message if page is not None else ""),
            "emergency_ratio_normalized": (
                bool(page.emergency_ratio_normalized) if page is not None else False
            ),
            "review_summary": _page_review_summary(page),
            "pending_action": pending_action,
            "user_skipped": user_skipped,
            "has_source_image": bool(
                self._page_image_file(
                    prepared,
                    page_number,
                    page,
                    PDF_PAGE_IMAGE_KIND_SOURCE,
                )
            ),
            "has_translated_image": bool(
                self._page_image_file(
                    prepared,
                    page_number,
                    page,
                    PDF_PAGE_IMAGE_KIND_TRANSLATED,
                )
            ),
            # 这一页留着上一版译文（上一次重生成把旧译图顶下来时留的）。对比弹窗
            # 靠它决定要不要出「当前版／上一版」切换和「换回这一版」。
            "has_previous_image": bool(
                self._page_image_file(
                    prepared,
                    page_number,
                    page,
                    PDF_PAGE_IMAGE_KIND_PREVIOUS,
                )
            ),
        }
        return entry

    def _page_image_file(
        self,
        prepared: _PreparedPdfFile,
        page_number: int,
        page: PdfPageRecord | None,
        kind: str,
    ) -> Path | None:
        """Return an existing page image contained by this task's archive dir."""
        source_kind = kind == PDF_PAGE_IMAGE_KIND_SOURCE
        base_dir = prepared.source_pages_dir if source_kind else prepared.translated_pages_dir
        if kind == PDF_PAGE_IMAGE_KIND_PREVIOUS:
            # 上一版只认记录里指名的那一份，没有任何按名字猜的兜底：猜出来的文件
            # 一旦不是这一页真正的上一版，换回就会把别的东西写进交付文件。
            recorded = str(page.previous_image_path) if page is not None else ""
            if not recorded:
                return None
            candidate = Path(recorded)
            # 名字必须带上一版前缀：_swap_page_versions 是拿 removeprefix 反推
            # 「当前版该叫什么」的，指到一个没前缀的文件会把当前版页图改名吞掉。
            # 今天没有写入路径能造出这种记录，这里是纯护栏。
            if not candidate.name.startswith(PDF_PAGE_IMAGE_PREVIOUS_PREFIX):
                return None
            if _path_is_within(candidate, base_dir) and candidate.is_file():
                return candidate
            return None
        page_count = max(1, int(prepared.record.page_count or 1))
        candidates: list[Path] = []
        if page is not None:
            recorded = page.source_image_path if source_kind else page.translated_image_path
            if recorded:
                candidates.append(Path(recorded))
        candidates.append(base_dir / page_image_name(page_number, page_count))
        if not source_kind:
            candidates.append(base_dir / page_image_name(page_number, page_count, failed=True))
            stem = page_image_name(page_number, page_count).removesuffix(".png")
            candidates.extend(sorted(base_dir.glob(f"{stem}.*")) if base_dir.is_dir() else [])
        for candidate in candidates:
            if not _path_is_within(candidate, base_dir):
                continue
            if candidate.is_file():
                return candidate
        return None

    def _apply_pending_page_actions(self, prepared_files: list[_PreparedPdfFile]) -> None:
        """Apply queued page actions while no page future can be in flight."""
        with self._page_action_lock:
            actions = list(self._pending_page_actions)
            self._pending_page_actions = []
        if not actions:
            return
        by_path = {prepared.record.relative_path: prepared for prepared in prepared_files}
        for action in actions:
            prepared = by_path.get(action.relative_path)
            if prepared is None:
                continue
            record = prepared.record
            if record.status == PDF_OUTPUT_STATE_FAILED:
                continue
            page = next(
                (item for item in record.pages if item.page_number == action.page_number),
                None,
            )
            if page is None or page.status not in _PDF_PAGE_REGENERABLE_STATUSES:
                # The page changed hands between the request and this point.
                self._log(
                    "WARN",
                    f"[{record.name}] 第 {action.page_number} 页状态已变化，已取消本次单页操作。",
                )
                continue
            if action.kind == PDF_PAGE_ACTION_REGENERATE:
                self._apply_page_regenerate(prepared, page)
            elif action.kind == PDF_PAGE_ACTION_SKIP:
                self._apply_page_skip(prepared, page)

    def _apply_page_regenerate(
        self,
        prepared: _PreparedPdfFile,
        page: PdfPageRecord,
        *,
        transactional: bool = False,
    ) -> list[tuple[Path, Path]]:
        """让一页重新排队生成。

        ``transactional=True`` 是终态任务的单页重生成用的：旧的译文页图只是改名
        挪开（失败时原样搬回），已经交付的高清/压缩 PDF 一个字节都不动——新产物
        由 ``_assemble_translated_pdf`` 写完整之后原子替换。任务运行中的那条路
        （暂停时排队的单页操作）仍然照旧清理，因为它后面必定会重新装配，而且续译
        流程本来就能从页面存档重建产物。

        返回值是「已挪开的译文页图」清单，交给调用方提交或回滚。
        """
        record = prepared.record
        # Dropping the record is what makes ``_iter_rendered_pages`` yield this
        # page again on re-entry, and it is also what keeps the re-yielded page
        # from becoming a duplicate entry at ``record.pages.append``.
        record.pages.remove(page)
        with self._page_action_lock:
            self._user_skipped_pages.discard((record.relative_path, page.page_number))
        self._forget_page_progress(page)
        stashed = self._remove_translated_page_images(prepared, page, stash=transactional)
        if not transactional:
            # A file that already produced a PDF must be assembled again after the
            # page is regenerated.
            self._clear_generated_pdf_outputs([prepared])
        self._log(
            "INFO",
            f"[{record.name}] 第 {page.page_number} 页已按用户要求重新排队生成。",
        )
        return stashed

    def _apply_page_skip(self, prepared: _PreparedPdfFile, page: PdfPageRecord) -> None:
        record = prepared.record
        if record.source_type == SOURCE_TYPE_IMAGE:
            return
        if page.status not in _PDF_PAGE_SKIPPABLE_STATUSES:
            self._log(
                "WARN",
                f"[{record.name}] 第 {page.page_number} 页已不是失败页，已取消跳过。",
            )
            return
        page.status = "placeholder_pending"
        page.placeholder = True
        page.error = page.error or "用户已选择跳过该页。"
        with self._page_action_lock:
            self._user_skipped_pages.add((record.relative_path, page.page_number))
        self._record_page_placeholder(page)
        self._log(
            "WARN",
            f"[{record.name}] 第 {page.page_number} 页已按用户选择跳过，将生成失败占位页。",
        )

    def _discard_stopped_page(
        self,
        prepared: _PreparedPdfFile,
        page_record: PdfPageRecord,
    ) -> None:
        """把中止时没跑出结果的页整条撤回「未开始」。

        页记录是在提交之前就 append 进去的，所以中止时它已经在 ``record.pages``
        里了。留着它（哪怕状态写成失败）会让 ``_record_has_all_pages_finished``
        认为整份文件已经跑完，于是占位页被烧进交付 PDF、任务还报「已完成、未截
        断」。撤掉之后这一页在快照里回到 pending，续译能重新排队。
        """
        record = prepared.record
        try:
            record.pages.remove(page_record)
        except ValueError:  # 页记录已被别处摘掉（单页重生成），不重复处理。
            pass
        key = self._page_status_key(page_record)
        with self._page_status_lock:
            # 提交时 +1 过，这一页没有产出，账要平回去。
            self._submitted_page_count = max(0, self._submitted_page_count - 1)
            self._retrying_pages.discard(key)
            self._retried_pages.discard(key)
            self._recovered_pages.discard(key)
            self._placeholder_pages.discard(key)
            self._emit_page_status_locked()
        self._log(
            "WARN",
            f"[{record.name}] 第 {page_record.page_number} 页在任务停止时尚未生成结果，已按未开始处理："
            "不生成占位页，继续上次翻译时将重新排队。",
            visible=False,
        )

    def _discard_stopped_pages(self, prepared_files: list[_PreparedPdfFile]) -> None:
        """兜底：把所有还挂着 ``stopped_unstarted`` 的页统一撤回「未开始」。

        正常收敛 future 的那条路上，每收回一页就地撤一页。但致命错误（翻译模型
        或审核模型不可用）是先置停止标志再 ``raise`` 出去的，收敛循环再也回不
        来——还在飞的页由执行器 ``__exit__`` 等回来，工作线程就地把它们标成
        ``stopped_unstarted`` 就没人管了。这个状态只是「撤回这条页记录」的内部
        信号，一旦跟着异常留在 ``record.pages`` 里，就会被写进清单、送进逐页面
        板：前端不认识这个字符串，会把一张连译文图都没有的页显示成「已跑完 ·
        未审核」，报告的「未开始页面」也漏计。

        调用点在 ``_process_prepared_pages`` 的 ``finally`` 里，且在
        ``with ThreadPoolExecutor`` 之外——执行器 ``__exit__`` 已经 join 完所有
        工作线程，这里遍历页记录不会和它们抢。
        """
        for prepared in prepared_files:
            for page_record in list(prepared.record.pages):
                if page_record.status != PDF_PAGE_STATUS_STOPPED_UNSTARTED:
                    continue
                self._discard_stopped_page(prepared, page_record)

    def _forget_page_progress(self, page_record: PdfPageRecord) -> None:
        """Undo the aggregate counters of a page that is about to run again."""
        key = self._page_status_key(page_record)
        with self._page_status_lock:
            self._completed_page_count = max(0, self._completed_page_count - 1)
            self._submitted_page_count = max(0, self._submitted_page_count - 1)
            self._retrying_pages.discard(key)
            self._retried_pages.discard(key)
            self._recovered_pages.discard(key)
            self._placeholder_pages.discard(key)
            self._emit_page_status_locked()

    def _remove_translated_page_images(
        self,
        prepared: _PreparedPdfFile,
        page: PdfPageRecord,
        *,
        stash: bool = False,
    ) -> list[tuple[Path, Path]]:
        """Drop stale translated page images so the panel cannot show them.

        ``stash=True`` 时不删除，只改名挪到同目录的隐藏名下，返回
        ``[(挪走后的路径, 原路径), ...]``：重生成成功就丢掉，失败就搬回来，
        这一页于是要么整个换新、要么原样不动。
        """
        base_dir = prepared.translated_pages_dir
        stashed: list[tuple[Path, Path]] = []
        if not base_dir.is_dir():
            return stashed
        page_count = max(1, int(prepared.record.page_count or 1))
        stem = page_image_name(page.page_number, page_count).removesuffix(".png")
        for candidate in sorted(base_dir.glob(f"{stem}*")):
            if not _path_is_within(candidate, base_dir) or not candidate.is_file():
                continue
            if candidate.name.startswith(PDF_PAGE_IMAGE_STASH_PREFIX):
                continue
            try:
                if stash:
                    target = base_dir / f"{PDF_PAGE_IMAGE_STASH_PREFIX}{candidate.name}"
                    target.unlink(missing_ok=True)
                    candidate.rename(target)
                    stashed.append((target, candidate))
                else:
                    candidate.unlink()
            except Exception as exc:  # noqa: BLE001 - cleanup must not block resume.
                self._log(
                    "WARN",
                    f"[{prepared.record.name}] 清除旧译图失败：{candidate.name}："
                    f"{user_facing_reason(exc, fallback='无法删除文件。')}",
                )
        return stashed

    def _commit_page_image_stash(
        self,
        prepared: _PreparedPdfFile,
        stashed: list[tuple[Path, Path]],
        *,
        page_backup: PdfPageRecord,
    ) -> None:
        """重生成成功：把挪开的旧译图提升为这一页的「上一版」。

        只留页图、不留整份旧 PDF——输出文件每次都是从全部页图整份重装的，留住
        页图就等于留住了那一版的输出，而且不会随文件大小翻倍占盘。
        只留紧邻的一版：上一轮留下的那份在这里被顶替掉。

        提升失败（改名报错）时退回旧行为把它删掉：宁可这一页没有「上一版」，
        也不能让记录指着一个不存在的文件。
        """
        base_dir = prepared.translated_pages_dir
        page_number = int(page_backup.page_number)
        wanted = str(page_backup.translated_image_path or "")
        # 一页正常只有一份译图；真出现多份（模型换过输出格式）时，只有记录指着的
        # 那一份才是「上一版」，其余是残留，跟着删掉。
        keeper = next((moved for moved, original in stashed if str(original) == wanted), None)
        if keeper is None and len(stashed) == 1:
            keeper = stashed[0][0]
        promoted = ""
        for moved, original in stashed:
            if moved is not keeper:
                try:
                    moved.unlink(missing_ok=True)
                except Exception as exc:  # noqa: BLE001 - 残留文件不影响产物。
                    logger.debug(f"[PDF] 删除挪开的旧译图失败：{moved}：{exc!r}")
                continue
            target = base_dir / f"{PDF_PAGE_IMAGE_PREVIOUS_PREFIX}{original.name}"
            try:
                target.unlink(missing_ok=True)
                os.replace(moved, target)
                promoted = str(target)
            except Exception as exc:  # noqa: BLE001 - 留不住上一版不该让重生成失败。
                logger.debug(f"[PDF] 保留上一版译图失败：{moved}：{exc!r}")
                try:
                    moved.unlink(missing_ok=True)
                except Exception as cleanup_exc:  # noqa: BLE001
                    logger.debug(f"[PDF] 删除挪开的旧译图失败：{moved}：{cleanup_exc!r}")
        page = next(
            (item for item in prepared.record.pages if item.page_number == page_number),
            None,
        )
        if page is None:
            # 这一页的记录在重生成里被换走了：留下的「上一版」没有任何记录指着它，
            # 只会是页面存档里一份看不懂的隐藏文件。
            promoted = ""
        self._prune_previous_page_images(prepared, page_number, keep=promoted)
        if page is None:
            return
        page.previous_image_path = promoted
        page.previous_page_state = _page_version_state(page_backup) if promoted else {}

    def _prune_previous_page_images(
        self,
        prepared: _PreparedPdfFile,
        page_number: int,
        *,
        keep: str,
    ) -> None:
        """删掉这一页除 ``keep`` 之外的所有「上一版」文件。

        后缀会变（模型换过输出格式：page_001.png → page_001.jpg），不清理的话
        同一页会留下两份「上一版」，而记录只认得其中一份，另一份成了孤儿。
        """
        base_dir = prepared.translated_pages_dir
        if not base_dir.is_dir():
            return
        page_count = max(1, int(prepared.record.page_count or 1))
        stem = page_image_name(page_number, page_count).removesuffix(".png")
        for candidate in sorted(base_dir.glob(f"{PDF_PAGE_IMAGE_PREVIOUS_PREFIX}{stem}*")):
            if str(candidate) == keep or not candidate.is_file():
                continue
            if not _path_is_within(candidate, base_dir):
                continue
            try:
                candidate.unlink()
            except Exception as exc:  # noqa: BLE001 - 残留文件不影响产物。
                logger.debug(f"[PDF] 清除更早的上一版译图失败：{candidate}：{exc!r}")

    def _restore_page_image_stash(self, stashed: list[tuple[Path, Path]]) -> None:
        """重生成失败：把挪开的旧译图原样搬回来。"""
        for stashed_path, original in stashed:
            try:
                if not stashed_path.is_file():
                    continue
                original.unlink(missing_ok=True)
                stashed_path.rename(original)
            except Exception as exc:  # noqa: BLE001 - 已经在失败路径上，不再抛新错。
                logger.debug(f"[PDF] 恢复旧译图失败：{stashed_path}：{exc!r}")

    def _log(self, level: str, message: str, *, visible: bool = True) -> None:
        self._queue.put(LogMsg(level=level, message=message, visible=visible))
        task_logger_method = {
            "ERROR": self._task_logger.error,
            "WARN": self._task_logger.warning,
        }.get(level, self._task_logger.info)
        task_logger_method(message)
        logger.info(f"[PDF][{level}] {message}")

    def _run(self) -> None:
        started = datetime.now()
        output_dir: Path | None = None
        file_records: list[PdfFileRecord] = []
        fatal_error = ""
        stopped = False
        self._fatal_model_error = ""
        self._fatal_review_model_error = ""
        self._total_page_count = 0
        self._completed_page_count = 0
        self._submitted_page_count = 0
        self._stop_wait_started_at = None
        self._stop_wait_last_emit = 0.0
        self._retrying_pages.clear()
        self._retried_pages.clear()
        self._recovered_pages.clear()
        self._placeholder_pages.clear()
        with self._page_action_lock:
            self._prepared_files = []
            self._pending_page_actions = []
            self._user_skipped_pages = set()
        model_signature = _safe_image_model_signature(self._settings)
        review_model_config = None
        review_concurrency = 1
        review_model_signature = _safe_pdf_review_model_signature(self._settings)
        review_enabled = bool(self._settings.pdf.review_enabled)

        self._image_model_missing = ""
        self._document_kinds = {}
        image_optional = getattr(self._settings.pdf, "translation_route", "image") != "image"
        model_config = None
        image_concurrency = 1
        try:
            model_config = resolve_effective_model_config(self._settings, ROLE_IMAGE)
            image_concurrency = resolve_pdf_page_concurrency(
                self._settings, mode=getattr(model_config, "mode", None)
            )
            model_signature = image_model_signature(self._settings)
            if not provider_supports_capability(model_config.provider, "image"):
                raise ImageModelUnavailableError(
                    unsupported_capability_message(
                        "PDF 与图片翻译模型", model_config.provider, "image"
                    )
                )
            if not model_config.model:
                raise ImageModelUnavailableError("PDF 与图片翻译模型的名称不能为空")
            if image_optional and model_config.mode == "cloud" and not model_config.api_key:
                raise ImageModelUnavailableError("PDF 与图片翻译模型还没有填写 API 密钥")
        except ImageModelUnavailableError as exc:
            message = str(exc)
            if image_optional:
                # 原位替换、转 Word 不用图像模型；要整页图像翻译的页到时保留原样。
                self._image_model_missing = message
                model_config = None
            else:
                record_image_model_availability(
                    self._settings,
                    ok=False,
                    message=message,
                    signature=model_signature,
                    checked_at=datetime.now().isoformat(timespec="seconds"),
                )
                self._queue.put(ErrorMsg(message=f"PDF 与图片翻译模型的设置不可用：{message}"))
                return
        except Exception as exc:  # noqa: BLE001 - init converted to UI error.
            logger.debug(f"[PDF] 解析翻译模型配置失败原始错误：{exc!r}")
            if image_optional:
                self._image_model_missing = user_facing_reason(exc, fallback="图像翻译模型不可用。")
                model_config = None
            else:
                self._queue.put(
                    ErrorMsg(
                        message="PDF 与图片翻译模型的设置不可用："
                        + user_facing_reason(
                            exc,
                            fallback="请在设置中检查 PDF 与图片翻译模型的连接。",
                        )
                    )
                )
                return

        if review_enabled and model_config is not None:
            try:
                review_model_config = resolve_effective_model_config(
                    self._settings,
                    ROLE_PDF_REVIEW,
                )
                review_concurrency = resolve_pdf_review_concurrency(
                    self._settings,
                    mode=getattr(review_model_config, "mode", None),
                )
                review_model_signature = pdf_review_model_signature(self._settings)
                if not provider_supports_capability(review_model_config.provider, "vision_text"):
                    raise PdfReviewModelUnavailableError(
                        unsupported_capability_message(
                            "PDF 与图片审核模型", review_model_config.provider, "vision_text"
                        )
                    )
                if not review_model_config.model:
                    raise PdfReviewModelUnavailableError("PDF 与图片审核模型的名称不能为空")
            except PdfReviewModelUnavailableError as exc:
                message = str(exc)
                record_pdf_review_model_availability(
                    self._settings,
                    ok=False,
                    message=message,
                    signature=review_model_signature,
                    checked_at=datetime.now().isoformat(timespec="seconds"),
                )
                self._queue.put(ErrorMsg(message=f"PDF 与图片审核模型的设置不可用：{message}"))
                return
            except Exception as exc:  # noqa: BLE001 - init converted to UI error.
                logger.debug(f"[PDF] 解析审核模型配置失败原始错误：{exc!r}")
                self._queue.put(
                    ErrorMsg(
                        message="PDF 与图片审核模型的设置不可用："
                        + user_facing_reason(
                            exc,
                            fallback="请在设置中检查 PDF 与图片审核模型的连接。",
                        )
                    )
                )
                return

        root_for_output = self._source_root if self._source_root else self._files[0].path.parent
        pdf_output = self._settings.pdf_output
        custom_output_dir = (
            pdf_output.custom_output_dir
            if pdf_output.use_custom_output_dir
            else None
        )

        try:
            output_dir = build_pdf_output_dir(root_for_output, custom_output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
            app_managed = is_app_managed_pdf_output_dir(output_dir)
            self._process_artifacts = ProcessArtifacts.create(
                "pdf", output_dir, run_id=self.task_id
            )
            self._process_work_dir = Path(self._process_artifacts.work_dir)
            self._process_work_dir.mkdir(parents=True, exist_ok=True)
            max_attempts = max_page_generation_attempts(
                self._settings.pdf.page_retry_attempts
            )
            concurrency = self._resolve_pdf_concurrency(image_concurrency)
            self._review_total = max_attempts
            scheduler = self._api_scheduler_override or WeightedApiScheduler(concurrency)
            same_review_group = (
                review_model_config is not None
                and _api_group_signature_from_config(model_config)
                == _api_group_signature_from_config(review_model_config)
            )
            review_scheduler = self._review_api_scheduler_override or (
                scheduler
                if same_review_group
                else WeightedApiScheduler(review_concurrency)
            )
            self._queue.put(ProgressMsg(1, 4, "读取文件", 0, max(1, len(self._files))))
            self._queue.put(StatusMsg("状态：正在读取文件，准备输出文件夹…"))
            image_file_count = sum(1 for item in self._files if item.source_type == SOURCE_TYPE_IMAGE)
            pdf_file_count = len(self._files) - image_file_count
            self._log("INFO", f"扫描到 {pdf_file_count} 个 PDF 文件、{image_file_count} 个图片文件")
            # 只写目录名：整条路径会被日志脱敏换成「[path]」，那一行等于什么都没说，
            # 而完整路径本来就在结果横幅和「打开输出目录」按钮上。
            self._log("INFO", f"输出目录：{Path(output_dir).name}")
            logger.info(
                f"[PDF] 页面以 {PDF_RENDER_DPI_DEFAULT} DPI 渲染为 PNG，图片按模型返回格式输出"
            )
            if self._image_model_missing:
                self._log(
                    "WARN",
                    f"没有可用的图像翻译模型（{self._image_model_missing}）：需要整页图像翻译的页将保留原样。",
                )
            elif self._settings.pdf.page_generation_concurrency is None:
                self._log("INFO", f"同时处理 {concurrency} 页（按所选模型自动设定）")
            else:
                self._log("INFO", f"同时处理 {concurrency} 页（按设置里指定的数量）")
            if self._settings.pdf.generate_compressed_pdf:
                self._log(
                    "INFO",
                    "将同时生成高清 PDF 和压缩 PDF",
                )
            else:
                self._log("INFO", "将只生成高清 PDF")
            if review_enabled:
                self._log("INFO", "已开启逐页审核：每页译图先经审核模型检查，通过后再采用。")
                self._emit_review_status()

            prepared_files = self._prepare_pdf_files(
                output_dir=output_dir,
                app_managed=app_managed,
            )
            file_records = [prepared.record for prepared in prepared_files]
            with self._page_action_lock:
                self._prepared_files = list(prepared_files)
            # 复用上次的成品页必须发生在这里：页记录一旦建好，_iter_rendered_pages
            # 就会自然跳过这些页，装订、逐页面板、停止/暂停语义全都不用另开分支。
            self._prefill_resume_pages(prepared_files, output_dir=output_dir)
            # 自动组合：混合文件里的正文段先转 Word 翻好，转不成的页随后按保底方式翻译。
            self._run_word_segments(prepared_files)
            total_pages = sum(record.page_count for record in file_records)
            self._total_page_count = total_pages
            self._emit_page_status()
            for prepared in prepared_files:
                if self._all_file_pages_settled_for_progress(prepared.record):
                    completed = self._settled_file_page_count(prepared.record)
                    self._file_progress.emit(
                        prepared.item.path,
                        "waiting",
                        "generate",
                        completed=completed,
                        total=prepared.record.page_count,
                    )
            self._queue.put(
                ProgressMsg(1, 4, "读取文件", len(prepared_files), max(1, len(self._files)))
            )
            self._queue.put(StatusMsg(f"状态：已准备 {len(prepared_files)} 个文件，共 {total_pages} 页。"))

            while True:
                if (
                    not self._stop_event.is_set()
                    and total_pages > 0
                    and self._has_unfinished_pages(file_records)
                ):
                    self._process_prepared_pages(
                        prepared_files,
                        max_attempts=max_attempts,
                        scheduler=scheduler,
                        review_scheduler=review_scheduler,
                        model_config=model_config,
                        review_model_config=review_model_config,
                        concurrency=concurrency,
                        total_pages=total_pages,
                    )

                if self._pause_event.is_set() and not self._stop_event.is_set():
                    if self._wait_while_paused():
                        # Every submitted page has settled by now and no new
                        # page is in flight, so this is the only point where a
                        # per-page request can rewrite page records safely.
                        self._apply_pending_page_actions(prepared_files)
                        continue

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

                has_unfinished_pages = self._has_unfinished_pages(file_records)
                # Ending a paused task is terminal even if its last submitted
                # page settled while the user made that choice.
                stopped = (
                    self._stop_event.is_set()
                    and (has_unfinished_pages or self._stop_reason == "end_paused")
                    and not fatal_error
                )
                if not fatal_error:
                    resume_requested = False
                    self._queue.put(ProgressMsg(3, 4, "生成文件", 0, max(1, len(file_records))))
                    # 进度卡标题否则还停在上一句「正在翻译 PDF 页面，已完成 N / N 页」。
                    if not self._stop_event.is_set():
                        self._queue.put(StatusMsg("状态：正在生成输出文件…"))
                    for index, prepared in enumerate(prepared_files, start=1):
                        if stopped and not self._stop_event.is_set() and has_unfinished_pages:
                            resume_requested = True
                            break
                        should_assemble = (
                            not stopped or self._record_has_all_pages_finished(prepared.record)
                        )
                        self._file_progress.emit(
                            prepared.item.path,
                            "active",
                            "generate",
                            completed=self._settled_file_page_count(prepared.record),
                            total=prepared.record.page_count,
                        )
                        self._finalize_file_record(
                            prepared,
                            should_assemble=should_assemble,
                        )
                        completed = self._settled_file_page_count(prepared.record)
                        result = _file_record_to_result(prepared.record)
                        output = str(result.get("output") or "")
                        if prepared.record.status == PDF_OUTPUT_STATE_FAILED:
                            self._file_progress.emit(
                                prepared.item.path,
                                "failed",
                                "generate",
                                completed=completed,
                                total=prepared.record.page_count,
                                result=result,
                            )
                        elif prepared.record.status == PDF_OUTPUT_STATE_STOPPED:
                            self._file_progress.emit(
                                prepared.item.path,
                                "stopped",
                                "generate",
                                completed=completed,
                                total=prepared.record.page_count,
                                result=result,
                            )
                        elif output and Path(output).is_file():
                            self._file_progress.emit(
                                prepared.item.path,
                                "generated",
                                "generate",
                                completed=completed,
                                total=prepared.record.page_count,
                                result=result,
                            )
                        self._queue.put(
                            ProgressMsg(3, 4, "生成文件", index, max(1, len(file_records)))
                        )
                        has_unfinished_pages = self._has_unfinished_pages(file_records)
                        if stopped and not self._stop_event.is_set() and has_unfinished_pages:
                            resume_requested = True
                            break
                    if resume_requested:
                        resettable_records = [
                            prepared
                            for prepared in prepared_files
                            if prepared.record.status
                            in {
                                PDF_OUTPUT_STATE_COMPLETED,
                                PDF_OUTPUT_STATE_NEEDS_REVIEW,
                                PDF_OUTPUT_STATE_STOPPED,
                            }
                        ]
                        self._clear_generated_pdf_outputs(prepared_files)
                        for prepared in resettable_records:
                            if prepared.record.status == PDF_OUTPUT_STATE_FAILED:
                                continue
                            finished = self._all_file_pages_settled_for_progress(prepared.record)
                            self._file_progress.emit(
                                prepared.item.path,
                                "waiting",
                                "generate" if finished else "translate",
                                completed=self._settled_file_page_count(prepared.record),
                                total=prepared.record.page_count,
                                result={},
                                force=True,
                            )
                        self._log("INFO", "已中断 PDF 生成并清除旧的输出文件，将继续翻译剩余页面。")
                        continue
                break

            # 转 Word 的文件不逐页翻译，上面「还有没翻完的页」判断不到它们：在收尾时
            # 被停下来的，任务同样算停止，不能报成已完成。
            if (
                not stopped
                and not fatal_error
                and self._stop_event.is_set()
                and any(
                    any(item.get("status") == "stopped" for item in record.word_segments)
                    for record in file_records
                )
            ):
                stopped = True

            if stopped and not fatal_error:
                self._log("WARN", "已停止：已全部翻译的文件均已生成，未翻译完成的文件保留已完成的页面和报告。")
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
            logger.exception("PDF 翻译任务失败")
            fatal_error = "PDF 翻译任务失败：" + user_facing_reason(
                exc,
                fallback="出现了未预期的问题，已保留已生成的页面素材和报告。",
            )
            self._log("ERROR", fatal_error)

        if output_dir is not None:
            summary = self._build_summary(
                output_dir=output_dir,
                started=started,
                file_records=file_records,
                stopped=stopped,
                fatal_error=fatal_error,
            )
            self._queue.put(ProgressMsg(4, 4, "生成文件", 0, 1))
            manifest_path, report_path = write_pdf_manifest_and_report(
                summary, artifact_dir=self._process_work_dir
            )
            logger.info(f"[PDF] 已写入进度记录：{manifest_path.name}")
            self._log("INFO", f"已写入翻译报告：{report_path.name}")
            self._queue.put(ProgressMsg(4, 4, "生成文件", 1, 1))
            # From here on the run thread is finished but the page records, the
            # archive folders and the output directory all stay reachable, which
            # is what lets a finished task still regenerate a single page.
            self._remember_finished_run(
                output_dir=output_dir,
                started=started,
                summary=summary,
                stopped=stopped,
                fatal_error=fatal_error,
            )
            if self._process_artifacts is not None:
                try:
                    process_status = {
                        PDF_OUTPUT_STATE_COMPLETED: "succeeded",
                        PDF_OUTPUT_STATE_NEEDS_REVIEW: "needs_review",
                        PDF_OUTPUT_STATE_STOPPED: "paused",
                        PDF_OUTPUT_STATE_FAILED: "failed",
                    }.get(summary.status, summary.status)
                    self._process_artifacts.finish(process_status)
                except Exception:  # pragma: no cover - retention must not fail delivery
                    logger.debug("[PDF] process artifact finish failed", exc_info=True)

            # 中止和失败也要把逐文件结果和指标交出去（Excel / Word 那两路一直是这么做的）：
            # 停在半路的任务恰恰是最需要看「哪几个文件已经写出来了、有多少页是占位」的时候，
            # 而这里此前只给一句话，任务中心的文件表和指标格全是空的。
            file_results = [_file_record_to_result(record) for record in file_records]
            kpi = _done_kpi(summary)
            if stopped:
                self._queue.put(
                    StoppedMsg(
                        message=(
                            # 完整路径会被日志脱敏成字面的「[path]」显示给用户，只报文件夹名。
                            f"PDF 翻译已结束，已生成的页面和报告保存在输出文件夹「{Path(output_dir).name}」。"
                            if self._stop_reason == "end_paused"
                            else f"PDF 翻译已停止，已生成的页面和报告保存在输出文件夹「{Path(output_dir).name}」。"
                        ),
                        output_dir=str(output_dir),
                        report_path=str(report_path),
                        manifest_path=str(manifest_path),
                        files=file_results,
                        kpi=kpi,
                        terms=self._terms_result(),
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
                        files=file_results,
                        kpi=kpi,
                        terms=self._terms_result(),
                    )
                )
                return
            self._queue.put(
                DoneMsg(
                    output_dir=str(output_dir),
                    file_results=file_results,
                    elapsed_sec=summary.elapsed_sec,
                    tm_hit_count=0,
                    api_call_count=self._api_call_count,
                    issues=_summary_issues(file_records),
                    report_path=str(report_path),
                    manifest_path=str(manifest_path),
                    kpi=_done_kpi(summary),
                    terms=self._terms_result(),
                    # 按了停止、但在飞的页刚好全部跑完：任务是正常完成的，可小结里得留下
                    # 这一下的痕迹，否则用户只看到「已完成 · 全部通过」，不知道自己按的那
                    # 一下有没有截断内容。
                    stop=(
                        {"requested": True, "truncated": False}
                        if self._stop_event.is_set()
                        else {}
                    ),
                )
            )
            return

        if fatal_error:
            self._queue.put(ErrorMsg(message=fatal_error))

    def _prepare_pdf_files(
        self,
        *,
        output_dir: Path,
        app_managed: bool,
    ) -> list[_PreparedPdfFile]:
        prepared_files: list[_PreparedPdfFile] = []
        # 「自动识别场景」：每份 PDF 的文档说明在读文件时就发出去，各文件同时请求，
        # 不等上一份写完；全部读完后再收齐，翻译开始前每份的说明都已就绪。
        profile_executor = (
            ThreadPoolExecutor(max_workers=self._profile_concurrency, thread_name_prefix="pdf-profile")
            if self._profile_requester is not None
            else None
        )
        profile_futures: dict[str, Future[str]] = {}
        try:
            self._prepare_pdf_file_records(
                prepared_files,
                output_dir=output_dir,
                app_managed=app_managed,
                profile_executor=profile_executor,
                profile_futures=profile_futures,
            )
            for source_path, future in profile_futures.items():
                context = wait_result(future, should_stop=self._stop_event.is_set, default="")
                if context:
                    self._document_contexts[source_path] = context
        finally:
            if profile_executor is not None:
                stopped = self._stop_event.is_set()
                profile_executor.shutdown(wait=not stopped, cancel_futures=stopped)
        return prepared_files

    def _prepare_pdf_file_records(
        self,
        prepared_files: list[_PreparedPdfFile],
        *,
        output_dir: Path,
        app_managed: bool,
        profile_executor: ThreadPoolExecutor | None,
        profile_futures: dict[str, Future[str]],
    ) -> None:
        for index, item in enumerate(self._files, start=1):
            if self._stop_event.is_set():
                remaining = [candidate.path for candidate in self._files[index - 1 :]]
                self._file_progress.emit_many("unstarted", "prepare", remaining)
                break
            self._file_progress.emit(item.path, "active", "prepare")
            relative_pdf = _relative_pdf_path(item.path, self._source_root)
            work_output_dir = self._process_work_dir or output_dir
            source_copy_path = work_output_dir / relative_pdf
            source_copy_path.parent.mkdir(parents=True, exist_ok=True)
            record = PdfFileRecord(
                name=item.path.name,
                source_path=str(item.path),
                relative_path=str(relative_pdf),
                source_type=item.source_type,
                source_copy_path=str(source_copy_path),
                source_pdf_size_bytes=_safe_file_size(item.path),
            )
            try:
                shutil.copy2(item.path, source_copy_path)
                logger.debug(f"[PDF] [{item.path.name}] 已复制源文件到过程目录")
            except Exception as exc:  # noqa: BLE001 - file-level failure.
                record.status = PDF_OUTPUT_STATE_FAILED
                logger.debug(f"[PDF] 复制源文件失败原始错误：{exc!r}")
                record.error = f"复制源文件失败：{user_facing_reason(exc, fallback='无法将源文件复制到工作文件夹。')}"
                prepared_files.append(
                    self._prepared_file_shell(
                        item,
                        relative_pdf=relative_pdf,
                        record=record,
                        output_dir=output_dir,
                        app_managed=app_managed,
                    )
                )
                self._file_progress.emit(
                    item.path,
                    "failed",
                    "prepare",
                    result=_file_record_to_result(record),
                )
                continue

            if item.source_type == SOURCE_TYPE_IMAGE:
                try:
                    _validate_source_image(item.path)
                    record.page_count = 1
                except Exception as exc:  # noqa: BLE001 - file-level validation failure.
                    record.status = PDF_OUTPUT_STATE_FAILED
                    logger.debug(f"[PDF] 图片读取失败原始错误：{exc!r}")
                    record.error = f"图片读取失败：{user_facing_reason(exc, fallback='无法打开此图片，文件可能已损坏或格式不受支持。')}"
                if record.status != PDF_OUTPUT_STATE_FAILED and self._image_model_missing:
                    record.status = PDF_OUTPUT_STATE_FAILED
                    record.error = _with_sentence_end(
                        "图片只能用图像翻译模型翻译，还没有设置它：" + self._image_model_missing
                    )
                prepared_files.append(
                    self._prepared_file_shell(
                        item,
                        relative_pdf=relative_pdf,
                        record=record,
                        output_dir=output_dir,
                        app_managed=app_managed,
                    )
                )
                if record.status == PDF_OUTPUT_STATE_FAILED:
                    self._file_progress.emit(
                        item.path,
                        "failed",
                        "prepare",
                        result=_file_record_to_result(record),
                    )
                else:
                    self._file_progress.emit(item.path, "waiting", "translate", total=record.page_count)
                self._queue.put(ProgressMsg(1, 4, "读取文件", index, max(1, len(self._files))))
                continue

            try:
                doc = _open_pdf_document(item.path)
                try:
                    record.page_count = len(doc)
                finally:
                    doc.close()
            except Exception as exc:  # noqa: BLE001 - dependency/file-level failure.
                record.status = PDF_OUTPUT_STATE_FAILED
                record.error = _pdf_read_error_message(exc)
            if record.status != PDF_OUTPUT_STATE_FAILED:
                self._classify_for_route(item, record)
            if (
                record.status != PDF_OUTPUT_STATE_FAILED
                and profile_executor is not None
            ):
                future = self._submit_document_context(profile_executor, item.path)
                if future is not None:
                    profile_futures[str(item.path)] = future

            prepared_files.append(
                self._prepared_file_shell(
                    item,
                    relative_pdf=relative_pdf,
                    record=record,
                    output_dir=output_dir,
                    app_managed=app_managed,
                )
            )
            if record.status == PDF_OUTPUT_STATE_FAILED:
                self._file_progress.emit(
                    item.path,
                    "failed",
                    "prepare",
                    result=_file_record_to_result(record),
                )
            else:
                self._file_progress.emit(item.path, "waiting", "translate", total=record.page_count)
            self._queue.put(ProgressMsg(1, 4, "读取文件", index, max(1, len(self._files))))

    def _classify_for_route(self, item: PdfFileItem, record: PdfFileRecord) -> None:
        """翻译方式不是整页图像翻译时，读文件判断类型，决定整份转 Word 还是逐页处理。"""
        pdf_settings = self._settings.pdf
        if getattr(pdf_settings, "translation_route", "image") == "image":
            return
        threshold = getattr(pdf_settings, "oversized_page_threshold", "A3")
        kind = classify_document(
            item.path,
            is_oversized=lambda width, height: is_oversized_for_threshold(threshold, width, height),
        )
        self._document_kinds[str(item.path)] = kind
        record.document_kind = kind.kind
        label = _DOCUMENT_KIND_LABELS.get(kind.kind, "")
        if label:
            self._log("INFO", f"[{record.name}] 判断为{label}，按页处理。")

    def _page_kind(self, source_path: str, page_index: int) -> str:
        kind = self._document_kinds.get(source_path)
        if kind is None or page_index >= len(kind.pages):
            return ""
        return kind.pages[page_index].kind

    def _prepared_file_shell(
        self,
        item: PdfFileItem,
        *,
        relative_pdf: Path,
        record: PdfFileRecord,
        output_dir: Path,
        app_managed: bool,
    ) -> _PreparedPdfFile:
        # Source copies, page PNGs and review candidates are process artifacts;
        # only assembled PDFs and translated standalone images belong in the
        # user's delivery directory.
        work_output_dir = self._process_work_dir or output_dir
        source_copy_path = work_output_dir / relative_pdf
        record.source_copy_path = str(source_copy_path)
        source_pages_dir, translated_pages_dir = resolve_pdf_page_archive_dirs(
            work_output_dir,
            relative_pdf,
        )
        review_candidates_dir = resolve_pdf_review_candidates_dir(work_output_dir, relative_pdf)
        source_pages_dir.mkdir(parents=True, exist_ok=True)
        translated_pages_dir.mkdir(parents=True, exist_ok=True)
        if self._settings.pdf.review_enabled:
            review_candidates_dir.mkdir(parents=True, exist_ok=True)
        output_source_name = item.path.name
        if (
            item.source_type != SOURCE_TYPE_IMAGE
            and self._settings.pdf_output.translate_output_filename
        ):
            original_stem = item.path.stem
            if original_stem not in self._translated_pdf_names:
                if not self._output_name_engine_failed and self._output_name_engine is None:
                    try:
                        from core.engine_dispatcher import build_engine

                        self._output_name_engine = build_engine(self._settings)
                    except Exception as exc:
                        self._output_name_engine_failed = True
                        logger.debug(f"[PDF] 文件名翻译模型不可用：{exc!r}")
                translated_stem = (
                    translate_output_stem(
                        self._output_name_engine,
                        original_stem,
                        self._settings.pdf.target_lang,
                        "auto",
                    )
                    if self._output_name_engine is not None
                    else original_stem
                )
                self._translated_pdf_names[original_stem] = translated_stem
                if translated_stem == original_stem:
                    self._log("WARN", f"[{item.path.name}] 输出文件名未获得可用译名，沿用原名。")
            output_source_name = self._translated_pdf_names[original_stem] + item.path.suffix
        translated_pdf_path, compressed_pdf_path = resolve_translated_pdf_variant_paths(
            output_dir / relative_pdf.parent,
            output_source_name,
            self._settings.pdf.target_lang,
            self._settings,
            app_managed=app_managed,
        )
        return _PreparedPdfFile(
            item=item,
            relative_pdf=relative_pdf,
            record=record,
            source_pages_dir=source_pages_dir,
            translated_pages_dir=translated_pages_dir,
            review_candidates_dir=review_candidates_dir,
            translated_pdf_path=translated_pdf_path,
            compressed_pdf_path=compressed_pdf_path,
            app_managed=app_managed,
            final_output_dir=output_dir,
        )

    def _prefill_resume_pages(
        self,
        prepared_files: list[_PreparedPdfFile],
        *,
        output_dir: Path,
    ) -> None:
        """Adopt the finished pages of the previous output package.

        The resume directory is only ever read: every adopted page is copied into
        this task's own archive first, so the historical package stays untouched
        and every path the page panel and the assembler see still resolves inside
        this task's own folders.  Any doubt about that package — missing manifest,
        different target language, a source file that changed since — degrades to
        a normal full translation with a log line, never to a task error.
        """
        if not self._resume_output_dir:
            return
        resume_path = Path(self._resume_output_dir)
        if _path_is_within(resume_path, Path(output_dir)) or _path_is_within(
            Path(output_dir), resume_path
        ):
            self._log("WARN", "继续上次翻译：上次的输出文件夹与本次重叠，本次将全部重新翻译。")
            return
        manifest = read_pdf_resume_manifest(resume_path)
        if manifest is None:
            self._log(
                "WARN",
                "继续上次翻译：无法读取上次输出文件夹中的进度记录（可能已被移动、删除或损坏），本次将全部重新翻译。",
            )
            return
        previous_lang = str(manifest.get("target_lang") or "")
        current_lang = str(self._settings.pdf.target_lang or "")
        if previous_lang and current_lang and previous_lang != current_lang:
            self._log(
                "WARN",
                f"继续上次翻译：上次的目标语言（{previous_lang}）与本次（{current_lang}）不一致，本次将全部重新翻译。",
            )
            return
        try:
            resume_archive_path = Path(
                str(manifest.get("_resume_process_dir") or resume_path)
            )
            reusable = reusable_pdf_pages(manifest, resume_archive_path)
        except Exception as exc:  # noqa: BLE001 - resume is best-effort by design.
            logger.debug(f"[PDF] 读取上次输出页存档失败原始错误：{exc!r}")
            self._log("WARN", "继续上次翻译：无法读取上次输出的页面存档，本次将全部重新翻译。")
            return

        manifest_files = _resume_manifest_files(manifest)
        total_pages = 0
        total_reused = 0
        for prepared in prepared_files:
            record = prepared.record
            total_pages += max(0, int(record.page_count or 0))
            if record.status == PDF_OUTPUT_STATE_FAILED or record.page_count <= 0:
                continue
            key = Path(record.relative_path).as_posix()
            entry = manifest_files.get(key)
            pages = reusable.get(key) or []
            if entry is None or not pages:
                continue
            previous_page_count = _resume_int(entry.get("page_count"))
            if previous_page_count != record.page_count:
                self._log(
                    "WARN",
                    f"[{record.name}] 继续上次翻译：源文件现有 {record.page_count} 页，上次记录为 {previous_page_count} 页，"
                    "此文件将全部重新翻译。",
                )
                continue
            previous_size = _resume_int(entry.get("source_pdf_size_bytes"))
            if (
                previous_size
                and record.source_pdf_size_bytes
                and previous_size != record.source_pdf_size_bytes
            ):
                self._log(
                    "WARN",
                    f"[{record.name}] 继续上次翻译：源文件在上次翻译后已被修改，此文件将全部重新翻译。",
                )
                continue
            reused = self._adopt_resume_pages(prepared, pages)
            if reused:
                total_reused += reused
                self._log(
                    "INFO",
                    f"[{record.name}] 继续上次翻译：复用上次已完成的 {reused} 页，"
                    f"本次仅需生成 {max(0, record.page_count - reused)} 页。",
                )

        if not total_reused:
            self._log("INFO", "继续上次翻译：上次输出中没有可复用的已完成页面，本次将全部重新翻译。")
            return
        with self._page_status_lock:
            # 复用页按「已提交且已完成」记账，和大幅面跳过页同一条口径，否则「N/M 已完成」
            # 会永远差这几页。它们没有调用过模型，_api_call_count 不动。
            self._submitted_page_count += total_reused
            self._completed_page_count += total_reused
            self._emit_page_status_locked()
        self._log(
            "INFO",
            f"继续上次翻译：复用上次已完成的 {total_reused} 页，"
            f"本次仅需生成 {max(0, total_pages - total_reused)} 页（复用的页面不调用模型）。",
        )

    def _adopt_resume_pages(
        self,
        prepared: _PreparedPdfFile,
        pages: list[ResumePageSource],
    ) -> int:
        record = prepared.record
        existing = {page.page_number for page in record.pages}
        adopted = 0
        for source in pages:
            if source.page_number in existing:
                continue
            target = prepared.translated_pages_dir / source.translated_image_path.name
            try:
                prepared.translated_pages_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source.translated_image_path, target)
            except Exception as exc:  # noqa: BLE001 - the page is simply re-translated.
                logger.debug(f"[PDF] 复用上次译图失败原始错误：{exc!r}")
                self._log(
                    "WARN",
                    f"[{record.name}] 继续上次翻译：第 {source.page_number} 页的旧译图复制失败，此页将重新生成。",
                )
                continue
            source_copy = ""
            if source.source_image_path is not None:
                # 源页图只影响逐页面板的原文/译文对照，缺了不影响装订，所以失败不退页。
                try:
                    prepared.source_pages_dir.mkdir(parents=True, exist_ok=True)
                    source_target = prepared.source_pages_dir / source.source_image_path.name
                    shutil.copy2(source.source_image_path, source_target)
                    source_copy = str(source_target)
                except Exception as exc:  # noqa: BLE001 - cosmetic copy only.
                    logger.debug(f"[PDF] 复用上次源页图失败原始错误：{exc!r}")
            record.pages.append(
                PdfPageRecord(
                    page_number=source.page_number,
                    source_image_path=source_copy,
                    file_name=record.name,
                    translated_image_path=str(target),
                    status="success",
                    # 复用页这一次没有调用过模型：attempts 记 1、审核记「未审核」，
                    # 否则上一轮的重试数和审核数会被算进这一轮的指标里。
                    attempts=1,
                    ratio_letterboxed=source.ratio_letterboxed,
                    render_dpi=source.render_dpi,
                    source_width_px=source.source_width_px,
                    source_height_px=source.source_height_px,
                    output_width_px=source.output_width_px,
                    output_height_px=source.output_height_px,
                    page_width_pt=source.page_width_pt,
                    page_height_pt=source.page_height_pt,
                )
            )
            existing.add(source.page_number)
            adopted += 1
        if adopted:
            record.pages.sort(key=lambda item: item.page_number)
        return adopted

    def _process_prepared_pages(
        self,
        prepared_files: list[_PreparedPdfFile],
        *,
        max_attempts: int,
        scheduler: WeightedApiScheduler,
        review_scheduler: WeightedApiScheduler,
        model_config,
        review_model_config,
        concurrency: int,
        total_pages: int,
    ) -> None:
        futures: dict[Any, tuple[_PreparedPdfFile, PdfPageRecord]] = {}
        max_pending = max(1, concurrency + PDF_PAGE_RENDER_AHEAD_COUNT)
        producer_done = False
        stop_logged = False
        page_iter = self._iter_rendered_pages(prepared_files)
        # 起点不是 0：续译复用的页在进这里之前就已经记完成了，写 0 会让进度条先跳回去。
        self._push_translation_progress(total_pages)
        # 有页在生成、却很久没有一页返回时，运行日志说一声在等几页（与逐批日志同一套）。
        wait_notice = RequestWaitNotice(
            self._log,
            self._page_requests,
            template=PDF_PAGE_WAIT_TEMPLATE,
            thread_name="pdf-page-wait-log",
        )
        # 计划事件把「上次返回」的起点拨到现在：前面渲染、续译核对花的时间不算等待。
        self._page_requests.on_event(
            BatchRequestEvent(BATCH_EVENT_PLAN, batch_total=total_pages, items=total_pages, concurrency=concurrency)
        )
        wait_notice.start()

        try:
            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                while not producer_done or futures:
                    while (
                        not producer_done
                        and not self._stop_event.is_set()
                        and not self._pause_event.is_set()
                        and len(futures) < max_pending
                    ):
                        try:
                            prepared, page_record = next(page_iter)
                        except StopIteration:
                            producer_done = True
                            break
                        if prepared.record.status == PDF_OUTPUT_STATE_FAILED:
                            continue
                        self._file_progress.emit(
                            prepared.item.path,
                            "active",
                            "translate",
                            completed=self._settled_file_page_count(prepared.record),
                            total=prepared.record.page_count,
                        )
                        prepared.record.pages.append(page_record)
                        if page_record.route_note:
                            self._log(
                                "WARN",
                                f"[{prepared.record.name}] 第 {page_record.page_number} 页：{page_record.route_note}。",
                            )
                        if page_record.skipped_oversize:
                            # 大幅面页：不进 executor.submit，直接按「已提交且已
                            # 完成」记账，否则「N/M 已完成」的进度会永远差这几页。
                            long_mm = _pt_to_mm(
                                max(page_record.page_width_pt, page_record.page_height_pt)
                            )
                            short_mm = _pt_to_mm(
                                min(page_record.page_width_pt, page_record.page_height_pt)
                            )
                            if page_record.kept_reason:
                                self._log(
                                    "WARN",
                                    f"[{prepared.record.name}] 第 {page_record.page_number} 页：{page_record.kept_reason}。",
                                )
                            elif page_record.word_requested:
                                pass  # 正文段已经转好，开工前那一行日志说过了
                            elif page_record.inplace_requested:
                                self._log(
                                    "INFO",
                                    f"[{prepared.record.name}] 第 {page_record.page_number} 页（{short_mm:.0f}×{long_mm:.0f} mm）"
                                    "将原位替换文字（Beta），在整份文件翻译完后处理。",
                                )
                            else:
                                self._log(
                                    "INFO",
                                    f"[{prepared.record.name}] 第 {page_record.page_number} 页为大幅面（{short_mm:.0f}×{long_mm:.0f} mm），不予翻译，"
                                    "原页将直接放入输出 PDF。",
                                )
                            self._record_page_submitted()
                            self._record_page_completed(page_record)
                            self._emit_file_page_progress(
                                prepared,
                                outstanding=self._file_outstanding_future_count(prepared, futures),
                            )
                            self._push_translation_progress(total_pages)
                            continue
                        if page_record.status == "placeholder_pending":
                            # 渲染阶段就坏掉的页：没有源页图可以交给模型，直接按
                            # 「已提交且已失败」记账，走占位页流程，不占用重试和配额。
                            self._record_page_submitted()
                            self._record_page_placeholder(page_record)
                            self._record_page_completed(page_record)
                            self._emit_file_page_progress(
                                prepared,
                                outstanding=self._file_outstanding_future_count(prepared, futures),
                            )
                            self._push_translation_progress(total_pages)
                            continue
                        self._log(
                            "INFO",
                            f"[{prepared.record.name}] 第 {page_record.page_number} 页已渲染",
                            visible=False,
                        )
                        future = executor.submit(
                            self._generate_page_with_retries,
                            page_record,
                            prepared.record.page_count,
                            prepared.translated_pages_dir,
                            prepared.review_candidates_dir,
                            max_attempts,
                            scheduler,
                            review_scheduler,
                            model_config,
                            review_model_config,
                            source_type=prepared.record.source_type,
                            document_context=self._document_contexts.get(prepared.record.source_path, ""),
                        )
                        futures[future] = (prepared, page_record)
                        self._file_progress.emit(
                            prepared.item.path,
                            "active",
                            "translate",
                            completed=self._settled_file_page_count(prepared.record),
                            total=prepared.record.page_count,
                        )
                        self._record_page_submitted()
                        self._log(
                            "INFO",
                            f"[{prepared.record.name}] 第 {page_record.page_number} 页已提交图像生成",
                            visible=False,
                        )

                    if self._stop_event.is_set() and not stop_logged:
                        stop_logged = True
                        self._stop_wait_started_at = time.monotonic()
                        self._log("WARN", "正在停止：不再开始新的页面，正在等待处理中的页面结束。")
                        self._emit_stop_wait_status(force=True)
                    elif self._stop_event.is_set() and stop_logged:
                        # 等待期间没有任何页跑完也要让状态行往前走（见 _stop_wait_status）。
                        self._emit_stop_wait_status()
                    elif not self._stop_event.is_set() and stop_logged:
                        stop_logged = False
                        self._stop_wait_started_at = None

                    if self._pause_event.is_set() and not futures:
                        for prepared in prepared_files:
                            if not self._record_has_all_pages_finished(prepared.record):
                                self._file_progress.emit(
                                    prepared.item.path,
                                    "paused",
                                    "translate",
                                    completed=self._settled_file_page_count(prepared.record),
                                    total=prepared.record.page_count,
                                )
                        return

                    if not futures:
                        break

                    done, _ = wait(
                        list(futures.keys()),
                        timeout=0.2,
                        return_when=FIRST_COMPLETED,
                    )
                    for future in done:
                        prepared, page_record = futures.pop(future)
                        try:
                            updated = future.result()
                            _copy_page_record(updated, page_record)
                        except ImageModelUnavailableError as exc:
                            page_record.status = PDF_OUTPUT_STATE_FAILED
                            page_record.error = str(exc)
                            page_record.attempts = max(1, page_record.attempts)
                            self._stop_event.set()
                            self._fatal_model_error = str(exc)
                            raise
                        except PdfReviewModelUnavailableError as exc:
                            page_record.status = PDF_OUTPUT_STATE_FAILED
                            page_record.error = str(exc)
                            page_record.attempts = max(1, page_record.attempts)
                            self._stop_event.set()
                            self._fatal_model_error = str(exc)
                            self._fatal_review_model_error = str(exc)
                            raise
                        except Exception as exc:  # noqa: BLE001 - page-level unknown failure.
                            logger.debug(
                                f"[PDF] 第 {page_record.page_number} 页失败原始错误：{exc!r}"
                            )
                            page_record.status = "placeholder_pending"
                            page_record.placeholder = True
                            page_record.error = user_facing_reason(
                                exc,
                                fallback="这一页处理失败，已改为失败占位页。",
                            )
                            page_record.attempts = max_attempts
                            self._record_page_placeholder(page_record)
                        if page_record.status == PDF_PAGE_STATUS_STOPPED_UNSTARTED:
                            # 中止时没跑出结果的页：撤掉页记录，让它回到「还没开始」。
                            # 这是「文件是否全部页面已完成」唯一的判据，也是报告里
                            # 「未开始页数」的来源。
                            self._discard_stopped_page(prepared, page_record)
                            self._push_translation_progress(total_pages)
                            continue
                        self._record_page_completed(page_record)
                        self._emit_file_page_progress(
                            prepared,
                            outstanding=self._file_outstanding_future_count(prepared, futures),
                        )
                        self._push_translation_progress(total_pages)

            for prepared in prepared_files:
                if prepared.record.status == PDF_OUTPUT_STATE_FAILED:
                    self._file_progress.emit(
                        prepared.item.path,
                        "failed",
                        "extract",
                        completed=self._settled_file_page_count(prepared.record),
                        total=prepared.record.page_count,
                        result=_file_record_to_result(prepared.record),
                    )

        finally:
            # 致命错误（模型/审核模型不可用）是直接 raise 出去的，收敛循环再也回不来；
            # 还在飞的页由执行器 __exit__ 等回来，工作线程就地把它们标成
            # stopped_unstarted。这个状态只是「撤回这条页记录」的内部信号，一旦跟着
            # 异常留在 record.pages 里，就会被写进清单、送进逐页面板——前端不认识它，
            # 会把一张连译文图都没有的页显示成「已跑完·未审核」，报告的「未开始页面」
            # 也漏计。所以不论从哪条路离开，都在这里统一撤干净。
            self._discard_stopped_pages(prepared_files)
            wait_notice.stop()

    @staticmethod
    def _page_render_phase(record: PdfFileRecord) -> str:
        """File-progress phase while one of this file's pages is being rendered.

        Only the first page reads as「渲染页面」. Pages are rendered one at a time
        between translations, so reporting every render would flip the file's
        chip between「渲染页面」and「翻译内容」for each page of a long PDF.
        """
        return "translate" if record.pages else "render"

    def _push_translation_progress(self, total_pages: int) -> None:
        """Emit the "N / total 已完成" progress pair used by both the normal
        future-completion path and the skipped-oversize-page fast path."""
        self._queue.put(
            ProgressMsg(
                2,
                4,
                "翻译",
                self._completed_page_count,
                max(1, total_pages),
            )
        )
        if self._stop_event.is_set():
            self._emit_stop_wait_status(force=True)
            return
        self._queue.put(
            StatusMsg(
                f"状态：正在翻译 PDF 页面，已完成 {self._completed_page_count} / {total_pages} 页。"
            )
        )

    def _iter_rendered_pages(self, prepared_files: list[_PreparedPdfFile]):
        needs_pdf_rendering = any(
            prepared.record.source_type != SOURCE_TYPE_IMAGE
            and prepared.record.status != PDF_OUTPUT_STATE_FAILED
            and prepared.record.page_count > 0
            for prepared in prepared_files
        )
        if needs_pdf_rendering:
            try:
                _load_pdfium()
            except Exception as exc:  # noqa: BLE001 - dependency failure.
                for prepared in prepared_files:
                    if prepared.record.source_type != SOURCE_TYPE_IMAGE and prepared.record.page_count:
                        prepared.record.status = PDF_OUTPUT_STATE_FAILED
                        logger.debug(f"[PDF] PDF 渲染组件不可用原始错误：{exc!r}")
                        prepared.record.error = (
                            "PDF 页面渲染组件加载失败，无法处理 PDF；请重新安装程序后重试。"
                        )
                return

        for prepared in prepared_files:
            record = prepared.record
            if record.status == PDF_OUTPUT_STATE_FAILED or record.page_count <= 0:
                continue
            self._file_progress.emit(
                prepared.item.path,
                "active",
                self._page_render_phase(record),
                completed=self._settled_file_page_count(record),
                total=record.page_count,
            )
            if record.source_type == SOURCE_TYPE_IMAGE:
                existing_pages = {page.page_number for page in record.pages}
                if 1 in existing_pages:
                    continue
                try:
                    yield (
                        prepared,
                        self._render_source_image(
                            prepared.item.path,
                            source_pages_dir=prepared.source_pages_dir,
                            file_name=record.name,
                        ),
                    )
                except Exception as exc:  # noqa: BLE001 - file-level render failure.
                    record.status = PDF_OUTPUT_STATE_FAILED
                    logger.debug(f"[PDF] 图片读取失败原始错误：{exc!r}")
                    record.error = f"图片读取失败：{user_facing_reason(exc, fallback='无法打开此图片，文件可能已损坏或格式不受支持。')}"
                    self._log("ERROR", f"[{record.name}] {record.error}")
                continue
            try:
                doc = _open_pdf_document(prepared.item.path)
                try:
                    existing_pages = {page.page_number for page in record.pages}
                    if existing_pages:
                        remaining_count = max(0, record.page_count - len(existing_pages))
                        self._log(
                            "INFO",
                            f"[{record.name}] 已跳过已完成页面 {len(existing_pages)} 页，继续处理剩余 {remaining_count} 页。",
                        )
                    for page_index in range(record.page_count):
                        page_number = page_index + 1
                        if page_number in existing_pages:
                            continue
                        self._file_progress.emit(
                            prepared.item.path,
                            "active",
                            self._page_render_phase(record),
                            completed=self._settled_file_page_count(record),
                            total=record.page_count,
                        )
                        try:
                            page_record = self._render_source_page(
                                doc,
                                page_index=page_index,
                                page_count=record.page_count,
                                source_pages_dir=prepared.source_pages_dir,
                                file_name=record.name,
                                source_pdf=record.source_copy_path,
                                source_path_key=str(prepared.item.path),
                            )
                        except Exception as exc:  # noqa: BLE001 - one broken page only costs that page.
                            # 一页对象损坏绝不能拖垮整份文件：第 180 页读不出来，
                            # 前 179 页已经翻译好的结果照样要装配成 PDF。坏页走
                            # 占位页流程，文件整体标 needs_review。
                            page_record = self._broken_page_record(
                                doc,
                                page_index=page_index,
                                file_name=record.name,
                                error=exc,
                            )
                            self._log(
                                "ERROR",
                                f"[{record.name}] 无法读取第 {page_number} 页，已改为失败占位页，其余页面继续处理："
                                f"{user_facing_reason(exc, fallback='无法从源文件读取此页。')}",
                            )
                        yield (prepared, page_record)
                finally:
                    doc.close()
            except Exception as exc:  # noqa: BLE001 - file-level render failure.
                record.status = PDF_OUTPUT_STATE_FAILED
                logger.debug(f"[PDF] PDF 渲染失败原始错误：{exc!r}")
                record.error = f"PDF 页面读取失败：{user_facing_reason(exc, fallback='无法读取此 PDF 的页面。')}"
                self._log("ERROR", f"[{record.name}] {record.error}")

    def _broken_page_record(
        self,
        doc,
        *,
        page_index: int,
        file_name: str,
        error: BaseException,
    ) -> PdfPageRecord:
        """Build the placeholder-bound record for a page that would not render.

        The page geometry still has to be plausible, or assembly would emit a
        0×0 page: try the page's own size first, fall back to A4 portrait.
        """
        width_pt, height_pt = PDF_A4_SHORT_EDGE_PT, PDF_A4_LONG_EDGE_PT
        try:
            page = doc.get_page(page_index)
            try:
                size = page.get_size()
            finally:
                page.close()
            if size and float(size[0]) > 0 and float(size[1]) > 0:
                width_pt, height_pt = float(size[0]), float(size[1])
        except Exception:  # noqa: BLE001 - the page is broken; defaults are the point.
            pass
        scale = _pdf_render_scale()
        return PdfPageRecord(
            page_number=page_index + 1,
            source_image_path="",
            file_name=file_name,
            status="placeholder_pending",
            placeholder=True,
            error=f"页面读取/渲染失败：{user_facing_reason(error, fallback='无法从源文件读取此页。')}",
            source_width_px=int(max(1, round(width_pt * scale))),
            source_height_px=int(max(1, round(height_pt * scale))),
            page_width_pt=float(width_pt),
            page_height_pt=float(height_pt),
        )

    def _finalize_file_record(
        self,
        prepared: _PreparedPdfFile,
        *,
        should_assemble: bool,
    ) -> None:
        record = prepared.record
        # 中止和失败这两条路以前直接 return，逐页计数一次都没刷新过：清单里 generated_page_count
        # 永远是 0，报告写「页面完成进度 12/14」（那其实是已提交数），而界面同一时刻写着
        # 「已生成 9 / 14 页」——三个口径三个数。计数只有一处实现，这两条路也得走一遍。
        if record.status == PDF_OUTPUT_STATE_FAILED:
            self._refresh_file_record_counts(record)
            return
        if not should_assemble:
            record.status = PDF_OUTPUT_STATE_STOPPED
            self._refresh_file_record_counts(record)
            self._log(
                "WARN",
                f"[{record.name}] 尚有页面未翻译完成，本次未生成此文件。",
            )
            return

        if record.source_type != SOURCE_TYPE_IMAGE:
            self._finalize_placeholders(record, prepared.translated_pages_dir)
        self._refresh_file_record_counts(record)
        if not _record_has_usable_translated_pages(record):
            record.status = PDF_OUTPUT_STATE_FAILED
            record.error = _no_usable_translated_pages_error(record)
            self._log("ERROR", f"[{record.name}] {record.error}")
            return
        if record.source_type == SOURCE_TYPE_IMAGE:
            self._finalize_image_record(record, prepared)
            return

        self._run_inplace_pages(record, prepared)
        self._refresh_file_record_counts(record)
        try:
            self._assemble_translated_pdf(record, prepared.translated_pdf_path)
            record.translated_pdf_path = str(prepared.translated_pdf_path)
            self._save_word_copy(record, prepared)
            record.high_quality_pdf_size_bytes = _safe_file_size(prepared.translated_pdf_path)
            # 这一轮重新装配，上一轮的压缩结论（成功路径与失败原因）全部作废：
            # 单页重生成不再预删旧产物，不在这里显式收口的话，旧压缩版会连同旧的
            # compression_error 一起被当成本轮的结果继续挂在记录上。
            compressed_built = False
            record.compression_error = ""
            if self._settings.pdf.generate_compressed_pdf:
                try:
                    self._assemble_translated_pdf(
                        record,
                        prepared.compressed_pdf_path,
                        compressed=True,
                    )
                    record.compressed_pdf_path = str(prepared.compressed_pdf_path)
                    record.compressed_pdf_size_bytes = _safe_file_size(
                        prepared.compressed_pdf_path
                    )
                    compressed_built = True
                    self._log(
                        "OK",
                        f"[{record.name}] 已生成压缩 PDF：{prepared.compressed_pdf_path.name}",
                    )
                except Exception as exc:  # noqa: BLE001 - compressed output is optional.
                    logger.debug(f"[PDF] 压缩 PDF 生成失败原始错误：{exc!r}")
                    record.compression_error = user_facing_reason(
                        exc,
                        fallback="压缩版 PDF 未能生成，高清版不受影响。",
                    )
                    self._log(
                        "WARN",
                        f"[{record.name}] 压缩 PDF 生成失败，已保留高清版："
                        f"{record.compression_error}",
                    )
            if _record_is_fully_skipped_oversize(record):
                # 产物只是源文件的原样副本，一个字都没翻。这种情况下报
                # 「翻译完成」是在骗用户，必须直说。
                record.error = fully_skipped_oversize_message(record)
                self._log("WARN", f"[{record.name}] {record.error}")
            elif _record_translated_nothing(record):
                # 同样一个字没翻，但原因不是幅面（例如原位替换认出的字都没能确认）。
                record.error = translated_nothing_message(record)
                self._log("WARN", f"[{record.name}] {record.error}")
            record.status = (
                PDF_OUTPUT_STATE_NEEDS_REVIEW
                if _record_needs_review(record)
                else PDF_OUTPUT_STATE_COMPLETED
            )
            if not compressed_built:
                # 清理放在状态落定之后：这样单页重生成失败要回滚时，磁盘上什么
                # 都还没被删过（那正是 高-5 的病根，不能为清残留把它请回来）。
                self._discard_superseded_compressed_pdf(record, prepared)
            self._log("OK", f"[{record.name}] 已生成高清 PDF：{prepared.translated_pdf_path.name}")
        except Exception as exc:  # noqa: BLE001 - file-level failure.
            record.status = PDF_OUTPUT_STATE_FAILED
            logger.debug(f"[PDF] PDF 合成失败原始错误：{exc!r}")
            record.error = f"生成 PDF 失败：{user_facing_reason(exc, fallback='已翻译的页面未能合成 PDF。')}"
            self._log("ERROR", f"[{record.name}] {record.error}")

    def _save_word_copy(self, record: PdfFileRecord, prepared: _PreparedPdfFile) -> None:
        """「同时输出 Word」：整份文件（空白页除外）都在同一段里转 Word 翻好时，把那份
        Word 放到译文 PDF 旁边。混合文件不出 Word：拆成几段的 Word 拼不回一份文件。"""
        if not getattr(self._settings.pdf, "also_save_word", False):
            return
        done = [item for item in record.word_segments if item.get("status") == "done" and item.get("docx_path")]
        kind = self._document_kinds.get(str(prepared.item.path))
        if len(done) != 1 or kind is None:
            return
        first, last = int(done[0]["first_page"]), int(done[0]["last_page"])
        if any(not first <= page.page_number <= last for page in kind.pages if page.kind != KIND_BLANK):
            return
        target = Path(prepared.translated_pdf_path).with_suffix(".docx")
        try:
            shutil.copy2(done[0]["docx_path"], target)
        except OSError as exc:
            self._log("WARN", f"[{record.name}] 另存 Word 失败：" + user_facing_reason(exc, fallback="无法写入输出文件夹。"))
            return
        record.translated_docx_path = str(target)
        self._log("OK", f"[{record.name}] 另存了一份 Word：{target.name}")

    def _planned_word_pages(self, prepared: _PreparedPdfFile) -> list[int]:
        """自动组合里首选转 Word 的页（可提取文字的页，且设置成转 Word）。"""
        record = prepared.record
        if (getattr(self._settings.pdf, "translation_route", "image") != "auto"
                or record.source_type == SOURCE_TYPE_IMAGE
                or record.status == PDF_OUTPUT_STATE_FAILED):
            return []
        kind = self._document_kinds.get(str(prepared.item.path))
        if kind is None:
            return []
        # 「继续上次翻译」已经复用了译好页图的页不再转 Word：段首会把整段转回的 PDF
        # 拼进来，复用的页再单独装一次，内容就重复了，还白花一次翻译。段在这些页处断开。
        reused = {page.page_number for page in record.pages}
        planned: list[int] = []
        blanks: list[int] = []
        for item in sorted(kind.pages, key=lambda page: page.page_number):
            if item.page_number in reused:
                blanks = []
                planned.append(-1)  # 断开
                continue
            if item.kind == KIND_BLANK and not item.oversized:
                blanks.append(item.page_number)
                continue
            if page_route(self._settings.pdf, oversized_a3=False, oversized=item.oversized,
                          page_kind=item.kind) == ROUTE_WORD:
                # 夹在两页转 Word 的页中间的空白页随段一起转：一份文字文件中间插一页
                # 空白，不该被拆成两段（拆开就出不了「同时输出 Word」）。
                if planned and planned[-1] > 0:
                    planned.extend(blanks)
                planned.append(item.page_number)
            else:
                planned.append(-1)
            blanks = []
        return [number for number in planned if number > 0]

    def _terms_result(self) -> dict[str, Any]:
        """任务详情里的「记忆库」一节：转 Word 的各段正文 + 原位替换的各页，合成一份。"""
        from core.term_session import merge_term_results

        parts: list[dict[str, Any]] = list(self._word_terms)
        memory = getattr(self._inplace_hooks, "memory", None)
        if memory is not None:
            try:
                parts.append(memory.finish())
            except Exception as exc:  # noqa: BLE001 - statistics never fail a finished task
                logger.warning(f"[PDF] 汇总记忆库结果失败：{exc!r}")
        return merge_term_results(parts)

    def _collect_word_terms(self, terms: dict[str, Any], where: str) -> None:
        """一段转 Word 翻译的「记忆库」结果：位置前面补上文件名和页码。"""
        if not terms:
            return
        terms = dict(terms)
        not_applied = []
        for item in terms.get("not_applied") or []:
            if not isinstance(item, dict):
                continue
            locations = [str(value) for value in item.get("locations") or [] if value]
            item = {**item, "locations": [f"{where} {value}" for value in locations] or [where]}
            not_applied.append(item)
        terms["not_applied"] = not_applied
        self._word_terms.append(terms)

    def _run_word_segments(self, prepared_files: list[_PreparedPdfFile]) -> None:
        """自动组合（Beta）：在页面翻译开始前，把每份文件里连续的正文页合成一段，转 Word
        翻译，再转回 PDF。转好的段在装配时按原页序拼进来；转不成的页随后按保底方式
        处理（见 _decide_page_route）。转出来的 Word 只留在过程文件夹里。"""
        from core.pdf_word_route import OfficePdfError, docx_to_pdf, translate_pdf_via_word

        plans = [(prepared, self._planned_word_pages(prepared)) for prepared in prepared_files]
        plans = [(prepared, pages) for prepared, pages in plans if pages]
        if not plans:
            return
        unavailable = self._word_segments_possible()
        hooks = self._inplace_hooks
        for prepared, page_numbers in plans:
            record = prepared.record
            key = str(prepared.item.path)
            segments: list[list[int]] = []
            for number in page_numbers:
                if segments and segments[-1][-1] == number - 1:
                    segments[-1].append(number)
                else:
                    segments.append([number])
            for index, seg in enumerate(segments, start=1):
                first, last = seg[0], seg[-1]
                label = f"第 {first} 页" if first == last else f"第 {first}–{last} 页"
                entry: dict[str, Any] = {"segment": index, "first_page": first, "last_page": last,
                                         "docx_path": "", "pdf_path": "", "status": "", "message": "",
                                         "issue_count": 0, "engine": ""}
                record.word_segments.append(entry)

                def mark(status: str, message: str = "", note: str = "") -> None:
                    entry.update(status=status, message=message)
                    for number in seg:
                        self._word_pages[(key, number)] = {"segment": index, "status": status,
                                                           "message": message, "note": note}

                if unavailable:
                    mark("failed", unavailable)
                    continue
                if not self._wait_while_paused():
                    mark("stopped", "任务已停止")
                    continue
                self._queue.put(StatusMsg(f"状态：正在把「{record.name}」{label}转成 Word 翻译…"))
                self._log("INFO", f"[{record.name}] {label}的文字可以提取，先转成 Word 翻译（Beta）。")
                work = Path(prepared.translated_pages_dir) / "word" / f"segment_{index:02d}"
                work.mkdir(parents=True, exist_ok=True)
                stem = Path(record.name).stem
                seg_pdf = work / f"{stem}_p{first}-{last}.pdf"
                try:
                    _extract_pdf_pages(Path(record.source_copy_path), seg_pdf, first, last)
                    result = translate_pdf_via_word(
                        seg_pdf, work, work / f"{stem}_p{first}-{last}_translated.docx", self._settings,
                        source_lang=str(getattr(hooks, "source_lang", "") or self._settings.source_lang or "auto"),
                        should_stop=self._stop_event.is_set, log=self._log,
                        file_label=f"{record.name} {label}", key_overrides=self._key_overrides,
                        term_file_paths=[Path(record.source_path)],
                    )
                    self._collect_word_terms(result.terms, f"{record.name} {label}")
                    if not result.ok:
                        mark("stopped" if result.stopped else "failed",
                             "任务已停止" if result.stopped else "转成 Word 翻译失败")
                        if not result.stopped:
                            self._log("WARN", f"[{record.name}] {label}转成 Word 翻译失败：{result.error}")
                        continue
                    translated_pdf = work / f"{stem}_p{first}-{last}_translated.pdf"
                    engine = docx_to_pdf(Path(result.output_path), translated_pdf)
                except OfficePdfError as exc:
                    mark("failed", "译好的正文转回 PDF 失败")
                    self._log("WARN", f"[{record.name}] {label}译好的正文转回 PDF 失败：{str(exc)[:200]}")
                    continue
                except Exception as exc:  # noqa: BLE001 - one segment only costs that segment
                    logger.exception("[PDF] 正文段转 Word 翻译失败")
                    mark("failed", "转成 Word 翻译出错")
                    self._log("WARN", f"[{record.name}] {label}转成 Word 翻译出错："
                              + user_facing_reason(exc, fallback="处理这几页时出错。"))
                    continue
                entry.update(docx_path=result.output_path, pdf_path=str(translated_pdf),
                             issue_count=len(result.issues), engine=engine)
                record.word_issue_count += len(result.issues)
                mark("done", note=f"{label}的文字可以提取，转成 Word 翻译后转回 PDF 拼入")
                self._log("OK", f"[{record.name}] {label}已转成 Word 翻译，并转回 PDF，装配时按原页序拼入。")

    def _run_inplace_pages(self, record: PdfFileRecord, prepared: _PreparedPdfFile) -> None:
        """「原位替换文字（Beta）」：逐页删字写回；失败或自检不过的页保留原页。"""
        # 只处理还没试过的页。单页重新生成、「换回上一版」也会走到这里，它们不该
        # 顺带把别的原位替换页重跑一遍（又花一分钟、又调一次模型）。
        pages = [page for page in record.pages if page.inplace_requested and not page.inplace_status]
        if not pages or self._inplace_hooks is None:
            return
        from core.pdf_vector import translate_page_inplace

        hooks = self._inplace_hooks
        if not str(self._settings.pdf.target_lang or "").lower().startswith("zh"):
            for page in pages:
                page.inplace_status = "kept_original"
                page.inplace_message = "原位替换文字（Beta）目前只支持译成中文，这一页保留原样"
            self._log("WARN", f"[{record.name}] 原位替换文字（Beta）目前只支持译成中文，相关页面保留原样。")
            return
        work_root = Path(prepared.translated_pages_dir) / "inplace"
        for page in sorted(pages, key=lambda item: item.page_number):
            if self._stop_event.is_set():
                page.inplace_status = "stopped"
                page.inplace_message = "任务已停止，这一页保留原样"
                continue
            self._log("INFO", f"[{record.name}] 第 {page.page_number} 页：原位替换文字（Beta）开始，大图约需 1–2 分钟。")
            work = work_root / f"page_{page.page_number:04d}"
            # 判成普通文字页的，字都在文字层里，不用再跑文字识别（图纸页、大幅面页
            # 常有画成线条的字，照常识别）。
            native_only = self._page_kind(record.source_path, page.page_number - 1) == KIND_TEXT or (
                not self._page_kind(record.source_path, page.page_number - 1)
                and not is_oversized_for_threshold(
                    getattr(self._settings.pdf, "oversized_page_threshold", "A3"),
                    page.page_width_pt, page.page_height_pt,
                )
            )
            try:
                result = translate_page_inplace(
                    Path(record.source_copy_path),
                    page.page_number - 1,
                    work,
                    translate=_with_stop(hooks.translate, self._stop_event.is_set),
                    profile=hooks.profile,
                    tesseract=hooks.tesseract,
                    tesseract_languages=hooks.tesseract_languages,
                    source_lang=hooks.source_lang,
                    log=self._log,
                    native_only=native_only,
                    memory=getattr(hooks, "memory", None),
                    memory_file=Path(record.source_path),
                    memory_location=f"{record.name} 第 {page.page_number} 页",
                )
            except Exception as exc:  # noqa: BLE001 - one page only costs that page
                logger.debug(f"[PDF] 原位替换失败：{exc!r}")
                page.inplace_status = "kept_original"
                page.inplace_message = "原位替换出错，这一页保留原样：" + user_facing_reason(exc, fallback="处理这一页时出错。")
                self._log("WARN", f"[{record.name}] 第 {page.page_number} 页{page.inplace_message}")
                continue
            page.inplace_written = result.written
            page.inplace_kept = result.kept
            page.inplace_review = result.review + result.not_written
            page.inplace_untranslated = int(getattr(result, "untranslated", 0) or 0)
            page.inplace_term_review = int(getattr(result, "term_review", 0) or 0)
            page.inplace_review_items = result.review_items
            if result.ok and result.written > 0:
                page.inplace_status = "done"
                page.inplace_pdf_path = result.output_pdf
                note = "" if result.ocr_used or not hooks.ocr_wanted or native_only else "（没有安装文字识别组件，画成线条的字未识别）"
                page.inplace_message = (
                    f"已替换 {result.written} 处，数字和编号等 {result.kept} 处不需要翻译，"
                    f"{page.inplace_review} 处认不准保留原文{note}"
                )
                if page.inplace_untranslated:
                    page.inplace_message += f"；{page.inplace_untranslated} 处未获得译文，需复核"
                if page.inplace_term_review:
                    page.inplace_message += f"；{page.inplace_term_review} 处未采用记忆库中的设定译法，需复核"
                self._log("OK", f"[{record.name}] 第 {page.page_number} 页：{page.inplace_message}")
            else:
                page.inplace_status = "kept_original"
                page.inplace_message = result.error or _inplace_nothing_written_message(
                    result, ocr_missing=not (result.ocr_used or not hooks.ocr_wanted or native_only)
                )
                self._log("WARN", f"[{record.name}] 第 {page.page_number} 页：{page.inplace_message}")

    def _finalize_image_record(
        self,
        record: PdfFileRecord,
        prepared: _PreparedPdfFile,
    ) -> None:
        source_page = next(
            (
                page
                for page in sorted(record.pages, key=lambda item: item.page_number)
                if page.translated_image_path and page.status == "success"
            ),
            None,
        )
        if source_page is None:
            record.status = PDF_OUTPUT_STATE_FAILED
            record.error = _no_usable_translated_pages_error(record)
            self._log("ERROR", f"[{record.name}] {record.error}")
            return
        source_path = Path(source_page.translated_image_path)
        previous_output = record.translated_image_path
        output_path = self._resolve_image_output_path(
            prepared,
            output_suffix=source_path.suffix or ".png",
        )
        try:
            # 和 PDF 装配同一条纪律：先写同目录的临时文件，写成功再原子替换。
            # 重新生成失败时，上一版译图必须原样留在磁盘上。
            temp_path = output_path.with_name(f".{output_path.name}.building")
            try:
                shutil.copy2(source_path, temp_path)
            except BaseException:
                try:
                    temp_path.unlink(missing_ok=True)
                except Exception as cleanup_exc:  # noqa: BLE001 - 不改变原始错误。
                    logger.debug(f"[PDF] 清理未完成的译图临时文件失败：{temp_path}：{cleanup_exc!r}")
                raise
            os.replace(temp_path, output_path)
            record.translated_image_path = str(output_path)
            record.translated_image_format = _image_format_from_path(output_path)
            record.high_quality_pdf_size_bytes = _safe_file_size(output_path)
            record.status = (
                PDF_OUTPUT_STATE_NEEDS_REVIEW
                if (record.review_failed_page_count or record.quality_flagged_page_count)
                else PDF_OUTPUT_STATE_COMPLETED
            )
            # 模型换了输出格式时新译图落在另一个后缀下（xxx_en.png → xxx_en.jpg），
            # 上一版不清就会以孤儿的身份留在用户的输出目录里，看上去像第二份译图。
            # 同样放在状态落定之后：重生成失败要回滚时磁盘上一个字节都没动过。
            self._discard_superseded_translated_image(record, previous_output, output_path)
            self._log("OK", f"[{record.name}] 已生成译图：{output_path.name}")
        except Exception as exc:  # noqa: BLE001 - file-level failure.
            record.status = PDF_OUTPUT_STATE_FAILED
            logger.debug(f"[PDF] 译图写入失败原始错误：{exc!r}")
            record.error = f"译图写入失败：{user_facing_reason(exc, fallback='译图无法写入输出目录。')}"
            self._log("ERROR", f"[{record.name}] {record.error}")

    def _resolve_image_output_path(
        self,
        prepared: _PreparedPdfFile,
        *,
        output_suffix: str,
    ) -> Path:
        """图片源的交付路径只解析一次，之后每一轮都原子覆盖同一个文件名。

        ``resolve_translated_image_path`` 是有副作用的：app 托管目录下它会先把已
        经存在的同名文件改名成 ``_R1`` 再返回基名。事务化重生成之后，那一步会成
        为整条链路上唯一一个「先动磁盘、再写新产物」的动作——重生成失败时回滚只
        还原记录字段、还不回文件名，用户就会拿到一个指向已消失文件的下载入口；
        重生成成功时被挪走的旧版又会以 ``_R1`` 的身份永久留在输出目录里，交付文
        件名还每重生成一次漂一级。所以这里跟 PDF 变体对齐：准备好的那份路径缓存
        在 ``prepared`` 上，重生成直接复用，只有模型换了输出格式时才换后缀。
        """
        cached = prepared.translated_image_path
        if cached is not None:
            if cached.suffix.lower() != output_suffix.lower():
                cached = cached.with_name(f"{cached.stem}{output_suffix}")
                prepared.translated_image_path = cached
            return cached
        resolved = resolve_translated_image_path(
            prepared.final_output_dir / Path(prepared.relative_pdf).parent,
            prepared.record.name,
            self._settings.pdf.target_lang,
            self._settings,
            app_managed=prepared.app_managed,
            output_suffix=output_suffix,
        )
        prepared.translated_image_path = resolved
        return resolved

    def _refresh_file_record_counts(self, record: PdfFileRecord) -> None:
        record.generated_page_count = sum(
            1
            for page in record.pages
            if page.status in {"success", "emergency_normalized", "placeholder"}
        )
        record.placeholder_page_count = sum(1 for page in record.pages if page.placeholder)
        record.emergency_ratio_normalized_count = sum(
            1 for page in record.pages if page.emergency_ratio_normalized
        )
        record.ratio_letterboxed_page_count = sum(
            1 for page in record.pages if page.ratio_letterboxed
        )
        record.quality_flagged_page_count = sum(
            1 for page in record.pages if page.quality_flags
        )
        record.render_downscaled_page_count = sum(
            1
            for page in record.pages
            if page.render_dpi and page.render_dpi < PDF_RENDER_DPI_DEFAULT
        )
        threshold = getattr(self._settings.pdf, "oversized_page_threshold", "A3")
        # 按设定原样保留的页（大幅面，或自动组合里选了原样保留的图纸页）。没有图像
        # 模型而保留的页、原位替换试过但没换成的页（大幅面页也一样）另有说法，不算
        # 在这里——那一页是试过了没换成，说成「因幅面过大被跳过」会让用户去关一个
        # 跟它无关的开关。
        record.skipped_oversize_page_count = sum(
            1 for page in record.pages
            if page.skipped_oversize and page.inplace_status != "done" and not page.kept_reason
            and not page.word_requested
            and (not page.inplace_requested
                 or (not page.inplace_status
                     and is_oversized_for_threshold(threshold, page.page_width_pt, page.page_height_pt)))
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
        # 「仍然采用」要认真正采用了的状态，不能只写 `not page.placeholder`：独立图片
        # 走的失败分支把 status 置成 failed 却把 placeholder 置成 False（没有占位图这回事），
        # 于是一张被丢掉、连输出都没有的图会被算进「有疑点仍采用」——任务中心的逐文件表
        # 写着「未生成」，同一屏的指标格却橙着一句「有疑点仍采用 1」。
        record.suspect_adopted_page_count = sum(
            1
            for page in record.pages
            if page.status in {"success", "emergency_normalized"}
            and (
                page.review_status == "failed"
                or page.quality_flags
                or page.emergency_ratio_normalized
            )
        )
        record.review_retry_count = sum(max(0, page.review_attempts - 1) for page in record.pages)
        record.review_minor_suggestion_count = sum(
            len(page.review_minor_suggestions) for page in record.pages
        )

    def _record_has_all_pages_finished(self, record: PdfFileRecord) -> bool:
        if record.status == PDF_OUTPUT_STATE_FAILED or record.page_count <= 0:
            return False
        finished_statuses = {
            "success",
            "emergency_normalized",
            "placeholder",
            "placeholder_pending",
            PDF_OUTPUT_STATE_FAILED,
            PDF_PAGE_STATUS_SKIPPED_OVERSIZE,
        }
        return (
            len(record.pages) == record.page_count
            and all(page.status in finished_statuses for page in record.pages)
        )

    @staticmethod
    def _settled_file_page_count(record: PdfFileRecord) -> int:
        finished_statuses = {
            "success",
            "emergency_normalized",
            "placeholder",
            PDF_OUTPUT_STATE_FAILED,
            PDF_PAGE_STATUS_SKIPPED_OVERSIZE,
        }
        return sum(page.status in finished_statuses for page in record.pages)

    @classmethod
    def _all_file_pages_settled_for_progress(cls, record: PdfFileRecord) -> bool:
        return (
            record.page_count > 0
            and len(record.pages) == record.page_count
            and cls._settled_file_page_count(record) == record.page_count
        )

    @staticmethod
    def _file_outstanding_future_count(
        prepared: _PreparedPdfFile,
        futures: dict[Any, tuple[_PreparedPdfFile, PdfPageRecord]],
    ) -> int:
        return sum(owner is prepared for owner, _page in futures.values())

    def _emit_file_page_progress(
        self,
        prepared: _PreparedPdfFile,
        *,
        outstanding: int = 0,
    ) -> None:
        record = prepared.record
        completed = self._settled_file_page_count(record)
        finished = self._all_file_pages_settled_for_progress(record)
        self._file_progress.emit(
            prepared.item.path,
            "waiting" if finished or outstanding == 0 else "active",
            "generate" if finished else "translate",
            completed=completed,
            total=record.page_count,
        )

    def _has_unfinished_pages(self, records: list[PdfFileRecord]) -> bool:
        return any(
            record.status != PDF_OUTPUT_STATE_FAILED
            and record.page_count > 0
            and not self._record_has_all_pages_finished(record)
            for record in records
        )

    def _discard_superseded_compressed_pdf(
        self,
        record: PdfFileRecord,
        prepared: _PreparedPdfFile,
    ) -> None:
        """本轮没有产出压缩版时，把上一轮留下的那份清掉。

        终态任务的单页重生成是事务化的：旧产物不再被预先删除，而是等新产物写成
        功后原子替换。代价是「本轮没被重写的旧产物」会原地留下——压缩版关掉、或
        者压缩装配失败时，磁盘上那份旧压缩 PDF 内容还停在重生成之前，却仍然挂在
        ``record.compressed_pdf_path`` 上被当成本任务的当前产物给用户下载。
        """
        stale_paths = {prepared.compressed_pdf_path}
        if record.compressed_pdf_path:
            stale_paths.add(Path(record.compressed_pdf_path))
        record.compressed_pdf_path = ""
        record.compressed_pdf_size_bytes = 0
        for path in stale_paths:
            try:
                if path.is_file():
                    path.unlink()
            except Exception as exc:  # noqa: BLE001 - 清理失败不该影响已交付的高清版。
                logger.debug(f"[PDF] 清除被取代的压缩 PDF 失败：{path}：{exc!r}")
                self._log(
                    "WARN",
                    f"[{record.name}] 清除上一版压缩 PDF 失败：{path.name}："
                    + user_facing_reason(exc, fallback="文件可能正被其他程序打开。"),
                )

    def _discard_superseded_translated_image(
        self,
        record: PdfFileRecord,
        previous_output: str,
        output_path: Path,
    ) -> None:
        """图片源换了输出格式时，把上一版另一个后缀的译图清掉。"""
        if not previous_output:
            return
        previous_path = Path(previous_output)
        try:
            if previous_path == output_path or not previous_path.is_file():
                return
            if previous_path.samefile(output_path):
                return
            previous_path.unlink()
        except FileNotFoundError:
            return
        except Exception as exc:  # noqa: BLE001 - 清理失败不该影响已写好的新译图。
            logger.debug(f"[PDF] 清除被取代的译图失败：{previous_path}：{exc!r}")
            self._log(
                "WARN",
                f"[{record.name}] 清除上一版译图失败：{previous_path.name}："
                + user_facing_reason(exc, fallback="文件可能正被其他程序打开。"),
            )

    def _clear_generated_pdf_outputs(self, prepared_files: list[_PreparedPdfFile]) -> None:
        for prepared in prepared_files:
            record = prepared.record
            paths = [prepared.translated_pdf_path, prepared.compressed_pdf_path]
            if record.translated_image_path:
                paths.append(Path(record.translated_image_path))
            for path in paths:
                try:
                    if path.exists():
                        path.unlink()
                except Exception as exc:  # noqa: BLE001 - cleanup should not block resume.
                    logger.debug(f"[PDF] 清除旧 PDF 产物失败原始错误：{path}：{exc!r}")
                    self._log(
                        "WARN",
                        f"[{record.name}] 清除旧的输出文件失败：{path.name}："
                        + user_facing_reason(
                            exc,
                            fallback="文件可能正被其他程序打开，重新生成的文件会另存一份。",
                        ),
                    )
            record.translated_pdf_path = ""
            record.compressed_pdf_path = ""
            record.translated_image_path = ""
            record.translated_image_format = ""
            record.high_quality_pdf_size_bytes = 0
            record.compressed_pdf_size_bytes = 0
            record.compression_error = ""
            if record.status in {
                PDF_OUTPUT_STATE_COMPLETED,
                PDF_OUTPUT_STATE_NEEDS_REVIEW,
                PDF_OUTPUT_STATE_STOPPED,
            }:
                record.status = "pending"

    def _render_source_page(
        self,
        doc,
        *,
        page_index: int,
        page_count: int,
        source_pages_dir: Path,
        file_name: str = "",
        source_pdf: str = "",
        source_path_key: str = "",
    ) -> PdfPageRecord:
        page = doc.get_page(page_index)
        try:
            page_width_pt, page_height_pt = page.get_size()
            route, fallback_reason = self._decide_page_route(
                page,
                page_index=page_index,
                width_pt=page_width_pt,
                height_pt=page_height_pt,
                source_path=source_path_key,
                source_pdf=source_pdf,
            )
            kept_reason = ""
            route_note = ""
            if fallback_reason:
                if route == ROUTE_SKIP:
                    kept_reason = f"{fallback_reason}，这一页保留原样"
                else:
                    route_note = f"{fallback_reason}，改用整页图像翻译"
            if route == ROUTE_IMAGE and self._image_model_missing:
                route = ROUTE_SKIP
                kept_reason = "没有设置图像翻译模型，这一页需要整页图像翻译，已保留原样"
            word_entry = self._word_pages.get((source_path_key, page_index + 1), {}) if route == ROUTE_WORD else {}
            if route in {ROUTE_SKIP, ROUTE_INPLACE, ROUTE_WORD}:
                # 不送图像模型的页（原样保留、原位替换）不渲染栅格化预览（A3 在
                # 300dpi 下是十来兆的位图，渲染纯属浪费），装配阶段会直接
                # 从源 PDF 把这一页矢量导入，栅格化只会拖慢流程、拉低质量。
                return PdfPageRecord(
                    page_number=page_index + 1,
                    source_image_path="",
                    file_name=file_name,
                    status=PDF_PAGE_STATUS_SKIPPED_OVERSIZE,
                    skipped_oversize=True,
                    inplace_requested=route == ROUTE_INPLACE,
                    word_requested=route == ROUTE_WORD,
                    word_segment=int(word_entry.get("segment", 0)),
                    word_status="done" if route == ROUTE_WORD else "",
                    word_message=str(word_entry.get("note", "")),
                    kept_reason=kept_reason,
                    page_width_pt=float(page_width_pt),
                    page_height_pt=float(page_height_pt),
                )
            scale, render_dpi = _pdf_render_scale_for_page(page_width_pt, page_height_pt)
            if render_dpi < PDF_RENDER_DPI_DEFAULT:
                self._log(
                    "WARN",
                    f"[{file_name}] 第 {page_index + 1} 页幅面过大（"
                    f"{_pt_to_mm(min(page_width_pt, page_height_pt)):.0f}×"
                    f"{_pt_to_mm(max(page_width_pt, page_height_pt)):.0f} mm），"
                    "已降低清晰度处理，以免内存不足。",
                )
                logger.info(
                    f"[PDF] [{file_name}] 第 {page_index + 1} 页渲染 DPI 从 {PDF_RENDER_DPI_DEFAULT} "
                    f"降到 {render_dpi:.0f}（单页像素上限 {PDF_PAGE_MAX_RENDER_PIXELS // 1_000_000} MP）"
                )
            bitmap = page.render(scale=scale, rev_byteorder=True)
            try:
                source_path = source_pages_dir / page_image_name(page_index + 1, page_count)
                source_path.parent.mkdir(parents=True, exist_ok=True)
                image = bitmap.to_pil()
                image.save(source_path, format="PNG")
                source_width_px, source_height_px = image.size
            finally:
                bitmap.close()
        finally:
            page.close()

        return PdfPageRecord(
            page_number=page_index + 1,
            source_image_path=str(source_path),
            file_name=file_name,
            status="rendered",
            source_width_px=int(source_width_px),
            source_height_px=int(source_height_px),
            page_width_pt=float(page_width_pt),
            page_height_pt=float(page_height_pt),
            render_dpi=float(render_dpi),
            route_note=route_note,
        )

    def _decide_page_route(
        self,
        page,
        *,
        page_index: int,
        width_pt: float,
        height_pt: float,
        source_path: str,
        source_pdf: str,
    ) -> tuple[str, str]:
        """(route, reason)：reason 非空表示首选用不了、改走了保底方式。"""
        pdf_settings = self._settings.pdf
        threshold = getattr(pdf_settings, "oversized_page_threshold", "A3")
        oversized = is_oversized_for_threshold(threshold, width_pt, height_pt)
        auto = getattr(pdf_settings, "translation_route", "image") == "auto"
        page_kind = self._page_kind(source_path, page_index)
        if not page_kind and auto:
            page_kind = KIND_TEXT if _page_has_text_layer(page, source_pdf, page_index) else KIND_SCAN
        route = page_route(
            pdf_settings,
            oversized_a3=is_oversized_page(width_pt, height_pt),
            oversized=oversized,
            page_kind=page_kind,
        )
        if not auto:
            return route, ""
        if page_kind == KIND_BLANK:
            entry = self._word_pages.get((source_path, page_index + 1))
            # 夹在转 Word 段里的空白页随段拼入；段没转成就照旧原样保留，不送图像模型。
            return (ROUTE_WORD if entry is not None and entry.get("status") == "done" else route), ""
        reason = self._route_unusable_reason(route, source_path, page_index + 1)
        if not reason:
            return route, ""
        return fallback_route(pdf_settings, oversized=oversized, image_available=not self._image_model_missing), reason

    def _route_unusable_reason(self, route: str, source_path: str, page_number: int) -> str:
        if route == ROUTE_IMAGE and self._image_model_missing:
            return "没有设置图像翻译模型"
        if route == ROUTE_INPLACE:
            if self._inplace_hooks is None:
                return "原位替换不可用"
            if not self._pdf_component_ready():
                return "未安装「PDF 处理组件」"
            if not str(self._settings.pdf.target_lang or "").lower().startswith("zh"):
                return "原位替换目前只支持译成中文"
        if route == ROUTE_WORD:
            entry = self._word_pages.get((source_path, page_number))
            if entry is None or entry.get("status") != "done":
                return (entry or {}).get("message") or "转成 Word 不可用"
        return ""

    def _pdf_component_ready(self) -> bool:
        """原位替换要用的 PyMuPDF（「PDF 处理组件」）能不能用。只查一次。"""
        if self._pdf_component_ok is None:
            from core.pdf_component import is_available

            self._pdf_component_ok = is_available(refresh=True)
            if not self._pdf_component_ok:
                self._log("WARN", "未安装「PDF 处理组件」：原位替换的页改用保底方式。可在「扩展」页安装。")
        return self._pdf_component_ok

    def _word_segments_possible(self) -> str:
        """可提取文字的页转 Word 要用的条件；全满足返回空串，否则返回原因。只查一次。"""
        if self._office_pdf_engine is None:
            from core.pdf_word_route import office_pdf_engine, word_route_available

            if not word_route_available():
                self._office_pdf_engine = ""
                self._office_pdf_message = "未安装「PDF 处理组件」"
            else:
                engine, message = office_pdf_engine()
                self._office_pdf_engine = engine
                self._office_pdf_message = "" if engine else "电脑上没有 Microsoft Word 或 LibreOffice，译好的正文转不回 PDF"
        return "" if self._office_pdf_engine else self._office_pdf_message

    def _render_source_image(
        self,
        source_image: Path,
        *,
        source_pages_dir: Path,
        file_name: str = "",
    ) -> PdfPageRecord:
        source_path = source_pages_dir / page_image_name(1, 1)
        source_path.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(source_image) as image:
            normalized = _to_white_rgb(ImageOps.exif_transpose(image))
            normalized.save(source_path, format="PNG")
            width, height = normalized.size
        return PdfPageRecord(
            page_number=1,
            source_image_path=str(source_path),
            file_name=file_name,
            status="rendered",
            source_width_px=int(width),
            source_height_px=int(height),
            page_width_pt=float(width),
            page_height_pt=float(height),
        )

    def _submit_document_context(self, executor: ThreadPoolExecutor, path: Path) -> Future[str] | None:
        """「自动识别场景」：读这份 PDF 的文字层，交给后台请文档翻译模型概括成一段文档说明。

        文字层在当前线程读（PDF 库不宜多线程同时用），只有模型请求放到后台。
        没有文字层就不请求，返回 None。
        """
        try:
            lines = _pdf_text_lines(path)
        except Exception as exc:  # noqa: BLE001 - the description is optional.
            logger.debug(f"[PDF] 读取文字层失败原始错误：{exc!r}")
            lines = []
        if not lines:
            self._log(
                "INFO",
                f"[{path.name}] 自动识别场景：这份 PDF 没有可读的文字层（扫描件或文字已转成线条），不生成文档说明",
            )
            return None
        requester = self._profile_requester

        def request() -> str:
            if requester is None or self._stop_event.is_set():
                return ""
            try:
                return str(requester(lines, self._log) or "")
            except Exception as exc:  # noqa: BLE001 - the description is optional.
                logger.debug(f"[PDF] 文档说明请求失败原始错误：{exc!r}")
                return ""

        return executor.submit(request)

    def _generate_page_attempts(
        self,
        page_record: PdfPageRecord,
        page_count: int,
        translated_pages_dir: Path,
        review_candidates_dir: Path,
        max_attempts: int,
        scheduler: WeightedApiScheduler,
        review_scheduler: WeightedApiScheduler,
        model_config,
        review_model_config,
        *,
        source_type: str = SOURCE_TYPE_PDF,
        document_context: str = "",
    ) -> PdfPageRecord:
        output_path = translated_pages_dir / page_image_name(page_record.page_number, page_count)
        preserve_model_format = source_type == SOURCE_TYPE_IMAGE
        last_error = ""
        last_quality: PageQualityResult | None = None
        last_image_bytes: bytes = b""
        last_review_failed = False
        review_feedback = ""
        target_language = get_target_lang_display(
            self._settings.pdf.target_lang,
            self._settings.custom_target_langs,
            include_optional=True,
        )
        review_enabled = bool(self._settings.pdf.review_enabled and review_model_config is not None)
        page_record.review_enabled = review_enabled
        # 质检容差必须跟着「这一页会被请求成什么尺寸」走，否则对只支持固定
        # 尺寸的模型（gpt-image-1）来说这道门谁都过不去，每页都跑满重试。
        ratio_tolerance = pdf_page_ratio_tolerance(
            source_width=page_record.source_width_px,
            source_height=page_record.source_height_px,
            model=str(getattr(model_config, "model", "") or ""),
        )
        if ratio_tolerance > PDF_ASPECT_RATIO_TOLERANCE + 1e-9:
            self._log(
                "INFO",
                f"{self._page_log_prefix(page_record)}当前模型仅支持固定尺寸输出，比例不符时将补充白边以保持原比例，"
                "不再重试。",
                visible=False,
            )
        attempt = 1
        while attempt <= max_attempts:
            try:
                lease = scheduler.acquire_lease(
                    1,
                    should_stop=self._stop_event.is_set,
                )
            except ApiSchedulerAcquireCancelled:
                break
            self._note_page_request_started(page_record)
            try:
                if self._stop_event.is_set():
                    break
                self._record_api_call()
                image_bytes = self._image_client.generate_page(
                    source_image_path=Path(page_record.source_image_path),
                    target_language=target_language,
                    target_lang_code=self._settings.pdf.target_lang,
                    model_config=model_config,
                    review_feedback=review_feedback or None,
                    # 只在有说明时才传，老的/第三方的生图客户端不必认识这个参数。
                    **({"document_context": document_context} if document_context else {}),
                )
            except Exception as exc:  # noqa: BLE001 - page/model classification.
                if isinstance(exc, ImageModelUnavailableError) or is_model_unavailable_error(exc):
                    # 这条消息会直接当成任务的终止提示显示出来，所以在抛出时就
                    # 换成人话；原文留给 debug 日志。
                    logger.debug(f"[PDF] 图像模型不可用原始错误：{exc!r}")
                    raise ImageModelUnavailableError(
                        user_facing_reason(
                            exc,
                            fallback=(
                                "PDF 与图片翻译模型当前不可用，请在设置中检查此连接的 API 密钥、"
                                "模型名称和账户额度。"
                            ),
                        )
                    ) from exc
                decision = handle_api_concurrency_limit(
                    exc,
                    scheduler=scheduler,
                    request_generation=lease.generation,
                    context_label=f"PDF 第 {page_record.page_number} 页",
                    error_callback=lambda message: self._record_rate_limit_reduction(message),
                    # 限流退避里睡的是整整 30 秒。不把停止标志交给它，用户点了停止
                    # 之后每一页都要先把这一觉睡完才肯回来，停止响应被拖成几十秒。
                    should_stop=self._stop_event.is_set,
                )
                if decision is not None and decision.should_retry:
                    time.sleep(0.2)
                    continue
                # 原始异常文本（含接口地址、JSON 错误体）只进 debug 日志；
                # 用户看到的每一处都用一句中文说明。
                logger.debug(
                    f"[PDF] 第 {page_record.page_number} 页第 {attempt} 次生成失败原始错误：{exc!r}"
                )
                last_error = user_facing_reason(
                    exc,
                    fallback="PDF 与图片翻译模型本次未返回可用结果，请稍后重试，或在设置中更换连接。",
                )
                self._record_page_retrying(page_record)
                self._log(
                    "WARN",
                    f"{self._page_log_prefix(page_record)}第 {attempt}/{max_attempts} 次生成失败：{last_error}",
                )
                page_record.attempts = attempt
                attempt += 1
                continue
            finally:
                scheduler.release(lease)

            quality = check_page_quality(
                image_bytes,
                source_width=page_record.source_width_px,
                source_height=page_record.source_height_px,
                ratio_tolerance=ratio_tolerance,
                source_image_path=page_record.source_image_path or None,
            )
            page_record.attempts = attempt
            last_quality = quality
            self._apply_quality_flags(page_record, quality)
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
                    review_request_error: Exception | None = None
                    try:
                        while True:
                            review_generation: int | None = None
                            try:
                                with review_scheduler.slot(
                                    1,
                                    category=API_REQUEST_CATEGORY_RECOVERY,
                                    should_stop=self._stop_event.is_set,
                                ) as review_lease:
                                    review_generation = review_lease.generation
                                    self._record_review_api_call()
                                    review_result = self._review_client.review_page(
                                        source_image_path=Path(page_record.source_image_path),
                                        translated_image_path=candidate_path,
                                        target_language=target_language,
                                        model_config=review_model_config,
                                    )
                                break
                            except ApiSchedulerAcquireCancelled:
                                raise
                            except Exception as review_exc:
                                if isinstance(review_exc, PdfReviewModelUnavailableError) or is_model_unavailable_error(review_exc):
                                    raise
                                decision = handle_api_concurrency_limit(
                                    review_exc,
                                    scheduler=review_scheduler,
                                    request_generation=review_generation,
                                    context_label=f"PDF 第 {page_record.page_number} 页审核",
                                    error_callback=lambda message: self._record_rate_limit_reduction(message),
                                    should_stop=self._stop_event.is_set,
                                )
                                if decision is None or not decision.should_retry:
                                    raise
                    except ApiSchedulerAcquireCancelled:
                        self._finish_page_review()
                        break
                    except Exception as exc:  # noqa: BLE001 - review participates in page recovery.
                        self._finish_page_review()
                        if isinstance(exc, PdfReviewModelUnavailableError) or is_model_unavailable_error(exc):
                            logger.debug(f"[PDF] 审核模型不可用原始错误：{exc!r}")
                            raise PdfReviewModelUnavailableError(
                                user_facing_reason(
                                    exc,
                                    fallback=(
                                        "PDF 与图片审核模型当前不可用，请在设置中检查此连接的 API 密钥、"
                                        "模型名称和账户额度。"
                                    ),
                                )
                            ) from exc
                        review_request_error = exc
                        logger.debug(f"[PDF] 审核请求失败原始错误：{exc!r}")
                        review_failure_reason = user_facing_reason(
                            exc,
                            fallback="审核模型本次未返回可用结果。",
                        )
                        review_result = PdfPageReviewResult(
                            passed=False,
                            blocking_issues=[
                                PdfReviewIssue(
                                    type="review_request_failed",
                                    problem=review_failure_reason,
                                    suggestion="请重试审核或更换 PDF 与图片审核模型。",
                                )
                            ],
                            summary=f"审核请求失败：{review_failure_reason}",
                        )
                    else:
                        self._finish_page_review()

                    review_path = candidate_path.with_name(f"attempt_{attempt:02d}_review.json")
                    _write_review_json(review_path, review_result)
                    self._mark_pdf_review_model_success()
                    candidate_artifact["review_path"] = str(review_path)
                    if review_request_error is not None:
                        candidate_artifact["review_status"] = "error"
                        candidate_artifact["summary"] = review_result.summary
                        page_record.review_status = "failed"
                        page_record.review_issues = [
                            issue.__dict__ for issue in review_result.blocking_issues
                        ]
                        page_record.error = review_result.summary
                        # 审核异常保留候选图时也要按真实格式定扩展名：这条分支此前
                        # 一直用循环开头那个写死的 .png，模型返回 JPEG/WebP 时就会
                        # 写出一张「名叫 png 的 jpg」，装订和预览都要另猜格式。
                        output_path = _translated_page_output_path(
                            translated_pages_dir,
                            page_record=page_record,
                            page_count=page_count,
                            image_bytes=image_bytes,
                            preserve_model_format=preserve_model_format,
                        )
                        width, height = self._write_accepted_page_image(
                            image_bytes,
                            output_path,
                            page_record=page_record,
                            quality=quality,
                            preserve_model_format=preserve_model_format,
                        )
                        page_record.status = "success"
                        page_record.translated_image_path = str(output_path)
                        page_record.output_width_px = width
                        page_record.output_height_px = height
                        page_record.final_candidate_attempt = attempt
                        self._record_page_review_failed()
                        self._mark_image_model_success()
                        self._record_page_recovered(page_record)
                        self._log(
                            "WARN",
                            f"{self._page_log_prefix(page_record)}审核步骤出错，已保留本次译图并标记为需人工复核。",
                        )
                        return page_record
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
                        output_path = _translated_page_output_path(
                            translated_pages_dir,
                            page_record=page_record,
                            page_count=page_count,
                            image_bytes=image_bytes,
                            preserve_model_format=preserve_model_format,
                        )
                        width, height = self._write_accepted_page_image(
                            image_bytes,
                            output_path,
                            page_record=page_record,
                            quality=quality,
                            preserve_model_format=preserve_model_format,
                        )
                        self._record_page_review_passed()
                        page_record.status = "success"
                        page_record.review_status = "passed"
                        # 上一轮没通过时留下的 blocking issue 必须清掉：面板的
                        # review_summary 优先读它，不清就会出现「通过」和上一轮那句
                        # 「编号标签误译」并排显示，同一行自相矛盾。
                        page_record.review_issues = []
                        page_record.translated_image_path = str(output_path)
                        page_record.output_width_px = width
                        page_record.output_height_px = height
                        page_record.final_candidate_attempt = attempt
                        self._mark_image_model_success()
                        self._log(
                            "OK",
                            self._page_success_log(
                                page_record,
                                attempt,
                                max_attempts,
                                review_checked=True,
                            ),
                        )
                        self._record_page_recovered(page_record)
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
                        f"{self._page_log_prefix(page_record)}第 {attempt}/{max_attempts} 轮审核未通过：{last_error}",
                    )
                    self._record_page_retrying(page_record)
                    attempt += 1
                    continue

                output_path = _translated_page_output_path(
                    translated_pages_dir,
                    page_record=page_record,
                    page_count=page_count,
                    image_bytes=image_bytes,
                    preserve_model_format=preserve_model_format,
                )
                width, height = self._write_accepted_page_image(
                    image_bytes,
                    output_path,
                    page_record=page_record,
                    quality=quality,
                    preserve_model_format=preserve_model_format,
                )
                self._mark_image_model_success()
                page_record.status = "success"
                page_record.translated_image_path = str(output_path)
                page_record.output_width_px = width
                page_record.output_height_px = height
                self._log(
                    "OK",
                    self._page_success_log(
                        page_record,
                        attempt,
                        max_attempts,
                        review_checked=False,
                    ),
                )
                self._record_page_recovered(page_record)
                return page_record

            last_review_failed = False
            last_error = quality.message
            self._record_page_retrying(page_record)
            self._log(
                "WARN",
                f"{self._page_log_prefix(page_record)}第 {attempt}/{max_attempts} 次自动检查未通过：{quality.message}",
            )
            attempt += 1

        if (
            source_type != SOURCE_TYPE_IMAGE
            and
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
            self._record_page_recovered(page_record)
            self._log("WARN", f"{self._page_log_prefix(page_record)}已自动修正页面比例{self._page_elapsed_text(page_record)}")
            return page_record

        if self._stop_event.is_set():
            # 中止时这一页没跑出任何可用结果：它可能一次模型调用都没发生（在队列里
            # 等着，闸门直接取消），也可能跑到一半被截断。无论哪种，它都不是「试过
            # 了没成」，不该被烧成失败占位页塞进交付产物、更不该让整份文件被判成
            # 「全部页面已完成」。退回「未开始」，交给主循环撤掉这条页记录：报告如实
            # 报截断，续译时这一页还能重新排队。
            page_record.status = PDF_PAGE_STATUS_STOPPED_UNSTARTED
            page_record.placeholder = False
            page_record.translated_image_path = ""
            page_record.error = last_error or "任务已停止，此页尚未生成结果。"
            return page_record

        if source_type == SOURCE_TYPE_IMAGE:
            page_record.status = PDF_OUTPUT_STATE_FAILED
            page_record.placeholder = False
            if last_review_failed:
                page_record.review_status = "failed"
                self._record_page_review_failed()
            page_record.translated_image_path = ""
            page_record.error = last_error or "此页未能生成译图"
            self._record_page_failed(page_record)
            self._log_page_not_generated(page_record)
            return page_record

        page_record.status = "placeholder_pending"
        page_record.placeholder = True
        if last_review_failed:
            page_record.review_status = "failed"
            self._record_page_review_failed()
        page_record.translated_image_path = str(
            translated_pages_dir / page_image_name(page_record.page_number, page_count, failed=True)
        )
        page_record.error = last_error or "此页未能生成译图"
        self._record_page_placeholder(page_record)
        self._log_page_not_generated(page_record)
        return page_record

    def _log_page_not_generated(self, page_record: PdfPageRecord) -> None:
        elapsed = self._page_elapsed_text(page_record)
        if elapsed:
            self._log("WARN", f"{self._page_log_prefix(page_record)}未能生成可用译图{elapsed}")

    def _generate_page_with_retries(self, page_record: PdfPageRecord, *args: Any, **kwargs: Any) -> PdfPageRecord:
        """Generate one page (``_generate_page_attempts``) and close its request bookkeeping."""
        try:
            return self._generate_page_attempts(page_record, *args, **kwargs)
        finally:
            with self._page_status_lock:
                started = self._page_started.pop(self._page_status_key(page_record), None)
            if started is not None:
                self._page_requests.on_event(
                    BatchRequestEvent(BATCH_EVENT_DONE, index=started[0], items=1, elapsed=time.monotonic() - started[1])
                )

    def _note_page_request_started(self, page_record: PdfPageRecord) -> None:
        """The page got its first model slot: start its clock (later attempts keep it)."""
        key = self._page_status_key(page_record)
        with self._page_status_lock:
            if key in self._page_started:
                return
            self._page_request_seq += 1
            index = self._page_request_seq
            self._page_started[key] = (index, time.monotonic())
        self._page_requests.on_event(BatchRequestEvent(BATCH_EVENT_SENT, index=index, items=1))

    def _page_elapsed_text(self, page_record: PdfPageRecord) -> str:
        """「，用时 N 秒」since the page's first request; empty before any request."""
        with self._page_status_lock:
            started = self._page_started.get(self._page_status_key(page_record))
        if started is None:
            return ""
        return f"，用时 {time.monotonic() - started[1]:.1f} 秒"

    def _apply_quality_flags(
        self,
        page_record: PdfPageRecord,
        quality: PageQualityResult,
    ) -> None:
        """Carry the content-QC verdict of the latest attempt onto the page."""
        page_record.quality_flags = list(quality.review_reasons)
        page_record.quality_message = quality.review_message
        if quality.review_reasons:
            self._log(
                "WARN",
                f"{self._page_log_prefix(page_record)}内容质检提示，已保留本页并标记人工复核："
                f"{quality.review_message}",
            )

    def _write_accepted_page_image(
        self,
        image_bytes: bytes,
        output_path: Path,
        *,
        page_record: PdfPageRecord,
        quality: PageQualityResult,
        preserve_model_format: bool,
    ) -> tuple[int, int]:
        """Write one accepted page, letterboxing it when the model's own size
        policy makes an exact aspect match impossible.

        Assembly scales the page image onto the PDF page rectangle, so an output
        whose shape differs from the source would be stretched.  Up to
        ``PDF_RATIO_LETTERBOX_THRESHOLD`` that stretch is invisible; beyond it
        (the fixed-size models are off by 5%~14%) the page is padded to the
        source shape instead.  This is a known, deterministic cost of that
        model — it does not consume a retry and does not mark the file for review.
        """
        source_ratio = _safe_ratio(page_record.source_width_px, page_record.source_height_px)
        output_ratio = _safe_ratio(quality.width, quality.height)
        needs_letterbox = (
            not preserve_model_format
            and source_ratio > 0
            and output_ratio > 0
            and abs(source_ratio - output_ratio) / source_ratio > PDF_RATIO_LETTERBOX_THRESHOLD
        )
        if not needs_letterbox:
            _write_translated_page_image(
                image_bytes,
                output_path,
                preserve_model_format=preserve_model_format,
            )
            return quality.width, quality.height

        width, height = normalize_image_to_source_ratio(
            image_bytes,
            source_width=page_record.source_width_px,
            source_height=page_record.source_height_px,
            output_path=output_path,
        )
        page_record.ratio_letterboxed = True
        self._log(
            "INFO",
            f"{self._page_log_prefix(page_record)}模型仅支持固定尺寸输出，已为此页补充白边以保持原比例。",
            visible=False,
        )
        return width, height

    def _page_status_key(self, page_record: PdfPageRecord) -> str:
        return str(page_record.source_image_path or f"{page_record.file_name}:{page_record.page_number}")

    def _page_log_prefix(self, page_record: PdfPageRecord) -> str:
        if page_record.file_name:
            return f"[{page_record.file_name}] 第 {page_record.page_number} 页 "
        return f"第 {page_record.page_number} 页 "

    def _page_success_log(
        self,
        page_record: PdfPageRecord,
        attempt: int,
        max_attempts: int,
        *,
        review_checked: bool,
    ) -> str:
        suffix = ("生成成功，审核通过" if review_checked else "生成成功") + self._page_elapsed_text(page_record)
        if attempt <= 1:
            return f"{self._page_log_prefix(page_record)}{suffix}"
        return f"{self._page_log_prefix(page_record)}第 {attempt}/{max_attempts} 次{suffix}"

    def _record_page_retrying(self, page_record: PdfPageRecord) -> None:
        key = self._page_status_key(page_record)
        with self._page_status_lock:
            self._retrying_pages.add(key)
            self._retried_pages.add(key)
            self._emit_page_status_locked()

    def _record_page_recovered(self, page_record: PdfPageRecord) -> None:
        key = self._page_status_key(page_record)
        with self._page_status_lock:
            if key in self._retried_pages:
                self._recovered_pages.add(key)
            self._retrying_pages.discard(key)
            self._emit_page_status_locked()

    def _record_page_placeholder(self, page_record: PdfPageRecord) -> None:
        key = self._page_status_key(page_record)
        with self._page_status_lock:
            self._retrying_pages.discard(key)
            self._placeholder_pages.add(key)
            self._emit_page_status_locked()

    def _record_page_failed(self, page_record: PdfPageRecord) -> None:
        key = self._page_status_key(page_record)
        with self._page_status_lock:
            self._retrying_pages.discard(key)
            self._emit_page_status_locked()

    def _record_page_completed(self, page_record: PdfPageRecord) -> None:
        with self._page_status_lock:
            self._completed_page_count += 1
            self._emit_page_status_locked()

    def _record_page_submitted(self) -> None:
        with self._page_status_lock:
            self._submitted_page_count += 1
            self._emit_page_status_locked()

    def _emit_page_status(self) -> None:
        with self._page_status_lock:
            self._emit_page_status_locked()

    def _emit_page_status_locked(self) -> None:
        pending_submitted = max(0, self._submitted_page_count - self._completed_page_count)
        self._queue.put(
            PdfPageRecoveryStatusMsg(
                total_pages=self._total_page_count,
                completed_pages=self._completed_page_count,
                submitted_page_count=self._submitted_page_count,
                pending_submitted_page_count=pending_submitted,
                retrying_page_count=len(self._retrying_pages),
                retried_page_count=len(self._retried_pages),
                recovered_page_count=len(self._recovered_pages),
                placeholder_page_count=len(self._placeholder_pages),
                skipped_oversize_page_count=sum(
                    1
                    for prepared in self._prepared_files
                    for page in prepared.record.pages
                    if page.skipped_oversize
                ),
            )
        )

    def _stop_wait_status(self) -> str:
        """中止后的等待状态行。

        这句话此前只在「检测到中止」和「又有页跑完了」两个时刻发出，而中止之后往往
        恰恰没有页再跑完——最后那几页正卡在上游的读超时里（实测 4 分 10 秒）。屏幕上
        于是四分钟一个字不动，看着像死掉了。现在它每隔几秒重发一次，并且明说在等什么。
        """
        pending_submitted = max(0, self._submitted_page_count - self._completed_page_count)
        if not pending_submitted:
            return "状态：正在停止任务：已提交的页面均已结束，正在完成最后处理。"
        waited = self._stop_wait_elapsed_label()
        return (
            f"状态：正在停止任务：已提交 {self._submitted_page_count} 页，正在等待 {pending_submitted} 页完成{waited}。"
            "这些页面需等待模型服务响应或超时，可能需要几分钟；"
            "已生成的页面和素材将予以保留。"
        )

    def _stop_wait_elapsed_label(self) -> str:
        if self._stop_wait_started_at is None:
            return ""
        seconds = int(max(0.0, time.monotonic() - self._stop_wait_started_at))
        if seconds < 60:
            return f"（已等待 {seconds} 秒）"
        return f"（已等待 {seconds // 60} 分 {seconds % 60} 秒）"

    def _emit_stop_wait_status(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if self._stop_wait_started_at is None:
            self._stop_wait_started_at = now
        if not force and now - self._stop_wait_last_emit < STOP_WAIT_STATUS_REFRESH_SECONDS:
            return
        self._stop_wait_last_emit = now
        self._queue.put(StatusMsg(self._stop_wait_status()))

    def _mark_image_model_success(self) -> None:
        if self._settings.image_model_role.availability_status == "available":
            return
        record_image_model_availability(
            self._settings,
            ok=True,
            message="PDF 与图片翻译模型调用成功。",
            checked_at=datetime.now().isoformat(timespec="seconds"),
        )

    def _mark_pdf_review_model_success(self) -> None:
        if self._settings.pdf_review_model_role.availability_status == "available":
            return
        record_pdf_review_model_availability(
            self._settings,
            ok=True,
            message="PDF 与图片审核模型调用成功。",
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

    def _record_api_call(self) -> None:
        """页面工作线程记一次图像生成调用（并发自增必须上锁）。"""
        with self._counter_lock:
            self._api_call_count += 1

    def _record_review_api_call(self) -> None:
        """页面工作线程记一次审核调用（并发自增必须上锁）。"""
        with self._counter_lock:
            self._review_api_call_count += 1

    def _record_rate_limit_reduction(self, message: str) -> None:
        with self._counter_lock:
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
                source_image_path=page.source_image_path or "（无法读取此页，无原页图像）",
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
        pdfium = _load_pdfium()
        output_pdf.parent.mkdir(parents=True, exist_ok=True)
        # 先写同目录的临时文件，整份写成功了再原子替换旧产物。装配中途失败（页图
        # 缺失、磁盘写满、模型不可用导致的重生成半途而废）时，上一次已经交付的
        # 高清/压缩 PDF 必须原样留在磁盘上：单页重新生成不该有机会把整份 PDF 弄丢，
        # 也不该在原地留下一个写了一半的坏文件。
        temp_pdf = output_pdf.with_name(f".{output_pdf.name}.building")
        out_doc = pdfium.PdfDocument.new()
        # 大幅面页矢量直传要从「复制到输出目录的源 PDF」按需打开一次，而不是
        # 每页都开一次；大多数文件根本没有大幅面页，绝大多数情况下这个文档
        # 永远不会被打开。
        source_doc = None
        try:
            with tempfile.TemporaryDirectory(prefix="xl-translator-pdf-") as tmp_dir:
                temp_dir = Path(tmp_dir)
                done_segments = {int(item["segment"]): item for item in record.word_segments
                                 if item.get("status") == "done" and item.get("pdf_path")
                                 and Path(item["pdf_path"]).exists()}
                imported_segments: set[int] = set()
                for page in sorted(record.pages, key=lambda item: item.page_number):
                    if page.word_status == "done" and page.word_segment in done_segments:
                        # 转 Word 翻译的正文段：整段转回的 PDF 在段首一次拼进来，页数可能和
                        # 原文不同（中文通常更短）；段内其余页不再单独处理。
                        if page.word_segment not in imported_segments:
                            imported_segments.add(page.word_segment)
                            seg_doc = _open_pdf_document(Path(done_segments[page.word_segment]["pdf_path"]))
                            try:
                                out_doc.import_pages(seg_doc)
                            finally:
                                seg_doc.close()
                        continue
                    if page.skipped_oversize:
                        # 装订顺序靠的是这个循环本身按页码顺序、逐页 append——
                        # 无论这一页是矢量导入还是栅格化图片页，只要在这里按
                        # 顺序处理，输出页序就和原文档一致，不需要额外记录
                        # 插入位置。压缩版也走这条分支：大幅面页不能被有损压缩。
                        if page.inplace_status == "done" and page.inplace_pdf_path and Path(page.inplace_pdf_path).exists():
                            # 原位替换后的单页：矢量、图片都在，压缩版同样不做有损处理。
                            inplace_doc = _open_pdf_document(Path(page.inplace_pdf_path))
                            try:
                                out_doc.import_pages(inplace_doc, pages=[0])
                            finally:
                                inplace_doc.close()
                            continue
                        if source_doc is None:
                            source_doc = _open_pdf_document(Path(record.source_copy_path))
                        out_doc.import_pages(source_doc, pages=[page.page_number - 1])
                        continue
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
                                f"第 {page.page_number} 页压缩失败，这一页改用高清版："
                                f"{user_facing_reason(exc, fallback='这一页压缩时出错。')}",
                            )
                            image_path = Path(page.translated_image_path)
                    _append_pdf_image_page(
                        pdfium,
                        out_doc,
                        image_path=image_path,
                        page_width_pt=page.page_width_pt,
                        page_height_pt=page.page_height_pt,
                    )
                out_doc.save(temp_pdf)
        except BaseException:
            # 半成品不能留在输出目录里：它既不是产物，也会被下一次装配当成旧文件。
            try:
                temp_pdf.unlink(missing_ok=True)
            except Exception as exc:  # noqa: BLE001 - 清理失败不改变原始错误。
                logger.debug(f"[PDF] 清理未完成的 PDF 临时文件失败：{temp_pdf}：{exc!r}")
            raise
        finally:
            if source_doc is not None:
                source_doc.close()
            out_doc.close()
        os.replace(temp_pdf, output_pdf)

    def _resolve_pdf_concurrency(self, auto_concurrency: int | None = None) -> int:
        """Runtime page concurrency: the explicit setting wins, blank means default.

        「同时生成页数」and `--pdf-page-concurrency` both write
        `pdf.page_generation_concurrency`.  When it is set, it is the user
        saying a number out loud and it has to be the number we run; a blank
        value takes the default from core.surface_rate (cloud 10, local 3).
        """
        raw = self._settings.pdf.page_generation_concurrency
        if raw is None:
            fallback = (
                auto_concurrency
                if auto_concurrency is not None
                else PDF_PAGE_CONCURRENCY_DEFAULT
            )
            try:
                value = int(fallback)
            except (TypeError, ValueError):
                value = PDF_PAGE_CONCURRENCY_DEFAULT
            maximum = (
                2**31 - 1
                if self._settings.engine.concurrency_unlocked
                else PDF_PAGE_CONCURRENCY_SAFETY_CAP
            )
            return max(1, min(maximum, value))
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = PDF_PAGE_CONCURRENCY_DEFAULT
        maximum = (
            2**31 - 1
            if self._settings.engine.concurrency_unlocked
            else PDF_PAGE_CONCURRENCY_SAFETY_CAP
        )
        return max(1, min(maximum, value))

    def _build_summary(
        self,
        *,
        output_dir: Path,
        started: datetime,
        file_records: list[PdfFileRecord],
        stopped: bool,
        fatal_error: str,
        elapsed_sec: float | None = None,
        stop_reason: str | None = None,
    ) -> PdfTaskSummary:
        # ``elapsed_sec`` / ``stop_reason`` are overridden only when a finished
        # task regenerates one page: the gap between the run ending and that
        # click is idle time, and the original stop reason still describes how
        # the run itself ended.
        completed = datetime.now()
        status = determine_pdf_task_status(
            stopped=stopped,
            file_records=file_records,
            fatal_error=fatal_error,
        )
        total_pages = sum(record.page_count for record in file_records)
        generated_pdfs = sum(1 for record in file_records if record.translated_pdf_path)
        generated_images = sum(1 for record in file_records if record.translated_image_path)
        generated_docx = sum(1 for record in file_records if record.translated_docx_path)
        compressed_pdfs = sum(1 for record in file_records if record.compressed_pdf_path)
        placeholder_count = sum(record.placeholder_page_count for record in file_records)
        emergency_count = sum(record.emergency_ratio_normalized_count for record in file_records)
        retry_count = sum(record.retry_count for record in file_records)
        skipped_oversize_count = sum(
            record.skipped_oversize_page_count for record in file_records
        )
        generated_page_count = sum(_real_generated_page_count(record) for record in file_records)
        unstarted_page_count = sum(_unstarted_page_count(record) for record in file_records)
        review_enabled = bool(self._settings.pdf.review_enabled)
        reviewed_page_count = sum(record.reviewed_page_count for record in file_records)
        review_passed_page_count = sum(record.review_passed_page_count for record in file_records)
        review_repaired_page_count = sum(record.review_repaired_page_count for record in file_records)
        review_failed_page_count = sum(record.review_failed_page_count for record in file_records)
        suspect_adopted_page_count = sum(
            record.suspect_adopted_page_count for record in file_records
        )
        review_retry_count = sum(record.review_retry_count for record in file_records)
        review_minor_suggestion_count = sum(
            record.review_minor_suggestion_count for record in file_records
        )
        partial_artifacts = any(record.pages or record.source_copy_path for record in file_records)
        return PdfTaskSummary(
            status=status,
            output_dir=str(output_dir),
            target_lang=self._settings.pdf.target_lang,
            target_lang_label=get_target_lang_display(
                self._settings.pdf.target_lang,
                self._settings.custom_target_langs,
                include_optional=True,
            ),
            started_at=started.isoformat(timespec="seconds"),
            completed_at=completed.isoformat(timespec="seconds"),
            elapsed_sec=(
                (completed - started).total_seconds()
                if elapsed_sec is None
                else float(elapsed_sec)
            ),
            file_count=len(file_records),
            total_page_count=total_pages,
            generated_pdf_count=generated_pdfs,
            generated_image_count=generated_images,
            generated_docx_count=generated_docx,
            placeholder_page_count=placeholder_count,
            emergency_ratio_normalized_count=emergency_count,
            retry_count=retry_count,
            skipped_oversize_page_count=skipped_oversize_count,
            generated_page_count=generated_page_count,
            unstarted_page_count=unstarted_page_count,
            compressed_pdf_enabled=bool(self._settings.pdf.generate_compressed_pdf),
            compressed_pdf_count=compressed_pdfs,
            compression_quality=PDF_COMPRESSED_JPEG_QUALITY_DEFAULT,
            compression_max_long_edge_px=PDF_COMPRESSED_MAX_LONG_EDGE_PX,
            review_enabled=review_enabled,
            reviewed_page_count=reviewed_page_count,
            review_passed_page_count=review_passed_page_count,
            review_repaired_page_count=review_repaired_page_count,
            review_failed_page_count=review_failed_page_count,
            suspect_adopted_page_count=suspect_adopted_page_count,
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
            stop_reason=(
                (self._stop_reason if stopped else "")
                if stop_reason is None
                else str(stop_reason)
            ),
            files=file_records,
        )


def _build_pdf_file_item(path: Path, *, root: Path | None = None) -> PdfFileItem:
    page_count = _read_pdf_page_count(path)
    if page_count <= 0:
        raise ValueError("PDF 无可读页面或文件已损坏。")
    return PdfFileItem(
        path=path,
        name=path.stem,
        size_kb=round(path.stat().st_size / 1024, 1),
        page_count=page_count,
        source_type=SOURCE_TYPE_PDF,
        relative_path=_relative_scan_path(path, root or path.parent),
        format="pdf",
        oversized_page_count=_read_pdf_oversized_page_count(path),
    )


def _build_image_file_item(path: Path, *, root: Path | None = None) -> PdfFileItem:
    width, height = _validate_source_image(path)
    return PdfFileItem(
        path=path,
        name=path.stem,
        size_kb=round(path.stat().st_size / 1024, 1),
        page_count=1,
        source_type=SOURCE_TYPE_IMAGE,
        relative_path=_relative_scan_path(path, root or path.parent),
        format=path.suffix.lower().lstrip("."),
        width_px=width,
        height_px=height,
    )


def _read_pdf_page_count(path: Path) -> int:
    try:
        doc = _open_pdf_document(path)
    except Exception:
        return 0
    try:
        return len(doc)
    finally:
        doc.close()


def _read_pdf_oversized_page_count(path: Path) -> int | None:
    """扫描阶段统计一份 PDF 里有多少页会被「跳过大幅面页」命中。

    只读每页的 /MediaBox 尺寸（page.get_size()），不渲染、不解码内容流，
    量级和 _read_pdf_page_count 一样便宜，可以在扫描时对每个文件都跑一遍。

    任何异常（文件整体打不开、某一页读不出尺寸）都直接返回 None，绝不能把
    「读了一半」的部分计数当成确定值返回——调用方要靠 None 和
    PdfFileItem.oversized_page_count 一起维持「未知 vs 零」的三态语义，
    半成品计数一旦被当成 0，就会在界面上显示成「确认没有大幅面页」。
    """
    try:
        doc = _open_pdf_document(path)
    except Exception:
        return None
    try:
        count = 0
        for index in range(len(doc)):
            page = doc.get_page(index)
            try:
                width_pt, height_pt = page.get_size()
            finally:
                page.close()
            if is_oversized_page(width_pt, height_pt):
                count += 1
        return count
    except Exception:
        return None
    finally:
        doc.close()


def _validate_source_image(path: Path) -> tuple[int, int]:
    with Image.open(path) as image:
        if bool(getattr(image, "is_animated", False)) or int(getattr(image, "n_frames", 1) or 1) > 1:
            raise ValueError("暂不支持动图或多页图片。")
        normalized = ImageOps.exif_transpose(image)
        normalized.load()
        width, height = normalized.size
    if width <= 0 or height <= 0:
        raise ValueError("图片尺寸无效。")
    return int(width), int(height)


# 内置目标语言的显示名白名单，只用来识别「后缀式」译文命名（见
# _is_new_style_translated_name）。include_optional=True 保留了未来
# 启用可选语言的扩展口子；不传 custom_target_langs 是因为扫描目录时
# 走不到 AppSettings——用户自定义语言名不在这个白名单里，是已知的
# 口子，见 _is_new_style_translated_name 的说明。
_BUILTIN_TARGET_LANG_LABELS = frozenset(get_supported_languages(None, include_optional=True))

# 后缀式译文名末尾可能叠着修订号 `_R2` 和高清/压缩变体 `_高清`/`_压缩`，
# 识别时要先把这两层剥掉，才能拿到真正的语言标签去对白名单。
_REVISION_SUFFIX_RE = re.compile(r"_R\d+$")
_VARIANT_SUFFIX_RE = re.compile(r"_(?:高清|压缩)$")


def _is_new_style_translated_name(name: str) -> bool:
    """识别「原文名_语言标签[_高清/_压缩][_R修订号].ext」这种后缀式译文命名。

    V9.3.0 之前产出的译文用前缀式「译文(语言)_原文名」，`_should_skip_scanned_input`
    靠 startswith("译文(") 就能认出来；换成后缀式之后没有这么显眼的标记了，只能靠
    「最后一段是不是我们认识的语言名」来判断。这里刻意要求最后一段必须命中内置语言
    白名单，而不是「文件名里有下划线就算」——否则任何 `随便一个词_另一个词.pdf`
    都会被当成自己的产物跳过扫描，把用户真正想翻译的原文件挡在外面。
    即便加了白名单限制，风险也没有完全消除：如果用户自己的原文件恰好叫
    `xxx_中文.pdf`（没有 `_高清`/`_压缩` 兜底,比如图片译文或未分变体的 PDF 命名），
    会被误判成我们的产物而跳过翻译。带 `_高清`/`_压缩` 后缀的 PDF 命名风险很低
    （PDF 正式产线只会走这条路），裸后缀（图片、以及不区分变体时的 PDF 命名）风险
    略高，这里的取舍是接受这个小概率误伤，换来不用把 AppSettings 一路传进扫描函数。
    """
    stem = _REVISION_SUFFIX_RE.sub("", Path(name).stem)
    stem = _VARIANT_SUFFIX_RE.sub("", stem)
    if "_" not in stem:
        return False
    _, _, label = stem.rpartition("_")
    return label in _BUILTIN_TARGET_LANG_LABELS


def _should_skip_scanned_input(relative_path: Path) -> bool:
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
    if name.startswith("译文("):
        # 旧命名（前缀式）。磁盘上已经堆了大量这种命名的历史译文，改了命名规则
        # 也必须继续认得，不然用户一重新扫描旧输出目录，这些译文就被当成待翻的
        # 原文又翻一遍，白花 API 的钱。
        return True
    return _is_new_style_translated_name(name)


def _should_skip_scanned_pdf(relative_path: Path) -> bool:
    return _should_skip_scanned_input(relative_path)


def _relative_pdf_path(path: Path, source_root: Path | None) -> Path:
    if source_root is None:
        return Path(path.name)
    try:
        if source_root.is_file():
            return Path(path.name)
        return path.relative_to(source_root)
    except ValueError:
        return Path(path.name)


def _load_pdfium():
    try:
        import pypdfium2 as pdfium  # type: ignore
    except Exception as exc:  # noqa: BLE001 - installed dependency may be unusable.
        logger.debug(f"[PDF] pypdfium2 未安装或不可用：{exc!r}")
        raise RuntimeError("读取 PDF 的组件不可用，请重新安装本程序。") from exc
    return pdfium


def _open_pdf_document(path: Path):
    return _load_pdfium().PdfDocument(str(path))


# 「自动识别场景」概括文档时最多读这么多页的文字层；够判断文档类型，又不至于把
# 几百页的册子整本读一遍。
_PROFILE_TEXT_MAX_PAGES = 30


def _extract_pdf_pages(source: Path, target: Path, first: int, last: int) -> None:
    """把第 first–last 页（从 1 数）另存成一份 PDF，用随安装包分发的 pypdfium2。"""
    pdfium = _load_pdfium()
    src = pdfium.PdfDocument(str(source))
    part = pdfium.PdfDocument.new()
    try:
        part.import_pages(src, pages=list(range(first - 1, last)))
        part.save(str(target))
    finally:
        part.close()
        src.close()


def _pdf_text_lines(path: Path, *, max_pages: int = _PROFILE_TEXT_MAX_PAGES) -> list[str]:
    """PDF 文字层里的文字，按行拆开；扫描件或文字已转成线条时返回空。"""
    doc = _open_pdf_document(path)
    lines: list[str] = []
    try:
        for index in range(min(len(doc), max_pages)):
            page = doc[index]
            try:
                textpage = page.get_textpage()
                try:
                    text = textpage.get_text_bounded()
                finally:
                    textpage.close()
            finally:
                page.close()
            lines.extend(line.strip() for line in str(text or "").splitlines() if line.strip())
    finally:
        doc.close()
    return lines


def _pdf_render_scale() -> float:
    return PDF_RENDER_DPI_DEFAULT / 72.0


def _pdf_render_scale_for_page(
    page_width_pt: float,
    page_height_pt: float,
    *,
    max_pixels: int | None = None,
) -> tuple[float, float]:
    """Return (pdfium scale, effective DPI) for one page, capped by total pixels.

    A fixed 300 DPI with no ceiling is fine right up until someone feeds in an
    A0 drawing with "skip oversized pages" off (it is off by default): that page
    alone is ~139 megapixels, i.e. a ~558MB pdfium bitmap plus a ~418MB PIL copy.
    Refusing the page would be worse than a soft answer, so the DPI drops for
    that page only, and the caller records the downgrade.
    """
    # 上限在调用时才解析，测试要能把它临时压小来验证降级路径。
    ceiling = PDF_PAGE_MAX_RENDER_PIXELS if max_pixels is None else int(max_pixels)
    scale = _pdf_render_scale()
    try:
        width_pt = float(page_width_pt)
        height_pt = float(page_height_pt)
    except (TypeError, ValueError):
        return scale, float(PDF_RENDER_DPI_DEFAULT)
    if width_pt <= 0 or height_pt <= 0:
        return scale, float(PDF_RENDER_DPI_DEFAULT)

    pixels = (width_pt * scale) * (height_pt * scale)
    if pixels <= ceiling:
        return scale, float(PDF_RENDER_DPI_DEFAULT)

    capped_scale = scale * (ceiling / pixels) ** 0.5
    dpi = max(PDF_PAGE_MIN_RENDER_DPI, capped_scale * 72.0)
    return dpi / 72.0, dpi


def _pdf_read_error_message(exc: Exception) -> str:
    if "password" in str(exc).lower():
        return "此 PDF 受密码或权限保护，暂时无法翻译。"
    logger.debug(f"[PDF] 打开 PDF 失败原始错误：{exc!r}")
    return f"PDF 读取失败：{user_facing_reason(exc, fallback='无法打开此 PDF，文件可能已损坏或扩展名与实际格式不符。')}"


def _append_pdf_image_page(
    pdfium,
    document,
    *,
    image_path: Path,
    page_width_pt: float,
    page_height_pt: float,
) -> None:
    image_object = pdfium.PdfImage.new(document)
    bitmap = None
    page = None
    try:
        if image_path.suffix.lower() in {".jpg", ".jpeg"}:
            image_object.load_jpeg(image_path, inline=True)
        else:
            with Image.open(image_path) as source:
                normalized = _to_white_rgb(ImageOps.exif_transpose(source))
                bitmap = pdfium.PdfBitmap.from_pil(normalized)
            image_object.set_bitmap(bitmap)
        image_object.set_matrix(pdfium.PdfMatrix().scale(page_width_pt, page_height_pt))
        page = document.new_page(page_width_pt, page_height_pt)
        page.insert_obj(image_object)
        page.gen_content()
    finally:
        if bitmap is not None:
            bitmap.close()
        if page is not None:
            page.close()


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
    for path in target_dir.iterdir() if target_dir.exists() else ():
        match = pattern.match(path.name)
        if match:
            revision = max(revision, int(match.group(1)) + 1)
    return revision


def _has_revision_files(target_dir: Path, stem: str, suffix: str) -> bool:
    pattern = re.compile(rf"^{re.escape(stem)}_R(\d+){re.escape(suffix)}$")
    return any(
        pattern.match(path.name)
        for path in target_dir.iterdir()
    ) if target_dir.exists() else False


def _safe_file_size(path: str | Path) -> int:
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


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


def _load_placeholder_font(size: int, *, bold: bool = False):
    # 占位页整页都是中文说明。Windows 上没有 CJK 字体候选时会掉到
    # ImageFont.load_default()，整段说明渲染成方框——用户根本读不到失败原因，
    # 所以 Windows 的常见 CJK 字体必须排在候选列表里。
    windows_fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    candidates = [
        # Windows：微软雅黑（Vista 起随系统安装）→ 黑体 → 宋体 → 等线。
        str(windows_fonts / ("msyhbd.ttc" if bold else "msyh.ttc")),
        str(windows_fonts / ("msyhbd.ttf" if bold else "msyh.ttf")),
        str(windows_fonts / "msyh.ttc"),
        str(windows_fonts / "simhei.ttf"),
        str(windows_fonts / "simsun.ttc"),
        str(windows_fonts / ("Deng b.ttf" if bold else "Deng.ttf")),
        str(windows_fonts / ("arialbd.ttf" if bold else "arial.ttf")),
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/Supplemental/Songti.ttc",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for candidate in candidates:
        path = Path(candidate)
        if not path.exists():
            continue
        try:
            return ImageFont.truetype(str(path), size=size)
        except Exception:
            continue
    return ImageFont.load_default(size=size)


def _font_line_height(font) -> int:
    try:
        bbox = font.getbbox("中文Ag")
        return max(1, int(bbox[3] - bbox[1]))
    except Exception:
        return 28


def _wrap_text_to_width(
    text: str,
    *,
    max_width: int,
    font,
    draw: ImageDraw.ImageDraw,
) -> list[str]:
    wrapped: list[str] = []
    for paragraph in str(text or "").splitlines() or [""]:
        current = ""
        for char in paragraph:
            candidate = current + char
            if current and _text_width(draw, candidate, font) > max_width:
                wrapped.append(current.rstrip())
                current = char.lstrip()
            else:
                current = candidate
        wrapped.append(current.rstrip())
    return wrapped or [""]


def _text_width(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        return int(bbox[2] - bbox[0])
    except Exception:
        return len(text) * 14


def _localized_pdf_placeholder_problem(error_summary: str) -> str:
    """Return the失败占位页's "问题描述" line — always a plain Chinese sentence."""
    raw = str(error_summary or "").strip()
    if not raw:
        return "未知错误。"
    # 质检类判定不走 humanize_error（它只认库/接口的错误串），这几个是我们
    # 自己产生的短代号，逐字翻过来就够。
    quality_labels = {
        "temporary image failure": "图像生成临时失败。",
        "low resolution": "生成图像分辨率过低。",
        "image generation failed": "图像生成失败。",
        "decode error": "生成图像无法解码。",
        "ratio error": "页面比例不匹配。",
    }
    for marker, sentence in quality_labels.items():
        if marker in raw.lower():
            return sentence
    return user_facing_reason(
        raw,
        fallback="PDF 与图片翻译模型本次未返回可用结果。",
    )


# 一份文件「已经交出了什么产物」的全部字段。终态任务的单页重生成把它拍下来，
# 重生成没成功时原样还原：磁盘上的旧产物没被动过，记录也不该被动过。
_FILE_ARTIFACT_FIELDS = (
    "status",
    "error",
    "translated_pdf_path",
    "compressed_pdf_path",
    "translated_image_path",
    "translated_image_format",
    "compression_error",
    "high_quality_pdf_size_bytes",
    "compressed_pdf_size_bytes",
)


def _file_artifact_snapshot(record: PdfFileRecord) -> dict[str, Any]:
    return {name: getattr(record, name) for name in _FILE_ARTIFACT_FIELDS}


def _restore_file_artifacts(record: PdfFileRecord, snapshot: dict[str, Any]) -> None:
    for name, value in snapshot.items():
        setattr(record, name, value)


# 一页「这一版跑出了什么结论」的全部字段：换回上一版时，这些要跟页图一起对调。
# 刻意不含源页相关字段（source_image_path / source_*_px / page_*_pt / render_dpi）——
# 那些描述的是原文页本身，两版之间不会变，对调它们只会给还原留下犯错的机会。
_PAGE_VERSION_FIELDS = (
    "translated_image_path",
    "status",
    "attempts",
    "error",
    "emergency_ratio_normalized",
    "ratio_letterboxed",
    "quality_flags",
    "quality_message",
    "placeholder",
    "failure_ordinal",
    "output_width_px",
    "output_height_px",
    "review_enabled",
    "review_status",
    "review_attempts",
    "review_issues",
    "review_minor_suggestions",
    "candidate_artifacts",
    "final_candidate_attempt",
)


def _page_version_state(page: PdfPageRecord) -> dict[str, Any]:
    """把一页当前这一版的结论拍成可直接写进 task_history 的纯 JSON 结构。"""
    return copy.deepcopy({name: getattr(page, name) for name in _PAGE_VERSION_FIELDS})


def _apply_page_version_state(page: PdfPageRecord, state: dict[str, Any]) -> None:
    """把一份版本结论写回页记录；不认识的键一律忽略（旧数据可能少字段）。"""
    for name in _PAGE_VERSION_FIELDS:
        if name in state:
            setattr(page, name, copy.deepcopy(state[name]))


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


def _path_is_within(candidate: Path, base_dir: Path) -> bool:
    """Return whether a resolved path really lives under a resolved base dir."""
    try:
        resolved = candidate.resolve(strict=False)
        base = base_dir.resolve(strict=False)
    except OSError:
        return False
    return resolved == base or base in resolved.parents


def _short_review_text(value: str, *, limit: int = 200) -> str:
    """Return panel-safe text: no absolute paths, no unbounded model prose."""
    text = redact_absolute_paths(str(value or "")).strip()
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def _page_review_summary(page: PdfPageRecord | None) -> str:
    if page is None:
        return ""
    problems = [
        str(issue.get("problem") or "").strip()
        for issue in page.review_issues
        if isinstance(issue, dict) and str(issue.get("problem") or "").strip()
    ]
    if problems:
        return _short_review_text("；".join(problems[:3]))
    return _short_review_text(page.error)


def _translated_page_output_path(
    translated_pages_dir: Path,
    *,
    page_record: PdfPageRecord,
    page_count: int,
    image_bytes: bytes,
    preserve_model_format: bool,
) -> Path:
    output_path = translated_pages_dir / page_image_name(page_record.page_number, page_count)
    if not preserve_model_format:
        return output_path
    return output_path.with_suffix(_image_extension_from_bytes(image_bytes))


def _write_translated_page_image(
    image_bytes: bytes,
    output_path: Path,
    *,
    preserve_model_format: bool,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if preserve_model_format:
        output_path.write_bytes(image_bytes)
        return
    with Image.open(BytesIO(image_bytes)) as image:
        image.save(output_path, format="PNG")


def _image_format_from_bytes(image_bytes: bytes) -> str:
    try:
        with Image.open(BytesIO(image_bytes)) as image:
            return str(image.format or "").upper()
    except Exception:
        return ""


def _image_extension_from_bytes(image_bytes: bytes) -> str:
    image_format = _image_format_from_bytes(image_bytes)
    return {
        "JPEG": ".jpg",
        "JPG": ".jpg",
        "PNG": ".png",
        "WEBP": ".webp",
        "BMP": ".bmp",
        "TIFF": ".tiff",
    }.get(image_format, f".{image_format.lower()}" if image_format else ".png")


def _image_format_from_path(path: str | Path) -> str:
    try:
        with Image.open(path) as image:
            return str(image.format or Path(path).suffix.lstrip(".")).upper()
    except Exception:
        return Path(path).suffix.lstrip(".").upper()


def _write_review_json(output_path: Path, result: PdfPageReviewResult) -> None:
    # Keep only structured review conclusions. Raw provider responses must not
    # be persisted into local result evidence.
    payload = {
        "pass": bool(result.passed),
        "blocking_issues": [
            {
                "type": issue.type,
                "location": issue.location,
                "problem": issue.problem,
                "suggestion": issue.suggestion,
            }
            for issue in result.blocking_issues
        ],
        "minor_suggestions": list(result.minor_suggestions),
        "summary": result.summary,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


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


