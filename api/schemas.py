"""Request bodies accepted by the sidecar API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator

from api.surfaces import DOCUMENT_SURFACE_KEYS, SURFACE_KEYS
from core.path_utils import normalize_user_path, normalize_user_paths


class ApiKeyPayload(BaseModel):
    api_key: str = Field(default="", max_length=16_384)
    base_url: str = Field(default="", max_length=2_048)


class ScanRequest(BaseModel):
    path: str = Field(min_length=1)
    # The file-based pages registered in api/surfaces/.
    surface: Literal[DOCUMENT_SURFACE_KEYS]  # type: ignore[valid-type]
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

    @field_validator("path", "preferred_resume_dir", mode="before")
    @classmethod
    def _normalize_path(cls, value: Any) -> Any:
        return normalize_user_path(value)

    @field_validator("paths", mode="before")
    @classmethod
    def _normalize_paths(cls, value: Any) -> Any:
        return normalize_user_paths(value)


class CadScanRequest(BaseModel):
    paths: list[str] = Field(min_length=1)
    source_language: str | None = None
    target_language: str = "zh"
    include_block_text: bool = True

    @field_validator("paths", mode="before")
    @classmethod
    def _normalize_paths(cls, value: Any) -> Any:
        return normalize_user_paths(value)


class PdfPageActionRequest(BaseModel):
    file: str = Field(min_length=1, max_length=1_024)
    page: int = Field(ge=1)


class TaskStartRequest(BaseModel):
    source_path: str = ""
    # Every surface registered in api/surfaces/.
    surface: Literal[SURFACE_KEYS]  # type: ignore[valid-type]
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
    lang_pair: str | None = None
    confirmation_token: str | None = None
    # 「接着上次继续」：上一次任务的输出目录。runner 只读取它里面的既有译文，
    # 绝不往里写；本次输出仍然新建目录。
    resume_output_dir: str | None = None
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


class TmEntryPayload(BaseModel):
    source_text: str = Field(min_length=1)
    target_text: str = Field(min_length=1)
    lang_pair: str = Field(min_length=3)
    sync_reverse: bool = False


class TmEntryUpdatePayload(BaseModel):
    source_text: str = Field(min_length=1)
    target_text: str = Field(min_length=1)
    sync_reverse: bool = False


class TmPinPayload(BaseModel):
    pinned: bool = True


class TmBulkPinPayload(TmPinPayload):
    ids: list[int] = Field(min_length=1)


class TmBulkDeletePayload(BaseModel):
    ids: list[int] = Field(min_length=1)


class TmImportPayload(BaseModel):
    lang_pair: str = Field(min_length=3)
    mode: Literal["skip", "overwrite", "keep_both"] = "skip"
    entries: list[dict[str, Any]]
    sync_reverse: bool = False


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
    connection_mode: Literal["cloud", "local", "follow"] | None = None
    source_role: str | None = None


class ConnectionReorderPayload(BaseModel):
    ordered_ids: list[str]


class ModelRoleTestPayload(BaseModel):
    role: str


class DomainSettingsPayload(BaseModel):
    preset: str = "同步工程场景"
    custom_prompt: str = ""
    # 新形态统一保存为「预设名 → {"_base": Prompt}」；仍接受旧的
    # 语言映射和扁平字符串，写入时折叠为一个主 Prompt。
    prompt_overrides: dict[str, dict[str, str] | str] = Field(default_factory=dict)
    name_overrides: dict[str, str] = Field(default_factory=dict)
    # Named user prompts also use one canonical ``_base`` value.  Language maps
    # and plain strings remain accepted for backwards compatibility.
    custom_prompts: dict[str, dict[str, str] | str] = Field(default_factory=dict)
    disabled_presets: list[str] = Field(default_factory=list)


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


class MaintenanceClearRequest(BaseModel):
    category: Literal["task_history", "logs", "diagnostics", "keys", "settings", "tm", "workspaces"]
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
    images: list[FeedbackRequestImage] = Field(default_factory=list, max_length=20)

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
