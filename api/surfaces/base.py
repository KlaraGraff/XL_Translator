"""What a translation surface (a page under 翻译 in the sidebar) tells the task manager.

The task manager used to know every page by name: ``if surface == "excel" …
elif surface == "word" … elif surface == "cad"`` in a dozen places, so adding
CAD meant hunting down each branch.  Now each page is one ``Surface``
subclass in ``api/surfaces/<key>.py``, registered in ``api/surfaces/__init__.py``,
and the task manager only asks it questions.  See docs/ADDING_A_TRANSLATION_PAGE.md.

Everything here has a default, so a new surface overrides only what differs.
The three methods without a usable default are ``scan``,
``validate_preflight`` and ``build_runner``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Literal

from api.task_contracts import (
    REASON_TRANSLATION_MODEL_MISSING,
    Runner,
    TaskInputError,
    TaskOptions,
)
from core.api_config_check import check_translation_api_config
from settings import AppSettings


#: Text in the three interface languages, ``{"zh": …, "en": …, "fr": …}``.
#: Descriptors carry text this way because a page registered at run time has
#: no entry in the front end's translation catalog.
LocalizedText = Mapping[str, str]

PHASE_READ: LocalizedText = {"zh": "读取文件", "en": "Reading files", "fr": "Lecture des fichiers"}
PHASE_TRANSLATE: LocalizedText = {"zh": "翻译", "en": "Translation", "fr": "Traduction"}
PHASE_WRITE: LocalizedText = {"zh": "生成文件", "en": "Generating files", "fr": "Génération des fichiers"}


@dataclass(frozen=True)
class OptionDef:
    """One switch a page shows under 运行设置, declared instead of hand-built.

    The four built-in pages still draw their switches from the front end's own
    tables and declare none; a page registered at run time has no such table,
    so the front end draws whatever its descriptor lists.
    """

    key: str
    label: LocalizedText
    default: bool = False
    help: LocalizedText = field(default_factory=dict)
    #: Only ``"toggle"`` for now.
    kind: Literal["toggle"] = "toggle"

    def describe(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "kind": self.kind,
            "label": dict(self.label),
            "help": dict(self.help),
            "default": self.default,
        }


def suffixes(values: Iterable[str]) -> tuple[str, ...]:
    """File suffixes as descriptors list them: lower case, no dot, sorted."""
    return tuple(sorted({str(value).lower().lstrip(".") for value in values if str(value).strip(".")}))


@dataclass(frozen=True)
class RunnerRequest:
    """Everything the task manager hands a surface to build its runner."""

    files: list[Any]
    settings: AppSettings
    source_root: Path
    options: TaskOptions
    source_lang: str
    key_overrides: dict[str, str]
    api_schedulers: dict[str, Any] = field(default_factory=dict)
    connection_chains: dict[str, tuple[str, ...]] = field(default_factory=dict)


class Surface:
    """One translation page as the backend sees it."""

    # ── identity ─────────────────────────────────────────────────────────
    #: Value of ``surface`` in API requests and task records.
    key: ClassVar[str]
    #: English label for the scheduler and task history ("Excel translation").
    label: ClassVar[str]
    #: Model page key passed to ``task_api_context_for_page``; it decides
    #: which model roles (translation / image / pdf_review / cleaner) the task uses.
    page_key: ClassVar[str]
    #: Shipped with the app.  The front end draws built-in pages from its own
    #: compiled tables and uses the descriptor only for the others.
    builtin: ClassVar[bool] = True

    # ── descriptor (``GET /api/surfaces``) ───────────────────────────────
    #: Name in the sidebar and the task center.
    display_label: ClassVar[LocalizedText] = {}
    #: What the page calls its files (「表格文件」, "documents").
    file_noun: ClassVar[LocalizedText] = {}
    #: Icon name from ``ui/src/icons.ts``.
    icon: ClassVar[str] = ""
    #: File suffixes the page translates (see ``suffixes``).
    file_extensions: ClassVar[tuple[str, ...]] = ()
    #: The page can write 原文 + 译文 / only 译文.
    output_bilingual: ClassVar[bool] = False
    output_translation_only: ClassVar[bool] = False
    #: Name of each runner phase, in the order and number of ``phase_weights``.
    phase_names: ClassVar[tuple[LocalizedText, ...]] = ()
    #: Switches under 运行设置 the front end draws from the descriptor.
    options: ClassVar[tuple[OptionDef, ...]] = ()
    #: Shown in the sidebar until the user switches it off on the 扩展 page
    #: (``TranslationTypesSettings.shows``).  None follows the key: built-in
    #: pages show, ``ext:`` types do not.  A format adapter sets it from its
    #: manifest when shipped with the app.
    visible_by_default: ClassVar[bool | None] = None

    # ── task start ───────────────────────────────────────────────────────
    #: False only for TM cleaning, which runs on a language pair, not on files.
    requires_source: ClassVar[bool] = True
    #: Open the TM database before the task starts.
    uses_translation_memory: ClassVar[bool] = False
    #: Default core rules for a run-time type (the manifest's ``prompt``); empty
    #: means the shared ``CORE_TRANSLATION_PROMPT``.  Built-in pages cannot
    #: change their core rules and keep this empty.
    default_prompt: ClassVar[str] = ""
    #: Where the target language lives in settings: ``"shared"`` (the global
    #: ``target_lang``), ``"per_surface"`` (``<key>_target_lang`` is copied
    #: into ``target_lang`` when the request names none) or ``"pdf"``
    #: (``settings.pdf.target_lang``).
    target_lang_setting: ClassVar[Literal["shared", "per_surface", "pdf"]] = "shared"
    #: Record ``<key>_domain_preset`` in the task snapshot.
    domain_preset_in_snapshot: ClassVar[bool] = False
    #: ``scan`` receives the user's ticked files and scans only those.
    scan_honours_selection: ClassVar[bool] = False

    # ── running task ─────────────────────────────────────────────────────
    #: Share of the progress bar per runner phase, in ``phase_index`` order.
    #: Empty means equal shares.  Runners that report ``phase_total <= 0``
    #: (CAD) give the overall percentage directly and ignore this.
    phase_weights: ClassVar[tuple[float, ...]] = ()
    #: The page offers 暂停 (the runner must define ``pause()``).
    can_pause: ClassVar[bool] = False
    #: ``"immediate"`` – the task shows *paused* as soon as pause is pressed;
    #: ``"file_boundary"`` – it shows *pausing* until the runner acknowledges
    #: a safe point (CAD: between drawings, or between two request batches
    #: inside one drawing).
    pause_mode: ClassVar[Literal["immediate", "file_boundary"]] = "immediate"
    #: The page offers 继续 after a pause (runner defines ``resume()``).
    can_resume: ClassVar[bool] = False
    #: The page offers 结束暂停任务 (runner defines ``end_paused()``).
    can_end_paused: ClassVar[bool] = False
    #: The finished task keeps its runner so per-page review and reruns keep
    #: working (PDF).  Other runners are released when the task ends.
    keeps_runner_after_finish: ClassVar[bool] = False

    # ── hooks ────────────────────────────────────────────────────────────

    def describe(self) -> dict[str, Any]:
        """The type descriptor: what a front end, CLI or agent needs to show
        and start this page without knowing it at build time."""
        return {
            "key": self.key,
            "builtin": self.builtin,
            "label": dict(self.display_label),
            "file_noun": dict(self.file_noun),
            "icon": self.icon,
            "extensions": list(self.file_extensions),
            "capabilities": {
                "requires_source": self.requires_source,
                "can_pause": self.can_pause,
                "pause_mode": self.pause_mode,
                "can_resume": self.can_resume,
                "can_end_paused": self.can_end_paused,
                "bilingual": self.output_bilingual,
                "translation_only": self.output_translation_only,
            },
            "phases": [dict(name) for name in self.phase_names],
            "options": [option.describe() for option in self.options],
        }

    def saved_request_defaults(self, settings: AppSettings, *, target_lang: str) -> dict[str, Any]:
        """Start-request fields the page's workspace fills in on its own.

        The window does not leave every ``TaskStartRequest`` field at its
        schema default: some come from saved settings, some from switch
        defaults that live only in the window (``buildPayload`` in
        ui/src/views/workspace.ts).  A caller without that window – the
        command line, an MCP client – sends ``apply_saved_defaults`` and gets
        these values for every field it did not set, so the same file comes
        out the way it would from the app.  ``target_lang`` is the task's
        effective target language.

        By default: the descriptor's switch defaults under ``options``, which
        is what the window sends for a page registered at run time.
        """
        if not self.options:
            return {}
        return {"options": {option.key: option.default for option in self.options}}

    def saved_scan_defaults(
        self, settings: AppSettings, *, source_lang: str, target_lang: str
    ) -> dict[str, Any]:
        """``ScanRequest`` fields the workspace fills in on its own (``scanPayload``).

        Same contract as ``saved_request_defaults``, for ``/api/sources/scan``.
        """
        return {}

    def scan(
        self,
        root: Path,
        options: TaskOptions,
        *,
        selected_paths: set[str] | None = None,
    ) -> list[Any]:
        """Files under ``root`` this surface can translate."""
        raise NotImplementedError

    def scan_sources(self, root: Path, request: Any) -> Any:
        """Scan for the page's file list (``/api/sources/scan``).

        Returns a scan result with ``items``, ``skipped``, ``summary`` and
        ``risk``; ``request`` is the ``ScanRequest``.  Results for several
        roots are merged by the route.
        """
        raise NotImplementedError

    def scan_response(self, request: Any, roots: list[Path]) -> dict[str, Any] | None:
        """A complete ``/api/sources/scan`` response, for a page whose list
        does not fit the shared shape (CAD).  ``None`` uses ``scan_sources``."""
        return None

    def validate_preflight(
        self, *, files: list[Any], settings: AppSettings, options: TaskOptions
    ) -> None:
        """Raise ``TaskInputError`` for anything that would fail the task anyway."""
        raise NotImplementedError

    def snapshot(
        self, *, settings: AppSettings, files: list[Any], options: TaskOptions
    ) -> dict[str, object]:
        """Surface-specific fields frozen into the task snapshot."""
        return {"selected_file_count": len(files)}

    def fingerprint_extras(
        self, *, files: list[Any], options: TaskOptions
    ) -> dict[str, object]:
        """Extra inputs that make two otherwise identical starts different tasks."""
        return {}

    def owner_label(self, snapshot: dict[str, object]) -> str:
        """How the scheduler names a running task of this surface."""
        return self.label

    def build_runner(self, request: RunnerRequest) -> Runner:
        raise NotImplementedError


def require_target_lang(value: str | None, page_name: str) -> None:
    if not str(value or "").strip():
        raise TaskInputError(f"请先选择 {page_name} 目标语言。")


def require_translation_model(settings: AppSettings) -> None:
    """The text translation model must be usable before a task may start."""
    model_check = check_translation_api_config(settings)
    if not model_check.ok:
        detail = f"（{model_check.detail}）" if model_check.detail else ""
        raise TaskInputError(
            f"{model_check.message}{detail}", reason=REASON_TRANSLATION_MODEL_MISSING
        )


def file_format(item: Any) -> str:
    """Normalized extension of a scanned item without assuming its class.

    Each scanner returns its own item type, and task-manager tests hand in
    bare objects, so absent metadata counts as "no format" rather than a crash.
    """
    explicit = str(getattr(item, "format", "") or "").strip().lower()
    if explicit:
        return explicit.lstrip(".")
    path = getattr(item, "path", None)
    return Path(path).suffix.lower().lstrip(".") if path else ""
