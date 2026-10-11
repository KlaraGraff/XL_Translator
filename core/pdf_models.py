"""PDF 翻译的数据模型：路径与状态常量、扫描结果、页/文件/任务记录。

从 pdf_image_translation.py 拆出来，让运行器、报告生成和接口层都能引用同一套
定义，而不必把 6000 多行的运行器整个导入。这里只放数据，不放任何流程逻辑。
pdf_image_translation 会把这里的名字原样再导出，旧的导入路径继续有效。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import PDF_COMPRESSED_JPEG_QUALITY_DEFAULT, PDF_COMPRESSED_MAX_LONG_EDGE_PX


SOURCE_TYPE_PDF = "pdf"
SOURCE_TYPE_IMAGE = "image"
SUPPORTED_PDF_SUFFIXES = {".pdf"}
SUPPORTED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
# 认得出是图片、但本程序不接的格式。它们和上面那批一样要走「跳过项」，不能悄悄消失。
UNSUPPORTED_IMAGE_SUFFIXES = {".gif", ".avif", ".heic", ".heif", ".svg"}
SKIP_KIND_IMAGES_DISABLED = "images_disabled"
# 中止后等待在飞页面时，状态行的重发间隔（秒）。
STOP_WAIT_STATUS_REFRESH_SECONDS = 5.0
# 「允许独立图片」关着时，文件夹里的图片此前是被 `continue` 直接吃掉的：扫描摘要写
# 「独立图片 0 / 跳过项 0」，日志写「扫描到 0 个图片文件」，用户把图纸截图放进来，
# 从头到尾没有一句话告诉他这些图根本没参与。跳过项这一格正是它该出现的地方。
IMAGES_DISABLED_SKIP_REASON = (
    "「也翻译图片文件」未开启，此图片文件未纳入本次任务；如需翻译，请开启该选项后重新扫描。"
)
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
PDF_PAGE_ACTION_REGENERATE = "regenerate"
PDF_PAGE_ACTION_SKIP = "skip"
# 终态任务单页重生成时，旧译文页图被改名挪到这个前缀下。前缀放在最前面，页图名
# 的所有 glob（页面存档复用、面板取图）都以 page_00N 开头，因此挪走的文件不会被
# 任何一处当成有效页图；重生成成功就删掉，失败就原样搬回。
PDF_PAGE_IMAGE_STASH_PREFIX = ".rerun_backup."
# 重生成成功后，被顶下去的那一版译文页图不再删掉，改名留在这个前缀下当「上一版」。
# 前缀同样放在最前面，理由和上面那条一模一样：所有找当前页图的 glob 都以 page_00N
# 开头，上一版因此不会被任何一处当成有效页图，只由 previous_image_path 指名读取。
# 只留一版：再往前的那版在下一次重生成时被顶替，磁盘不随重跑次数无限长。
PDF_PAGE_IMAGE_PREVIOUS_PREFIX = ".previous."
# 换回上一版时，当前版先改名挪到这个前缀下当中转（三步互换的第一步）。同一目录内
# os.replace，中途失败按相反顺序还原，磁盘上不会留下半套。
PDF_PAGE_IMAGE_SWAP_PREFIX = ".restore_swap."
PDF_PAGE_IMAGE_KIND_SOURCE = "source"
PDF_PAGE_IMAGE_KIND_TRANSLATED = "translated"
PDF_PAGE_IMAGE_KIND_PREVIOUS = "previous"
# 「跳过大幅面页」功能专用的页面终态：页面判定为超出 A4 的大幅面页，从未提交给
# 翻译模型，装订时从源 PDF 矢量直传。语义上和 request_page_skip 的用户手动跳过
# （发生在失败之后）完全不同，不要合并成同一个状态。
PDF_PAGE_STATUS_SKIPPED_OVERSIZE = "skipped_oversize"
# 中止时还没跑出任何结果的页面。占位页是「模型试过了，就是没成」的结论；中止的页
# 没有这个结论——它连一次模型调用都没发生过。这个状态只在工作线程和主循环之间传
# 一次：主循环见到它就把这条页记录整条撤掉，让这一页回到「还没开始」，于是产物不
# 会被烧进占位页、报告如实报截断、续译时这一页还能重新排队。它不写进清单，也不该
# 出现在前端（页记录不在了，快照给的就是 pending）。
PDF_PAGE_STATUS_STOPPED_UNSTARTED = "stopped_unstarted"
# A page may only be re-run or skipped once its own processing unit settled.
# Everything else is still owned by an in-flight future.
_PDF_PAGE_REGENERABLE_STATUSES = frozenset(
    {
        "success",
        "emergency_normalized",
        "placeholder_pending",
        "placeholder",
        PDF_OUTPUT_STATE_FAILED,
    }
)
_PDF_PAGE_SKIPPABLE_STATUSES = frozenset({"placeholder_pending", PDF_OUTPUT_STATE_FAILED})


@dataclass
class PdfFileItem:
    path: Path
    name: str
    size_kb: float
    page_count: int = 0
    source_type: str = SOURCE_TYPE_PDF
    relative_path: str = ""
    format: str = "pdf"
    width_px: int = 0
    height_px: int = 0
    # 扫描阶段的大幅面页（超出 A4 尺寸）预估，仅读 /MediaBox 等元数据，不渲染。
    # None 表示「读不出来」（例如某一页损坏），不是「确认没有大幅面页」——两者
    # 不能用 0 混在一起，否则界面会把「无法判断」显示成「零大幅面页」。与
    # file_scanner 里 image_count / image_count_unknown_files 的三态写法保持一致。
    oversized_page_count: int | None = None


@dataclass(frozen=True)
class PdfScanSkippedItem:
    """A PDF/image-looking input that must remain visible but unselectable."""

    path: Path
    relative_path: str
    reason: str
    format: str = ""
    source_type: str = ""
    # 跳过原因的机器可读分类。目前只区分「开关没开」（images_disabled）和其余，
    # 用来决定摘要那句话怎么说：图片被开关挡下不是「文件坏了」。
    kind: str = ""


@dataclass
class PdfScanResult:
    """Typed PDF/image scan contract consumed by the local API and Tauri UI."""

    root: Path
    include_images: bool
    items: list[PdfFileItem] = field(default_factory=list)
    skipped: list[PdfScanSkippedItem] = field(default_factory=list)

    @property
    def summary(self) -> dict[str, int]:
        pdf_count = sum(1 for item in self.items if item.source_type == SOURCE_TYPE_PDF)
        image_count = sum(1 for item in self.items if item.source_type == SOURCE_TYPE_IMAGE)
        total_units = sum(
            1 if item.source_type == SOURCE_TYPE_IMAGE else max(0, int(item.page_count))
            for item in self.items
        )
        # 大幅面页计数只对 PDF 有意义；独立图片输入没有「页」的概念，不参与
        # 已知/未知的统计，否则会把「单张图片」错记成「零大幅面页的 PDF」。
        pdf_items = [item for item in self.items if item.source_type == SOURCE_TYPE_PDF]
        known_oversized_page_counts = [
            item.oversized_page_count for item in pdf_items if item.oversized_page_count is not None
        ]
        return {
            "scanned_count": len(self.items),
            "selected_count": len(self.items),
            "pdf_count": pdf_count,
            "image_count": image_count,
            "total_page_or_image_count": total_units,
            "skipped_count": len(self.skipped),
            "oversized_page_count": sum(known_oversized_page_counts),
            "oversized_page_count_unknown_files": sum(
                1 for item in pdf_items if item.oversized_page_count is None
            ),
        }

    @property
    def risk(self) -> dict[str, object]:
        skipped_count = len(self.skipped)
        images_disabled_count = sum(
            1 for item in self.skipped if item.kind == SKIP_KIND_IMAGES_DISABLED
        )
        if not skipped_count:
            message = ""
        elif images_disabled_count == skipped_count:
            message = (
                f"文件夹中有 {images_disabled_count} 个图片文件，因「也翻译图片文件」未开启，本次不会翻译；"
                "其余文件仍可继续处理。"
            )
        else:
            message = (
                "部分 PDF/图片不可读、不受支持或未纳入本次任务，已列入跳过项目；其余文件仍可继续。"
            )
        return {
            "include_images": self.include_images,
            "mixed_input_supported": bool(
                self.summary["pdf_count"] and self.summary["image_count"]
            ),
            "generated_output_excluded": True,
            "has_skipped": bool(skipped_count),
            "skipped_count": skipped_count,
            "images_disabled_count": images_disabled_count,
            "message": message,
        }


@dataclass
class PageQualityResult:
    ok: bool
    status: str
    message: str = ""
    width: int = 0
    height: int = 0
    # 内容维度的疑点。故意不参与 `ok`：这些判据是启发式的，误杀一张正常页
    # 的代价（丢掉一页真译文、白烧一轮重试）比放过一张可疑页大得多，所以
    # 命中的页照样进产物，只是打上「请复核」并在报告里点名。
    review_reasons: list[str] = field(default_factory=list)
    review_message: str = ""


@dataclass
class PdfPageRecord:
    page_number: int
    source_image_path: str
    file_name: str = ""
    translated_image_path: str = ""
    status: str = "pending"
    attempts: int = 0
    error: str = ""
    emergency_ratio_normalized: bool = False
    # 模型只支持固定尺寸时，输出比例注定和源页不同，这一页按源页比例补白边
    # 后再入册。这是该模型的已知代价，不是异常，不进 needs_review。
    ratio_letterboxed: bool = False
    # 内容质检的疑点（near-blank / 分辨率过低）。页面照常进产物，文件标 needs_review。
    quality_flags: list[str] = field(default_factory=list)
    quality_message: str = ""
    # 实际渲染 DPI；低于 PDF_RENDER_DPI_DEFAULT 表示这一页因为像素上限被降级。
    render_dpi: float = 0.0
    placeholder: bool = False
    failure_ordinal: str = ""
    source_width_px: int = 0
    source_height_px: int = 0
    output_width_px: int = 0
    output_height_px: int = 0
    page_width_pt: float = 0.0
    page_height_pt: float = 0.0
    # 「跳过大幅面页」命中的页面：从未提交给翻译模型，装订时从源 PDF 矢量直传。
    # UI 需要靠这个字段和别的终态区分开，单独给张「未翻译，原样保留」的角标。
    skipped_oversize: bool = False
    review_enabled: bool = False
    review_status: str = "skipped"
    review_attempts: int = 0
    review_issues: list[dict[str, Any]] = field(default_factory=list)
    review_minor_suggestions: list[str] = field(default_factory=list)
    candidate_artifacts: list[dict[str, Any]] = field(default_factory=list)
    final_candidate_attempt: int = 0
    # 单页重生成留下的「上一版」译文页图（同目录改名保留，见
    # PDF_PAGE_IMAGE_PREVIOUS_PREFIX）。空串＝这一页没有上一版，界面不给换回入口。
    previous_image_path: str = ""
    # 上一版那一次的页面结论（状态/质检/审核/输出尺寸，字段清单见
    # _PAGE_VERSION_FIELDS）。只留图不留结论的话，一页从失败占位页换回好译文之后，
    # 记录仍然写着「占位页」，报告、计数和界面会继续把这一页当坏页。
    # 两个字段都带默认值：更早的 task_history 记录里没有它们，读回来是空，
    # 表现就是「这一页还没有上一版」，其余功能不受影响。
    # 注意：这个 dict 是进程内快照，写清单时会在 _summary_to_manifest 里剔掉，
    # 不进用户输出目录的 pdf_translation_manifest.json。
    previous_page_state: dict[str, Any] = field(default_factory=dict)


@dataclass
class PdfFileRecord:
    name: str
    source_path: str
    relative_path: str
    source_type: str = SOURCE_TYPE_PDF
    source_copy_path: str = ""
    translated_pdf_path: str = ""
    compressed_pdf_path: str = ""
    translated_image_path: str = ""
    translated_image_format: str = ""
    status: str = "pending"
    page_count: int = 0
    generated_page_count: int = 0
    placeholder_page_count: int = 0
    emergency_ratio_normalized_count: int = 0
    ratio_letterboxed_page_count: int = 0
    quality_flagged_page_count: int = 0
    render_downscaled_page_count: int = 0
    skipped_oversize_page_count: int = 0
    retry_count: int = 0
    review_enabled: bool = False
    reviewed_page_count: int = 0
    review_passed_page_count: int = 0
    review_repaired_page_count: int = 0
    review_failed_page_count: int = 0
    # 「译文有疑点但仍然采用」的页数：审核没通过、本地质检有标记、或走了应急比例归一化，
    # 而这一页没有退回失败占位页。口径跟 _record_needs_review 判 needs_review 的那半边对齐
    # （整份跳过大幅面另有自己的说法）。
    # 单独给这个数是因为另外几个计数两两都会重叠——审核没通过的页有一半是直接退回占位页的
    # （见 _finalize_page_failure），质检标记也留在退回前的最后一次尝试上——界面把它们相加
    # 就会把一页坏页说成两三页。这个数与 placeholder_page_count 严格互斥，可以直接相加。
    suspect_adopted_page_count: int = 0
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
    skipped_oversize_page_count: int = 0
    # 中止/失败的任务在任务中心只能靠这两个数说清楚「跑到哪了」：真正写出了译图的页数，
    # 和连开始都没轮到的页数。（提交了但没跑出来的页在 placeholder_page_count。）
    generated_page_count: int = 0
    unstarted_page_count: int = 0
    compressed_pdf_enabled: bool = False
    compressed_pdf_count: int = 0
    generated_image_count: int = 0
    compression_quality: int = PDF_COMPRESSED_JPEG_QUALITY_DEFAULT
    compression_max_long_edge_px: int = PDF_COMPRESSED_MAX_LONG_EDGE_PX
    review_enabled: bool = False
    reviewed_page_count: int = 0
    review_passed_page_count: int = 0
    review_repaired_page_count: int = 0
    review_failed_page_count: int = 0
    # 与 placeholder_page_count 严格互斥的「有疑点但仍然采用」页数，口径见 PdfFileRecord
    # 上的同名字段。任务中心那排指标格只能拿这个数跟占位页并列，拿 review_failed_page_count
    # 并列会把同一页在两个格子里各数一次。
    suspect_adopted_page_count: int = 0
    review_retry_count: int = 0
    review_minor_suggestion_count: int = 0
    rate_limit_reduction_count: int = 0
    partial_artifacts_available: bool = False
    image_model_signature: str = ""
    pdf_review_model_signature: str = ""
    stopped: bool = False
    stop_reason: str = ""
    files: list[PdfFileRecord] = field(default_factory=list)


@dataclass
class _PreparedPdfFile:
    item: PdfFileItem
    relative_pdf: Path
    record: PdfFileRecord
    source_pages_dir: Path
    translated_pages_dir: Path
    review_candidates_dir: Path
    translated_pdf_path: Path
    compressed_pdf_path: Path
    app_managed: bool
    final_output_dir: Path
    # 图片源的交付路径。PDF 变体在准备阶段就解析定了，图片源要等模型吐回来才知道
    # 后缀，所以这里留空、由第一次落盘时解析并缓存——之后每一轮重生成都原子覆盖
    # 同一个文件名，不再重复触发 ``resolve_translated_image_path`` 的改名副作用。
    translated_image_path: Path | None = None
