"""PDF 翻译的结果整理：页数统计、状态文案、清单（manifest）与报告、接口返回的结果结构。

从 pdf_image_translation.py 拆出来。这些都是只读任务记录、不改磁盘状态（写报告
文件的 _write_review_json 除外）的纯函数，单独放一个模块更容易看清「任务结束后
用户看到的数字和文字是怎么来的」。pdf_image_translation 会把这里的名字原样再导出。
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from config import PDF_RENDER_DPI_DEFAULT
from core.file_progress import file_progress_id
from core.pdf_models import (
    PDF_OUTPUT_STATE_COMPLETED,
    PDF_OUTPUT_STATE_FAILED,
    PDF_OUTPUT_STATE_NEEDS_REVIEW,
    PDF_OUTPUT_STATE_STOPPED,
    PDF_PAGES_ROOT,
    PDF_PAGE_STATUS_SKIPPED_OVERSIZE,
    PdfFileRecord,
    PdfPageRecord,
    PdfTaskSummary,
    REVIEW_CANDIDATES_DIRNAME,
    SOURCE_PAGES_DIRNAME,
    SOURCE_TYPE_IMAGE,
    TRANSLATED_PAGES_DIRNAME,
)
from core.pdf_review import PdfPageReviewResult


def _record_is_fully_skipped_oversize(record: PdfFileRecord) -> bool:
    """A file where every single page was skipped for being oversized.

    The artifact is then byte-for-byte the source content — not one word was
    translated — so it must never be reported as a completed translation.
    """
    return bool(
        record.page_count > 0
        and record.skipped_oversize_page_count >= record.page_count
    )


def fully_skipped_oversize_message(record: PdfFileRecord) -> str:
    return (
        f"本文件全部 {record.page_count} 页均因幅面过大被跳过，未翻译任何内容，"
        "输出 PDF 与源文件一致。如需翻译这些页面，"
        "请关闭「跳过 A3 及更大的页面」，或在「翻译方式」里改选原位替换后重新翻译。"
    )


def _page_copied_from_source(page: Any) -> bool:
    """输出里这一页是源文件原页（按设定保留、没有图像模型、原位替换没换成…）。"""
    return bool(
        getattr(page, "skipped_oversize", False)
        and getattr(page, "inplace_status", "") != "done"
        and getattr(page, "word_status", "") != "done"
    )


def _record_translated_nothing(record: PdfFileRecord) -> bool:
    """Every page of the output is the source page, whatever the reason."""
    return bool(
        record.page_count > 0
        and len(record.pages) >= record.page_count
        and all(_page_copied_from_source(page) for page in record.pages)
    )


def _page_kept_reason(page: Any) -> str:
    if getattr(page, "inplace_requested", False) and getattr(page, "inplace_status", ""):
        reason = page.inplace_message or ""
    else:
        reason = getattr(page, "kept_reason", "") or ""
    return reason


def translated_nothing_message(record: PdfFileRecord) -> str:
    reasons = [
        (page.page_number, _page_kept_reason(page))
        for page in sorted(record.pages, key=lambda item: item.page_number)
    ]
    reasons = [(number, reason) for number, reason in reasons if reason]
    message = "本文件没有翻译任何内容，输出 PDF 与源文件一致。"
    if len({reason for _number, reason in reasons}) == 1:
        return message + f"原因：{reasons[0][1]}。"
    if reasons:
        return message + "各页原因见任务报告。"
    return message


def _page_archive_stem(relative_pdf: Path) -> Path:
    return relative_pdf.with_suffix("")


def _format_file_size(size_bytes: int) -> str:
    if size_bytes <= 0:
        return "未知"
    size = float(size_bytes)
    for unit in ("B", "KB", "MB"):
        if size < 1024 or unit == "MB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} MB"


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


def _completed_pdf_file_count(summary: PdfTaskSummary) -> int:
    return sum(
        1
        for record in summary.files
        if record.source_type != SOURCE_TYPE_IMAGE
        and (record.translated_pdf_path or record.translated_docx_path)
    )


def _unfinished_pdf_file_count(summary: PdfTaskSummary) -> int:
    return sum(
        1
        for record in summary.files
        if record.source_type != SOURCE_TYPE_IMAGE
        and not (record.translated_pdf_path or record.translated_docx_path)
    )


def _completed_image_file_count(summary: PdfTaskSummary) -> int:
    return sum(
        1
        for record in summary.files
        if record.source_type == SOURCE_TYPE_IMAGE and record.translated_image_path
    )


def _unfinished_image_file_count(summary: PdfTaskSummary) -> int:
    return sum(
        1
        for record in summary.files
        if record.source_type == SOURCE_TYPE_IMAGE and not record.translated_image_path
    )


def _summary_terminal_reason(summary: PdfTaskSummary) -> str:
    if summary.stopped or summary.status == PDF_OUTPUT_STATE_STOPPED:
        return summary.stop_reason or "user_stopped"
    if summary.status == PDF_OUTPUT_STATE_FAILED:
        return "failed"
    if summary.status == PDF_OUTPUT_STATE_NEEDS_REVIEW:
        return "completed_needs_review"
    return "completed"


def _summary_terminal_reason_label(summary: PdfTaskSummary) -> str:
    reason = _summary_terminal_reason(summary)
    return {
        "end_paused": "用户结束了任务",
        "user_stopped": "用户停止了任务",
        "failed": "出错失败",
        "completed_needs_review": "已完成，有页面需要复核",
        "completed": "正常完成",
    }.get(reason, reason)


def _pdf_status_label(status: str) -> str:
    return {
        PDF_OUTPUT_STATE_COMPLETED: "已完成",
        PDF_OUTPUT_STATE_NEEDS_REVIEW: "已完成，有页面需要复核",
        PDF_OUTPUT_STATE_STOPPED: "已停止",
        PDF_OUTPUT_STATE_FAILED: "出错失败",
    }.get(status, status or "未知")


# 报告的逐页表格给人看，不写内部状态码。
_PDF_PAGE_STATUS_LABELS = {
    "success": "已生成",
    "emergency_normalized": "已生成（自动修正了比例）",
    "placeholder": "失败占位页",
    "placeholder_pending": "失败，待生成占位页",
    "pending": "未开始",
    "rendered": "未开始",
    "failed": "失败",
    PDF_PAGE_STATUS_SKIPPED_OVERSIZE: "大幅面，未翻译",
    "stopped_unstarted": "未开始",
}

_PDF_REVIEW_STATUS_LABELS = {
    "passed": "通过",
    "failed": "未通过",
    "retrying": "重试中",
    "skipped": "未审核",
}


def _pdf_page_status_label(status: str) -> str:
    return _PDF_PAGE_STATUS_LABELS.get(str(status or ""), str(status or "未知"))


def _pdf_review_status_label(status: str) -> str:
    return _PDF_REVIEW_STATUS_LABELS.get(str(status or ""), str(status or "未审核"))


def _pdf_file_status_label(record: PdfFileRecord) -> str:
    if record.source_type == SOURCE_TYPE_IMAGE:
        if record.translated_image_path:
            return "已完成译图"
        if record.status == PDF_OUTPUT_STATE_FAILED:
            return "生成失败"
        if record.status == PDF_OUTPUT_STATE_STOPPED:
            return "未完成图片，已保留页面素材"
        return "未完成图片"
    if record.translated_pdf_path:
        return "已完成 PDF"
    if record.status == PDF_OUTPUT_STATE_FAILED:
        return "生成失败"
    if record.status == PDF_OUTPUT_STATE_STOPPED:
        return "未完成 PDF，已保留页面素材"
    return "未完成 PDF"


def _pdf_artifact_label(path: str, record: PdfFileRecord) -> str:
    if path:
        return path
    if record.source_type == SOURCE_TYPE_IMAGE:
        if record.status == PDF_OUTPUT_STATE_STOPPED:
            return "未生成（未完成，不生成失败译图）"
        return "未生成"
    if record.status == PDF_OUTPUT_STATE_STOPPED:
        return "未生成（未完成，不生成占位版）"
    return "未生成"


def _pdf_file_material_paths(summary: PdfTaskSummary, record: PdfFileRecord) -> dict[str, str]:
    output_dir = Path(summary.output_dir)
    archive_stem = _page_archive_stem(
        Path(record.relative_path or record.name or "未命名 PDF")
    )
    return {
        "source_pages": str(output_dir / PDF_PAGES_ROOT / SOURCE_PAGES_DIRNAME / archive_stem),
        "translated_pages": str(
            output_dir / PDF_PAGES_ROOT / TRANSLATED_PAGES_DIRNAME / archive_stem
        ),
        "review_candidates": str(
            output_dir / PDF_PAGES_ROOT / REVIEW_CANDIDATES_DIRNAME / archive_stem
        ),
    }


def _record_has_usable_translated_pages(record: PdfFileRecord) -> bool:
    return any(
        (
            page.translated_image_path
            and not page.placeholder
            and page.status in {"success", "emergency_normalized"}
        )
        # 跳过大幅面页的页面没有 translated_image_path（矢量直传，不走栅格化），
        # 但它本身就是一份有效产物；一份全是大幅面页的 PDF 不该被判定成「零
        # 可用页面」而整份失败。没有图像模型而保留原样的页不算：一页都没翻。
        or (page.skipped_oversize and not getattr(page, "kept_reason", ""))
        for page in record.pages
    )


def _no_usable_translated_pages_error(record: PdfFileRecord) -> str:
    page_errors = [
        str(page.error or "").strip()
        for page in sorted(record.pages, key=lambda item: item.page_number)
        if page.placeholder or page.status in {"placeholder_pending", PDF_OUTPUT_STATE_FAILED}
    ]
    first_error = next((error for error in page_errors if error), "")
    if record.pages and all(getattr(page, "kept_reason", "") for page in record.pages):
        if all(page.kept_reason.startswith("没有设置图像翻译模型") for page in record.pages):
            return "这份文件取不到文字，需要整页图像翻译，但还没有设置图像翻译模型，没有生成译文。"
        return f"没有一页能按所选方式翻译，没有生成译文。第 1 页：{record.pages[0].kept_reason}。"
    if record.source_type == SOURCE_TYPE_IMAGE:
        message = "图片翻译失败，未生成译图；已保留源图、页面素材和报告。"
    else:
        message = "全部页面生成失败，未生成译文 PDF；已保留源页、失败页素材和报告。"
    if first_error:
        message += f"首个错误：{first_error}"
    return message


_DOCUMENT_KIND_TEXT = {"text": "文字文档", "drawing": "图纸", "scan": "扫描件", "mixed": "图文混合"}


def _route_report_lines(record: PdfFileRecord) -> list[str]:
    """翻译方式（Beta 分支）：文件判断成什么、走了哪条路、没有图像模型而保留的页。"""
    lines: list[str] = []
    if record.document_kind:
        lines.append(f"- 文件类型判断：{_DOCUMENT_KIND_TEXT.get(record.document_kind, record.document_kind)}")
    if record.translated_docx_path:
        lines.append(f"- 另存的 Word：{record.translated_docx_path}")
    for segment in getattr(record, "word_segments", []) or []:
        pages = (f"第 {segment.get('first_page')} 页" if segment.get("first_page") == segment.get("last_page")
                 else f"第 {segment.get('first_page')}–{segment.get('last_page')} 页")
        if segment.get("status") == "done":
            lines.append(f"- {pages}：文字可以提取，转成 Word 翻译（Beta）后转回 PDF 拼入")
        else:
            lines.append(f"- {pages}：转成 Word 没有成功：{segment.get('message', '')}")
    # 没按首选方式翻译的页：同一个原因的页并成一行（「第 1、2 页：……，这一页保留原样」）。
    for field_name in ("route_note", "kept_reason"):
        grouped: dict[str, list[int]] = {}
        for page in sorted(record.pages, key=lambda item: item.page_number):
            reason = str(getattr(page, field_name, "") or "")
            if reason:
                grouped.setdefault(reason, []).append(page.page_number)
        for reason, numbers in grouped.items():
            shown = "、".join(str(number) for number in numbers[:30]) + ("…" if len(numbers) > 30 else "")
            lines.append(f"- 第 {shown} 页：{reason}")
    return lines


def _inplace_report_lines(record: PdfFileRecord) -> list[str]:
    """「原位替换文字（Beta）」的逐页结果：替换了多少处、哪些地方保留了原文。"""
    pages = [page for page in record.pages if getattr(page, "inplace_requested", False)]
    if not pages:
        return []
    done = [page for page in pages if page.inplace_status == "done"]
    lines = [
        f"- 原位替换文字（Beta）：{len(done)}/{len(pages)} 页已替换，"
        f"共替换 {sum(page.inplace_written for page in done)} 处，"
        f"保留原文 {sum(page.inplace_review for page in done)} 处"
    ]
    for page in sorted(pages, key=lambda item: item.page_number):
        lines.append(f"  - 第 {page.page_number} 页：{page.inplace_message or page.inplace_status}")
        for item in page.inplace_review_items[:30]:
            if item.get("kind") == "term":
                lines.append(f"    - 需复核「{item.get('text', '')}」：{item.get('detail') or item.get('reason', '')}")
            else:
                lines.append(f"    - 保留原文「{item.get('text', '')}」：{item.get('reason', '')}")
    return lines


def _finished_page_numbers(record: PdfFileRecord) -> list[int]:
    numbers: list[int] = []
    for page in record.pages:
        if (
            page.status in {"success", "emergency_normalized", "placeholder", PDF_PAGE_STATUS_SKIPPED_OVERSIZE}
            # placeholder_pending 的页要排掉：它的 translated_image_path 指着一张
            # 「page_00N_failed.png」，那是占位图**将要**写到的位置，中止时这一步根本没跑，
            # 文件并不存在。不排掉的话报告会把 3 页失败页算成已完成（12/14 而不是 9/14）。
            or (
                page.translated_image_path
                and page.status
                not in {"pending", "failed", "placeholder_pending", PDF_OUTPUT_STATE_FAILED}
            )
        ):
            numbers.append(int(page.page_number))
    return sorted(set(numbers))


def _finished_page_count(record: PdfFileRecord) -> int:
    page_count = max(0, int(record.page_count or 0))
    explicit = max(0, int(record.generated_page_count or 0))
    computed = len(_finished_page_numbers(record))
    if record.translated_pdf_path and page_count and not max(explicit, computed):
        return page_count
    finished = max(explicit, computed)
    return min(page_count, finished) if page_count else finished


def _last_contiguous_finished_page(record: PdfFileRecord) -> int:
    page_count = max(0, int(record.page_count or 0))
    numbers = set(_finished_page_numbers(record))
    if record.translated_pdf_path and page_count and not numbers:
        return page_count
    current = 0
    for page_number in range(1, page_count + 1):
        if page_number not in numbers:
            break
        current = page_number
    return current


def _page_progress_label(record: PdfFileRecord) -> str:
    page_count = max(0, int(record.page_count or 0))
    finished = _finished_page_count(record)
    return f"{finished}/{page_count}" if page_count else str(finished)


def _real_generated_page_count(record: PdfFileRecord) -> int:
    """真正生成了译图的页数。

    「跑完的页」（_finished_page_count）里混着占位页和整页跳过的 A3+ 页——它们确实各自
    留下了一张图，但都不是译文。界面上「已生成 N / M 页」这句话早就是扣掉占位页之后的数
    （见 workspace.ts 的 pdf 进度徽标），报告和任务中心也得按同一个口径说，否则又会出现
    「已生成页面 14/14」和「失败占位页 2」并排的自相矛盾。
    """
    finished = _finished_page_count(record)
    # 原页照搬的页（按设定保留、没有图像模型、原位替换没成功）都不是译文
    copied = sum(1 for page in record.pages if page.skipped_oversize and page.inplace_status != "done"
                 and getattr(page, "word_status", "") != "done")
    not_translated = max(0, int(record.placeholder_page_count or 0)) + max(
        copied, max(0, int(record.skipped_oversize_page_count or 0))
    )
    return max(0, finished - not_translated)


def _generated_progress_label(record: PdfFileRecord) -> str:
    page_count = max(0, int(record.page_count or 0))
    generated = _real_generated_page_count(record)
    return f"{generated}/{page_count}" if page_count else str(generated)


def _unstarted_page_count(record: PdfFileRecord) -> int:
    """从未开始处理的页数：中止时没轮到的那几页，连页记录都还没建。

    以页记录条数为准，但不能只看它：合成成功的文件可以只留下产物路径而不留页记录
    （历史清单、测试替身都是这样），那种情况下已生成页数才是真正处理过的页数。
    """
    page_count = max(0, int(record.page_count or 0))
    if not page_count:
        return 0
    touched = max(len(record.pages), _finished_page_count(record))
    return max(0, page_count - touched)


def _last_finished_page_label(record: PdfFileRecord) -> str:
    page_count = max(0, int(record.page_count or 0))
    last_page = _last_contiguous_finished_page(record)
    if not last_page:
        return "尚未处理任何页面"
    if page_count and last_page >= page_count:
        return "全部页面"
    return f"第 {last_page} 页"


def _summary_to_manifest(summary: PdfTaskSummary) -> dict[str, Any]:
    payload = asdict(summary)
    # previous_page_state 是「换回上一版」的进程内快照（页尺寸、结论等运行期字段），
    # 只在换回流程里读写，清单没有它的读者；写出去等于把内部字段升格成对外格式，
    # 以后一改就背兼容包袱。previous_image_path 保留——它回答「这一页有没有上一版
    # 可换」，排查清单时用得上。
    for file_payload in payload.get("files") or []:
        for page_payload in file_payload.get("pages") or []:
            page_payload.pop("previous_page_state", None)
    payload["route"] = "pdf_image_layout_translation"
    payload["render_dpi"] = PDF_RENDER_DPI_DEFAULT
    payload["image_format"] = "png"
    payload["terminal_reason"] = _summary_terminal_reason(summary)
    payload["terminal_reason_label"] = _summary_terminal_reason_label(summary)
    payload["completed_pdf_file_count"] = _completed_pdf_file_count(summary)
    payload["unfinished_pdf_file_count"] = _unfinished_pdf_file_count(summary)
    payload["completed_image_file_count"] = _completed_image_file_count(summary)
    payload["unfinished_image_file_count"] = _unfinished_image_file_count(summary)
    return payload


def _summary_to_report(summary: PdfTaskSummary) -> str:
    lines = [
        "# PDF 翻译报告",
        "",
        f"- 状态：{_pdf_status_label(summary.status)}",
        f"- 结束原因：{_summary_terminal_reason_label(summary)}",
        f"- 目标语言：{summary.target_lang_label}",
        f"- 输出目录：{summary.output_dir}",
        f"- 文件数：{summary.file_count}",
        f"- 总页数：{summary.total_page_count}",
        f"- 已完成 PDF 文件：{_completed_pdf_file_count(summary)}",
        f"- 未完成 PDF 文件：{_unfinished_pdf_file_count(summary)}",
        f"- 已完成图片文件：{_completed_image_file_count(summary)}",
        f"- 未完成图片文件：{_unfinished_image_file_count(summary)}",
        f"- 已生成高清 PDF：{summary.generated_pdf_count}",
        f"- 已生成压缩 PDF：{summary.compressed_pdf_count}",
        f"- 已生成译图：{summary.generated_image_count}",
        *([f"- 已转成 Word 翻译（Beta）：{summary.generated_docx_count}"] if summary.generated_docx_count else []),
        f"- 压缩输出：{'开启' if summary.compressed_pdf_enabled else '关闭'}",
        f"- 失败占位页：{summary.placeholder_page_count}",
        f"- 大幅面页（未翻译，原样保留）：{summary.skipped_oversize_page_count}",
        f"- 自动修正比例的页：{summary.emergency_ratio_normalized_count}",
        f"- 重试次数：{summary.retry_count}",
        f"- 逐页审核：{'开启' if summary.review_enabled else '关闭'}",
        f"- 审核通过页：{summary.review_passed_page_count}",
        f"- 审核修复页：{summary.review_repaired_page_count}",
        f"- 审核未通过页：{summary.review_failed_page_count}",
        f"- 审核重试次数：{summary.review_retry_count}",
        f"- 审核轻微建议：{summary.review_minor_suggestion_count}",
        f"- 因模型繁忙而降速的次数：{summary.rate_limit_reduction_count}",
        f"- 保留部分素材：{'是' if summary.partial_artifacts_available else '否'}",
        f"- 页面素材目录：{Path(summary.output_dir) / PDF_PAGES_ROOT}",
        "",
        "## 文件明细",
        "",
    ]
    for file_record in summary.files:
        material_paths = _pdf_file_material_paths(summary, file_record)
        lines.extend(
            [
                f"### {file_record.name}",
                "",
                f"- 状态：{_pdf_file_status_label(file_record)}",
                f"- 类型：{'图片' if file_record.source_type == SOURCE_TYPE_IMAGE else 'PDF'}",
                f"- 源{'图片' if file_record.source_type == SOURCE_TYPE_IMAGE else 'PDF'}：{file_record.source_path}",
                f"- 高清 PDF：{_pdf_artifact_label(file_record.translated_pdf_path, file_record)}",
                f"- 压缩 PDF：{_pdf_artifact_label(file_record.compressed_pdf_path, file_record)}",
                f"- 译图：{_pdf_artifact_label(file_record.translated_image_path, file_record)}",
                f"- 模型输出格式：{file_record.translated_image_format or '未生成'}",
                f"- 源文件体积：{_format_file_size(file_record.source_pdf_size_bytes)}",
                f"- 高清版体积：{_format_file_size(file_record.high_quality_pdf_size_bytes)}",
                f"- 压缩版体积：{_format_file_size(file_record.compressed_pdf_size_bytes)}",
                f"- 页数：{file_record.page_count}",
                # 「页面完成进度」在中止的报告里被读成了「已提交」（实测写 12/14，界面同时
                # 写着 9/14）。改成三行说清楚：真正生成了译文的页、跑到哪一页、一页都没轮到
                # 的页——它们和「失败占位页」「大幅面页」相加正好是总页数。
                f"- 已生成页面：{_generated_progress_label(file_record)}",
                f"- 已连续处理到页码：{_last_finished_page_label(file_record)}",
                f"- 未开始页面：{_unstarted_page_count(file_record)}",
                f"- 源{'图片' if file_record.source_type == SOURCE_TYPE_IMAGE else '页'}素材：{material_paths['source_pages']}",
                f"- 译后{'图片' if file_record.source_type == SOURCE_TYPE_IMAGE else '页'}素材：{material_paths['translated_pages']}",
                f"- 失败占位页：{file_record.placeholder_page_count}",
                f"- 大幅面页处理：本文件 {file_record.skipped_oversize_page_count} 页判定为大幅面，"
                f"将原样保留；{max(0, file_record.page_count - file_record.skipped_oversize_page_count)} "
                "页参与翻译",
                *_inplace_report_lines(file_record),
                *_route_report_lines(file_record),
                f"- 自动修正比例的页：{file_record.emergency_ratio_normalized_count}",
                f"- 自动检查发现问题、待复核的页：{file_record.quality_flagged_page_count}",
                f"- 补白边保持比例的页：{file_record.ratio_letterboxed_page_count}",
                f"- 因页面过大降低清晰度的页：{file_record.render_downscaled_page_count}",
                f"- 逐页审核：{'开启' if file_record.review_enabled else '关闭'}",
                f"- 审核通过页：{file_record.review_passed_page_count}",
                f"- 审核修复页：{file_record.review_repaired_page_count}",
                f"- 审核未通过页：{file_record.review_failed_page_count}",
                f"- 审核轻微建议：{file_record.review_minor_suggestion_count}",
            ]
        )
        if summary.review_enabled or file_record.review_enabled:
            lines.append(f"- 审核候选图：{material_paths['review_candidates']}")
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
            or page.quality_flags
            or page.review_issues
            or page.review_minor_suggestions
        ]
        if review_pages:
            lines.extend(["", "| 页码 | 状态 | 审核 | 失败页编号 | 摘要 | 原始页图像 | 译后页图像 |", "| --- | --- | --- | --- | --- | --- | --- |"])
            for page in review_pages:
                review_note = _pdf_review_status_label(page.review_status)
                if page.review_minor_suggestions:
                    review_note += "；轻微建议：" + "；".join(page.review_minor_suggestions[:2])
                lines.append(
                    "| "
                    + " | ".join(
                        [
                            str(page.page_number),
                            _pdf_page_status_label(page.status),
                            _escape_table(review_note),
                            page.failure_ordinal,
                            _escape_table(
                                page.error
                                or page.quality_message
                                or _page_review_issue_text(page)
                            ),
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
    fully_skipped = _record_is_fully_skipped_oversize(record)
    # 全部页面都被大幅面跳过（或因别的原因全是原页）时产物只是源文件副本：文件
    # 确实生成了，但翻译一个字都没有，所以这里不能算 success。
    untranslated = fully_skipped or _record_translated_nothing(record)
    success = bool(
        record.translated_docx_path or record.translated_pdf_path or record.translated_image_path
    ) and not untranslated
    detail_parts = []
    if record.translated_docx_path:
        detail_parts.append("另存了一份 Word")
    kept_pages = sum(1 for page in record.pages if getattr(page, "kept_reason", ""))
    if kept_pages:
        detail_parts.append(f"{kept_pages} 页没能按所选方式翻译，保留原样，原因见任务报告")
    rerouted_pages = sum(1 for page in record.pages if getattr(page, "route_note", ""))
    if rerouted_pages:
        detail_parts.append(f"{rerouted_pages} 页改用整页图像翻译，原因见任务报告")
    if fully_skipped:
        detail_parts.append("全部页面因幅面过大被跳过，未翻译")
    elif untranslated:
        detail_parts.append("没有翻译任何内容，原因见任务报告")
    if record.quality_flagged_page_count:
        detail_parts.append(f"{record.quality_flagged_page_count} 页自动检查发现问题，待复核")
    if record.compression_error:
        detail_parts.append("压缩版生成失败")
    return {
        "file_id": file_progress_id(record.source_path),
        "name": record.name,
        "source_type": record.source_type,
        "success": success,
        "status": record.status,
        "detail": "，".join(detail_parts),
        "error": (
            record.error
            or (fully_skipped_oversize_message(record) if fully_skipped else "")
            or (translated_nothing_message(record) if untranslated else "")
        ),
        "all_pages_skipped_oversize": fully_skipped,
        "untranslated": untranslated,
        "quality_flagged_page_count": record.quality_flagged_page_count,
        "ratio_letterboxed_page_count": record.ratio_letterboxed_page_count,
        "render_downscaled_page_count": record.render_downscaled_page_count,
        "output": record.translated_pdf_path or record.translated_image_path or record.translated_docx_path,
        "output_format": "image" if record.translated_image_path else "pdf",
        "word_output": record.translated_docx_path,
        "document_kind": record.document_kind,
        "compressed_output": record.compressed_pdf_path,
        "translated_image_path": record.translated_image_path,
        "translated_image_format": record.translated_image_format,
        "page_count": record.page_count,
        "placeholder_page_count": record.placeholder_page_count,
        "skipped_oversize_page_count": record.skipped_oversize_page_count,
        "emergency_ratio_normalized_count": record.emergency_ratio_normalized_count,
        "review_enabled": record.review_enabled,
        "reviewed_page_count": record.reviewed_page_count,
        "review_passed_page_count": record.review_passed_page_count,
        "review_repaired_page_count": record.review_repaired_page_count,
        "review_failed_page_count": record.review_failed_page_count,
        "suspect_adopted_page_count": record.suspect_adopted_page_count,
        "review_retry_count": record.review_retry_count,
        "review_minor_suggestion_count": record.review_minor_suggestion_count,
        "high_quality_pdf_size_bytes": record.high_quality_pdf_size_bytes,
        "compressed_pdf_size_bytes": record.compressed_pdf_size_bytes,
    }


def _done_kpi(summary: PdfTaskSummary) -> dict[str, Any]:
    """任务中心那排指标格的数据源。

    PDF 以前一个字段都不给，任务中心的 PDF 指标表因此整段是死代码：跑完点进详情，
    页数、失败占位、审核未通过全都不显示。键名对齐 ui/src/views/tasks.ts 的 PDF 回退表。

    没发生的事不占格子：没有疑点页就不报「有疑点仍采用 0」，没出译图就不报「译图 0」——
    一排零会让人以为哪里出了问题。

    跟占位页并列的那格给的是 suspect_adopted_page_count，不是 review_failed_page_count：
    审核没通过的页有一半是直接退回失败占位页的，两个数并列会让同一页在两个格子里各数一次。
    """
    kpi: dict[str, Any] = {
        "file_count": summary.file_count,
        "total_page_count": summary.total_page_count,
        "generated_pdf_count": summary.generated_pdf_count,
        "placeholder_page_count": summary.placeholder_page_count,
    }
    # 中止和失败的任务，「高清 PDF 0 / 失败占位 0」几乎等于什么都没说：工作区那一屏明写着
    # 「已生成 9 / 14 页」，任务中心得给得出同一个数，否则同一份任务两屏两种说法。跑完的
    # 任务不出这两格——那时已生成页数就是总页数，多一格只是占地方。
    if summary.stopped or summary.status == PDF_OUTPUT_STATE_FAILED:
        kpi["generated_page_count"] = summary.generated_page_count
        if summary.unstarted_page_count:
            kpi["unstarted_page_count"] = summary.unstarted_page_count
    if summary.generated_image_count:
        kpi["generated_image_count"] = summary.generated_image_count
    if summary.generated_docx_count:
        kpi["generated_docx_count"] = summary.generated_docx_count
    if summary.suspect_adopted_page_count:
        kpi["suspect_adopted_page_count"] = summary.suspect_adopted_page_count
    if summary.skipped_oversize_page_count:
        kpi["skipped_oversize_page_count"] = summary.skipped_oversize_page_count
    return kpi


def _summary_issues(file_records: list[PdfFileRecord]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for file_record in file_records:
        if file_record.word_issue_count:
            issues.append(
                {
                    "file": file_record.name,
                    "location_label": "整份文件",
                    "severity": "needs_review",
                    "problem": f"转成 Word 翻译后有 {file_record.word_issue_count} 处需要复核（保留了原文或疑似漏译），详见 Word 的质量报告。",
                    "status": "转成 Word 翻译（Beta）",
                    "source_image_path": file_record.source_path,
                    "translated_image_path": "",
                }
            )
        for page in file_record.pages:
            if page.kept_reason:
                issues.append(
                    {
                        "file": file_record.name,
                        "location_label": f"第 {page.page_number} 页",
                        "severity": "needs_review",
                        "problem": page.kept_reason,
                        "status": "保留原样",
                        "source_image_path": file_record.source_path,
                        "translated_image_path": "",
                    }
                )
        if _record_is_fully_skipped_oversize(file_record):
            issues.append(
                {
                    "file": file_record.name,
                    "location_label": "整份文件",
                    "severity": "needs_review",
                    "problem": fully_skipped_oversize_message(file_record),
                    "status": "全部页面因幅面过大跳过，未翻译",
                    "source_image_path": file_record.source_path,
                    "translated_image_path": "",
                }
            )
        elif _record_translated_nothing(file_record):
            issues.append(
                {
                    "file": file_record.name,
                    "location_label": "整份文件",
                    "severity": "needs_review",
                    "problem": translated_nothing_message(file_record),
                    "status": "未翻译任何内容",
                    "source_image_path": file_record.source_path,
                    "translated_image_path": "",
                }
            )
        for page in file_record.pages:
            if not (
                page.placeholder
                or page.emergency_ratio_normalized
                or page.error
                or page.quality_flags
                or page.review_status == "failed"
            ):
                continue
            issues.append(
                {
                    "file": file_record.name,
                    "location_label": (
                        "图片文件"
                        if file_record.source_type == SOURCE_TYPE_IMAGE
                        else f"第 {page.page_number} 页"
                    ),
                    "severity": "needs_review",
                    "problem": (
                        page.error
                        or page.quality_message
                        or _page_review_issue_text(page)
                        or _pdf_page_status_label(page.status)
                    ),
                    "status": (
                        "审核未通过，占位待人工复核"
                        if page.review_status == "failed"
                        else "失败占位页"
                        if page.placeholder
                        else "已自动修正页面比例"
                        if page.emergency_ratio_normalized
                        else "自动检查发现问题，待复核"
                        if page.quality_flags
                        else _pdf_page_status_label(page.status)
                    ),
                    "source_image_path": page.source_image_path,
                    "translated_image_path": page.translated_image_path,
                }
            )
    return issues
