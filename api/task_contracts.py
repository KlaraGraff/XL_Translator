"""Types shared by the task manager and the surface definitions.

``api.surfaces`` describes each translation page; ``api.task_manager`` runs
the tasks.  Both need the runner protocol, the task errors and the start
options, so they live here; ``api.task_manager`` re-exports all of them.
"""

from __future__ import annotations

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
    """Raised when a source path has no usable files for the selected surface."""

    def __init__(self, message: str, *, reason: str = "invalid_input") -> None:
        super().__init__(message)
        self.reason = reason


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
    cad_keep_work_dxf: bool = True
    cad_copy_related_files: bool = False
    cad_verify_roundtrip: bool = True
    cad_scan_replacement_chars: bool = True
    cad_include_block_text: bool = True
    cad_glossary_path: str | None = None
    cad_use_memory: bool = True
    cad_check_entity_counts: bool = True
    cad_scan_residual: bool = True
    cad_translate_output_filename: bool = False

    @property
    def xls_conversion_mode(self) -> str:
        return "compatibility" if self.allow_xls_fallback else "high_fidelity"

    @property
    def doc_conversion_mode(self) -> str:
        return "compatibility" if self.allow_doc_fallback else "high_fidelity"
