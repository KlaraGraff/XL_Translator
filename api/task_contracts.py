"""Types shared by the task manager and the surface definitions.

``api.surfaces`` describes each translation page; ``api.task_manager`` runs
the tasks.  Both need the runner protocol, the task errors and the start
options, so they live here; ``api.task_manager`` re-exports all of them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol


class Runner(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...

    def needs_poll(self) -> bool: ...

    def get_message(self, timeout: float = 0.05) -> Any: ...


class TaskNotFoundError(KeyError):
    """Raised when a task ID does not belong to this sidecar."""


class TaskConflictError(RuntimeError):
    """Raised when the scheduler cannot admit a candidate task."""

    def __init__(self, message: str, *, reason: str = "conflict") -> None:
        super().__init__(message)
        self.reason = reason


class TaskInputError(ValueError):
    """Raised when a source path has no usable files for the selected surface.

    ``accept`` marks a stop the user may step past (the window shows it with
    「仍要继续」): the request fields that, sent again, say "yes, continue".
    A caller without the window – the command line, MCP – reads them from the
    error body instead of matching the sentence.
    """

    def __init__(
        self,
        message: str,
        *,
        reason: str = "invalid_input",
        accept: Mapping[str, bool] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.accept = dict(accept) if accept else None


#: ``reason`` of a stop the user may step past by confirming (see ``TaskInputError.accept``).
REASON_NEEDS_CONFIRMATION = "needs_confirmation"


# 模型还没配好时的拦截原因（随错误响应的 reason 一起给界面）。界面认出这几个就在
# 弹窗里放「去设置」，直接带到「设置 › 模型服务」里对应的角色，而不是只有「知道了」。
REASON_TRANSLATION_MODEL_MISSING = "translation_model_missing"
REASON_IMAGE_MODEL_MISSING = "image_model_missing"
REASON_PDF_REVIEW_MODEL_MISSING = "pdf_review_model_missing"


@dataclass(frozen=True)
class TaskOptions:
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
    # 「接着上次继续」时上一次任务的输出目录；runner 只读它，不往里写。
    resume_output_dir: str | None = None
    # CAD-only task settings. These are deliberately separate from the
    # document output suffix settings: CAD always creates a sibling output
    # file/directory and never exposes a filename-suffix option.
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
    # 运行时注册的类型（ext:<id>）自己的开关，键见它的描述符 options；内置页面不用。
    options: Mapping[str, bool | int | float | str] | None = None

    @property
    def xls_conversion_mode(self) -> str:
        return "compatibility" if self.allow_xls_fallback else "high_fidelity"

    @property
    def doc_conversion_mode(self) -> str:
        return "compatibility" if self.allow_doc_fallback else "high_fidelity"
