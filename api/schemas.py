"""Request bodies accepted by the sidecar API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator

from api.surfaces import all_surfaces
from core.path_utils import normalize_user_path, normalize_user_paths


def _registered_surface(value: Any, *, file_based: bool) -> str:
    """A ``surface`` value must name a registered page exactly.

    The registry grows at run time (``register_surface``), so the accepted
    values are looked up per request instead of frozen into a ``Literal``.
    """
    keys = [surface.key for surface in all_surfaces() if surface.requires_source or not file_based]
    if not isinstance(value, str) or value not in keys:
        raise ValueError(f"surface must be one of {keys}")
    return value


class ApiKeyPayload(BaseModel):
    api_key: str = Field(default="", max_length=16_384)
    base_url: str = Field(default="", max_length=2_048)


class ClassifySourcesRequest(BaseModel):
    """「翻译」入口页：拖入或选中的路径，按扩展名数一数再决定进哪个类型页。"""
    paths: list[str] = Field(min_length=1, max_length=500)


class ScanRequest(BaseModel):
    path: str = Field(min_length=1)
    # The file-based pages registered in api/surfaces/.
    surface: str
    include_images: bool = False
    # 「浏览 → 选择文件」允许一次挑多个文件（只限类型，不限数量）。给了 paths 就按这
    # 几个路径各扫一次再合并；不给就还是按 path 扫一个文件或一整个文件夹。
    paths: list[str] = Field(default_factory=list)
    # 续译弹窗里用户从历史下拉换了一个输出目录后重扫时带上；平时为空，
    # 检测自动选最新的一个。
    preferred_resume_dir: str | None = None
    # 仅 CAD 使用：候选文字数取决于语言对（已是目标语言的文字不算候选），
    # 所以扫描要带上当前选择的语言，而不是写死 auto→zh。
    source_language: str | None = None
    target_language: str | None = None
    include_block_text: bool = True
    # 锁定不翻译的图块/图层：统计数不含这些文字，和翻译时的口径一致。
    locked_blocks: str = ""
    locked_layers: str = ""
    # 命令行和 MCP 用：没传的字段按界面会填的值补（Surface.saved_scan_defaults）。界面自己不传。
    apply_saved_defaults: bool = False

    @field_validator("path", "preferred_resume_dir", mode="before")
    @classmethod
    def _normalize_path(cls, value: Any) -> Any:
        return normalize_user_path(value)

    @field_validator("surface", mode="before")
    @classmethod
    def _known_surface(cls, value: Any) -> str:
        return _registered_surface(value, file_based=True)

    @field_validator("paths", mode="before")
    @classmethod
    def _normalize_paths(cls, value: Any) -> Any:
        return normalize_user_paths(value)


class CadScanRequest(BaseModel):
    paths: list[str] = Field(min_length=1)
    source_language: str | None = None
    target_language: str = "zh"
    include_block_text: bool = True
    locked_blocks: str = ""
    locked_layers: str = ""

    @field_validator("paths", mode="before")
    @classmethod
    def _normalize_paths(cls, value: Any) -> Any:
        return normalize_user_paths(value)


class CadReviewEditsRequest(BaseModel):
    """译文对照表：一张图纸里改过的译文 ``{原文: 新译文}``，清空即改回原译文。"""

    file: str = Field(min_length=1)
    edits: dict[str, str] = Field(default_factory=dict)


class CadReviewImportRequest(BaseModel):
    """导回改好的 Excel：base64，不带 data: 前缀。"""

    data: str = Field(min_length=1)


class CadReviewRewriteRequest(BaseModel):
    """按对照表重新写回的图纸（对照表里每张图纸的 key）。"""

    files: list[str] = Field(default_factory=list)


class CadInspectRequest(BaseModel):
    """逐张统计 / 扫描图块名：一次只开一张图（DWG 会转换并缓存给翻译任务用）。"""

    path: str = Field(min_length=1)
    source_language: str | None = None
    target_language: str = "zh"
    include_block_text: bool = True
    locked_blocks: str = ""
    locked_layers: str = ""

    @field_validator("path", mode="before")
    @classmethod
    def _normalize_path(cls, value: Any) -> Any:
        return normalize_user_path(value)


class PdfPageActionRequest(BaseModel):
    file: str = Field(min_length=1, max_length=1_024)
    page: int = Field(ge=1)


class TaskStartRequest(BaseModel):
    source_path: str = ""
    # Every surface registered in api/surfaces/.
    surface: str
    selected_paths: list[str] = Field(default_factory=list)
    untranslated_only: bool = False
    protect_front_matter: bool = False
    translate_headers_footers: bool = False
    allow_xls_fallback: bool = False
    allow_doc_fallback: bool = False
    include_images: bool = False
    source_lang: str | None = None
    target_lang: str | None = None
    allow_known_review_failure: bool = False
    # 审核模型不能识别图片时「本次不审核，直接开始」（见 TaskOptions）。
    allow_skip_pdf_review: bool = False
    lang_pair: str | None = None
    confirmation_token: str | None = None
    # 「接着上次继续」：上一次任务的输出目录。runner 只读取它里面的既有译文，
    # 绝不往里写；本次输出仍然新建目录。
    resume_output_dir: str | None = None
    # 同一批任务共用的输出文件夹，只由 POST /api/tasks/batch 填写（见 TaskOptions）。
    batch_output_dir: str | None = None
    cad_output_dir: str | None = None
    cad_use_terminology: bool = True
    cad_keep_work_dxf: bool = False
    cad_copy_related_files: bool = False
    cad_verify_roundtrip: bool = True
    cad_scan_replacement_chars: bool = True
    cad_include_block_text: bool = True
    cad_glossary_path: str | None = None
    cad_use_memory: bool = True
    cad_check_entity_counts: bool = True
    cad_scan_residual: bool = True
    cad_translate_output_filename: bool = False
    # 译文排版、显示模式、锁定名单、译文字体（对应设置里的 cad_output.*）。
    cad_fit_layout: bool = True
    # 不传时按目标语言取默认（core.cad_layout.default_fit_settings：中文 1.0/0.4，其他 0.7/0.4）。
    cad_fit_default_scale: float | None = None
    cad_fit_min_scale: float | None = None
    cad_display_mode: str = "translation"
    cad_locked_blocks: str = ""
    cad_locked_layers: str = ""
    cad_translation_font: str = "compatible"
    # DWG 译文图纸的版本：source 跟随原图，或 2018/2013/2010/2007/2004/2000。
    cad_output_version: str = "source"
    # 运行时注册的类型（ext:<id>）在「运行设置」里的开关，键见它的描述符 options。
    # 内置四个页面的开关仍是上面这些具名字段，不走这里。
    options: dict[str, bool | int | float | str] | None = None
    # 命令行和 MCP 用：没传的字段按界面会填的值补（已存的设置和页面开关的默认值，
    # 见 Surface.saved_request_defaults），同一份文件翻出来和在应用里一样。界面自己不传。
    apply_saved_defaults: bool = False

    @field_validator("surface", mode="before")
    @classmethod
    def _known_surface(cls, value: Any) -> str:
        return _registered_surface(value, file_based=False)

    @field_validator(
        "source_path",
        "resume_output_dir",
        "cad_output_dir",
        "cad_glossary_path",
        mode="before",
    )
    @classmethod
    def _normalize_path(cls, value: Any) -> Any:
        return normalize_user_path(value)

    @field_validator("selected_paths", mode="before")
    @classmethod
    def _normalize_paths(cls, value: Any) -> Any:
        return normalize_user_paths(value)

    @model_validator(mode="after")
    def _require_source_path(self) -> "TaskStartRequest":
        # An empty path would resolve to the process cwd and trigger a
        # recursive scan there. Only tm_clean runs without a source path.
        if self.surface != "tm_clean" and not self.source_path.strip():
            raise ValueError("source_path is required for document tasks")
        return self



class BatchStartRequest(BaseModel):
    """「翻译」页的「全部开始」：每一项和 POST /api/tasks 的请求一样，各带各页的设置。"""

    items: list[TaskStartRequest] = Field(min_length=1, max_length=16)
    # 和这一批之外正在跑的任务共用连接时，用户已在弹窗里确认过。
    allow_shared: bool = False

class TmEntryPayload(BaseModel):
    source_text: str = Field(min_length=1)
    target_text: str = Field(min_length=1)
    lang_pair: str = Field(min_length=3)
    sync_reverse: bool = False
    # 备注（适用场景）、采用方式、生效范围。旧客户端不带时按通用词条、默认采用方式。
    note: str = Field(default="", max_length=200)
    usage: Literal["", "always", "scene", "segment"] = ""
    scope: str = Field(default="", max_length=100)


class TmEntryUpdatePayload(BaseModel):
    source_text: str = Field(min_length=1)
    target_text: str = Field(min_length=1)
    sync_reverse: bool = False
    # None 表示不改；记忆库页编辑固定词条时，用户确认过才带 allow_pinned=true。
    note: str | None = Field(default=None, max_length=200)
    usage: Literal["", "always", "scene", "segment"] | None = None
    scope: str | None = Field(default=None, max_length=100)
    allow_pinned: bool = False


class TmPinPayload(BaseModel):
    pinned: bool = True


class TmBulkPinPayload(TmPinPayload):
    ids: list[int] = Field(min_length=1)


class TmBulkDeletePayload(BaseModel):
    ids: list[int] = Field(min_length=1)
    allow_pinned: bool = False


class TmImportPayload(BaseModel):
    lang_pair: str = Field(min_length=3)
    mode: Literal["skip", "overwrite", "keep_both"] = "skip"
    entries: list[dict[str, Any]]
    sync_reverse: bool = False


class TmImportParsePayload(BaseModel):
    """用户自己的词表文件：base64，不带 data: 前缀。"""

    file_name: str = ""
    data: str = Field(min_length=1)
    lang_pair: str = ""


class TmFullImportPayload(BaseModel):
    format_version: Literal["tm-full-v1"]
    custom_target_langs: list[dict[str, Any]] = Field(default_factory=list)
    entries: list[dict[str, Any]] = Field(default_factory=list)
    conflict_candidates: list[dict[str, Any]] = Field(default_factory=list)
    mode: Literal["skip", "overwrite", "keep_both"] = "skip"
    code_map: dict[str, str] = Field(default_factory=dict)
    sync_reverse: bool = False


class DataHealthDismissPayload(BaseModel):
    """「知道了」随手带上的作用域清单：只清横幅上真正展示过的恢复事件。"""

    scopes: list[str] | None = None


class TmSuggestionPayload(BaseModel):
    entry_id: int
    source_text: str = ""
    old_target: str = ""
    new_target: str = Field(min_length=1)
    accepted: bool = True
    # 建议表主键与乐观并发版本：缺了它们，确认写入既无法把建议标成已处理，
    # 也等于关掉了版本校验——期间被别人改过的译文会被无声覆盖。
    # 旧客户端可能不带，服务端再按内容回查一次兜底。
    suggestion_id: int = 0
    lang_pair: str = ""
    expected_version: str = ""


class TmApplySuggestionsPayload(BaseModel):
    suggestions: list[TmSuggestionPayload]
    auto_pin: bool = False
    sync_reverse: bool = False


class CustomTargetLanguagePayload(BaseModel):
    name: str = Field(min_length=1, max_length=32)
    description: str = Field(default="", max_length=2_000)


class LanguagePreflightRequest(BaseModel):
    file_id: str = Field(min_length=1)
    texts: list[str] = Field(default_factory=list)
    target_lang: str = Field(min_length=2)
    detected_languages: list[str] | None = None


class TmCleanRequest(BaseModel):
    lang_pair: str = Field(min_length=3)
    confirmation_token: str | None = None


class ModelFetchRequest(BaseModel):
    provider: str = Field(min_length=1)
    base_url: str = ""
    api_key: str = ""
    refresh: bool = False


class ModelCatalogRefreshPayload(BaseModel):
    refresh: bool = False
    # 面板当前展示的连接；不给就取主连接。
    connection_id: str = ""


class ModelConnectivityPayload(BaseModel):
    """Which pool entry the panel wants tested; empty means the primary."""

    connection_id: str = ""


class ModelRoleUpdatePayload(BaseModel):
    source_role: str | None = None
    mode: Literal["cloud", "local"] | None = None
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_mode: str | None = None


class ConnectionUpsertPayload(BaseModel):
    label: str | None = None
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    api_mode: str | None = None
    thinking: Literal["auto", "off", "on"] | None = None
    connection_mode: Literal["cloud", "local", "follow"] | None = None
    source_role: str | None = None


class ConnectionReorderPayload(BaseModel):
    ordered_ids: list[str]


class ModelRoleTestPayload(BaseModel):
    role: str


class DomainSettingsPayload(BaseModel):
    # 「自动识别场景」开关。
    smart_context: bool = True
    # 选中的补充要求：「无」或 custom_prompts 里的一个名字。
    preset: str = "无"
    # 用户的补充要求，统一存成「名称 → {"_base": 正文}」；仍接受旧的语言映射和
    # 扁平字符串，写入时折叠成一份。
    custom_prompts: dict[str, dict[str, str] | str] = Field(default_factory=dict)


class DomainPromptOptimizePayload(BaseModel):
    preset: str
    target_lang: str = ""
    prompt: str = Field(min_length=1, max_length=20000)


class ThroughputPayload(BaseModel):
    batch_size: StrictInt | None = Field(default=None, ge=1, le=2**31 - 1)
    concurrency: StrictInt | None = Field(default=None, ge=1, le=2**31 - 1)


class ThroughputUnlockPayload(BaseModel):
    code: str


class UpdatePreferencesRequest(BaseModel):
    notifications_paused: bool | None = None
    ignored_release_version: str | None = Field(default=None, max_length=64)
    quick_start_completed: bool | None = None
    pdf_guide_seen: bool | None = None
    cad_guide_seen: bool | None = None


class MaintenanceClearRequest(BaseModel):
    # "workspaces"（「临时文件」）这个可清理类别已经整体删掉：从来没有代码往那个目录
    # 写过东西。旧界面或用户上一次打开、还没刷新的窗口仍可能带着它来请求，这里不再
    # 接受——FastAPI 会在进到处理函数之前就把它拒成 422 字段校验错误，而不是让它落到
    # 路由里的 if 链上悄悄做完一个空操作，也不会以 500 收场。
    category: Literal["task_history", "logs", "diagnostics", "keys", "settings", "tm"]
    confirmation: bool = False
    lang_pair: str | None = Field(default=None, max_length=128)


class FullResetRequest(BaseModel):
    confirmation: bool = False
    phrase: str = ""


class FeedbackRequestImage(BaseModel):
    # 描述框里粘贴的截图，base64（不带 data: 前缀）。单张上限在 core.feedback 里查。
    data: str = Field(default="", max_length=14_500_000)


class FeedbackRequest(BaseModel):
    category: Literal["problem", "suggestion", "other"] = "problem"
    # 长度上限在 core.feedback.validate_draft 里查，好给出中文提示而不是 422。
    description: str = Field(default="", max_length=20_000)
    contact: str = Field(default="", max_length=1_000)
    include_system: bool = True
    # 下面三项取自 task_ids 选中的任务：日志、原文件、译文（含压缩版 PDF）。
    include_logs: bool = True
    include_source_files: bool = False
    include_output_files: bool = False
    ui_language: str = Field(default="zh", max_length=16)
    task_ids: list[str] = Field(default_factory=list, max_length=20)
    file_paths: list[str] = Field(default_factory=list, max_length=50)
    # 截图不限张数，真正的上限是 core.feedback 里截图合计的大小；这里只挡离谱的请求。
    images: list[FeedbackRequestImage] = Field(default_factory=list, max_length=500)

    @field_validator("file_paths", mode="before")
    @classmethod
    def _normalize_paths(cls, value: Any) -> Any:
        return normalize_user_paths(value)


class FeedbackFilesRequest(BaseModel):
    paths: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("paths", mode="before")
    @classmethod
    def _normalize_paths(cls, value: Any) -> Any:
        return normalize_user_paths(value)
