"""A translation page made from a format adapter's manifest (``ext:<id>``).

Every format adapter gets the same page: its descriptor comes from the
manifest (``core/format_adapter.py``), its files are found by extension and
counted by the adapter's ``scan``, and its tasks run on the shared executor
(``core/adapter_runner.py``).  Nothing here knows any particular format.

``register_bundled_adapters()`` registers the adapters shipped with the app
(``adapters/<id>/``) when the API starts; adapters installed from outside
come in a later phase with a child-process host.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from loguru import logger

from api.surfaces import all_surfaces, register_surface
from api.surfaces.base import (
    PHASE_READ,
    PHASE_TRANSLATE,
    PHASE_WRITE,
    OptionDef,
    RunnerRequest,
    Surface,
    require_target_lang,
    require_translation_model,
    suffixes,
)
from api.task_contracts import Runner, TaskInputError, TaskOptions
from core.format_adapter import (
    BILINGUAL_OPTION_KEY,
    BUNDLED_ADAPTERS_DIR,
    AdapterError,
    AdapterHost,
    AdapterManifest,
    InProcessAdapterHost,
    discover_adapters,
)
from core.language_registry import get_target_lang_display
from core.surface_settings import EXTENSION_PAGE_KEY, extension_settings
from settings import AppSettings

#: Where an adapter came from, shown on the 扩展 page.
AdapterOrigin = Literal["bundled"]

#: Added by the app to every type that writes both outputs, so the switch reads
#: the same on every page; ``core/adapter_runner.py`` maps it to the write mode.
BILINGUAL_OPTION = OptionDef(
    BILINGUAL_OPTION_KEY,
    {"zh": "双语对照", "en": "Bilingual", "fr": "Bilingue"},
    default=False,
    help={
        "zh": "开启后在原文之后附上译文；关闭时只保留译文。",
        "en": "When on, each source passage is followed by its translation; when off, only the translation is kept.",
        "fr": "Activé, chaque passage source est suivi de sa traduction ; désactivé, seule la traduction est conservée.",
    },
)


@dataclass
class AdapterFileItem:
    path: Path
    name: str
    size_kb: float
    relative_path: str = ""
    format: str = ""
    #: Units the adapter's ``scan`` counted; ``None`` before counting (task start).
    unit_count: int | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class AdapterSkippedItem:
    path: Path
    name: str
    relative_path: str
    reason: str
    format: str = ""


@dataclass
class AdapterScanResult:
    root: Path
    items: list[AdapterFileItem] = field(default_factory=list)
    skipped: list[AdapterSkippedItem] = field(default_factory=list)

    @property
    def summary(self) -> dict[str, int]:
        return {
            "scanned_count": len(self.items) + len(self.skipped),
            "selected_count": len(self.items),
            "skipped_count": len(self.skipped),
            "unit_count": sum(item.unit_count or 0 for item in self.items),
        }

    @property
    def risk(self) -> dict[str, object]:
        return {}


def _is_output_or_hidden(part: str) -> bool:
    return part.startswith(".") or part.endswith("_翻译输出") or "_翻译输出_" in part


def list_files(root: Path, extensions: tuple[str, ...]) -> list[tuple[Path, str]]:
    """``(path, relative_path)`` of every file under ``root`` with one of ``extensions``.

    Earlier translation output folders and hidden folders are left out, so a
    second run never picks up the first run's results.
    """
    root = Path(root)
    wanted = {f".{suffix}" for suffix in extensions}
    if root.is_file():
        return [(root, root.name)] if root.suffix.lower() in wanted else []
    if not root.is_dir():
        return []
    found: list[tuple[Path, str]] = []
    for directory, subdirectories, filenames in os.walk(root):
        subdirectories[:] = sorted(name for name in subdirectories if not _is_output_or_hidden(name))
        for filename in sorted(filenames):
            path = Path(directory) / filename
            if filename.startswith(".") or path.suffix.lower() not in wanted:
                continue
            found.append((path, str(path.relative_to(root))))
    return found


def _size_kb(path: Path) -> float:
    try:
        return round(path.stat().st_size / 1024, 1)
    except OSError:
        return 0.0


class AdapterSurface(Surface):
    """The page of one format adapter; every field comes from its manifest."""

    builtin = False
    page_key = EXTENSION_PAGE_KEY
    target_lang_setting = "per_surface"
    uses_translation_memory = True
    domain_preset_in_snapshot = True
    phase_names = (PHASE_READ, PHASE_TRANSLATE, PHASE_WRITE)
    phase_weights = (10, 80, 10)
    # Pauses between files: the file in progress finishes first.
    can_pause = True
    pause_mode = "immediate"
    can_resume = True

    def __init__(self, manifest: AdapterManifest, host: AdapterHost, *, origin: AdapterOrigin = "bundled") -> None:
        self.manifest = manifest
        self.host = host
        self.origin = origin
        self.key = manifest.key
        self.label = f"{manifest.label['en']} translation"
        self.display_label = dict(manifest.label)
        self.file_noun = dict(manifest.file_noun)
        self.icon = manifest.icon
        self.file_extensions = suffixes(manifest.extensions)
        self.output_bilingual = manifest.capabilities.bilingual
        self.output_translation_only = manifest.capabilities.translation_only
        both = self.output_bilingual and self.output_translation_only
        self.options = ((BILINGUAL_OPTION,) if both else ()) + tuple(
            OptionDef(option.key, dict(option.label), default=option.default, help=dict(option.help))
            for option in manifest.options
        )

    # ── descriptor ───────────────────────────────────────────────────────

    def describe(self) -> dict[str, Any]:
        descriptor = super().describe()
        descriptor["adapter"] = {
            "id": self.manifest.id,
            "version": self.manifest.version,
            "origin": self.origin,
            "license": self.manifest.license,
            "repo": self.manifest.repo,
        }
        return descriptor

    def resolve_options(self, settings: AppSettings | None, options: TaskOptions | None) -> dict[str, bool]:
        """Switch values for a task: descriptor default ← saved on the page ← the request."""
        saved = dict(extension_settings(settings, self.key).options) if settings is not None else {}
        requested = dict((options.options if options is not None else None) or {})
        resolved: dict[str, bool] = {}
        for option in self.options:
            value: Any = option.default
            if option.key in saved:
                value = saved[option.key]
            if option.key in requested:
                value = requested[option.key]
            resolved[option.key] = bool(value)
        return resolved

    # ── scan ─────────────────────────────────────────────────────────────

    def _items(self, root: Path) -> list[AdapterFileItem]:
        return [
            AdapterFileItem(
                path=path,
                name=path.name,
                size_kb=_size_kb(path),
                relative_path=relative,
                format=path.suffix.lower().lstrip("."),
            )
            for path, relative in list_files(root, self.file_extensions)
        ]

    def scan(
        self,
        root: Path,
        options: TaskOptions,
        *,
        selected_paths: set[str] | None = None,
    ) -> list[Any]:
        # Starting a task only lists the files; the runner reads each one.
        return list(self._items(root))

    def scan_sources(self, root: Path, request: Any) -> AdapterScanResult:
        result = AdapterScanResult(root=Path(root))
        for item in self._items(root):
            try:
                scanned = self.host.scan(item.path)
            except AdapterError as exc:
                logger.warning(f"[{self.key}] 无法读取 {item.name}：{exc}")
                result.skipped.append(
                    AdapterSkippedItem(
                        path=item.path,
                        name=item.name,
                        relative_path=item.relative_path,
                        reason=f"无法读取：{exc}",
                        format=item.format,
                    )
                )
                continue
            item.unit_count = scanned.unit_count
            item.warnings = list(scanned.warnings)
            result.items.append(item)
        return result

    # ── task start ───────────────────────────────────────────────────────

    def validate_preflight(self, *, files: list[Any], settings: AppSettings, options: TaskOptions) -> None:
        if not files:
            raise TaskInputError(f"没有可翻译的{self.file_noun.get('zh', '文件')}。")
        require_target_lang(settings.target_lang, self.display_label.get("zh", self.key))
        require_translation_model(settings)

    def snapshot(self, *, settings: AppSettings, files: list[Any], options: TaskOptions) -> dict[str, object]:
        return {
            "selected_file_count": len(files),
            "adapter": {"id": self.manifest.id, "version": self.manifest.version, "origin": self.origin},
            "options": self.resolve_options(settings, options),
            "target_lang_display": get_target_lang_display(settings.target_lang),
        }

    def fingerprint_extras(self, *, files: list[Any], options: TaskOptions) -> dict[str, object]:
        identities = []
        for item in files:
            path = Path(getattr(item, "path", ""))
            try:
                stat = path.stat()
                identities.append([str(path.resolve()), stat.st_size, stat.st_mtime_ns])
            except OSError:
                identities.append([str(path), None, None])
        return {"files": identities, "options": dict(options.options or {})}

    def owner_label(self, snapshot: dict[str, object]) -> str:
        return f"{self.label} → {get_target_lang_display(str(snapshot.get('target_lang') or ''))}"

    def build_runner(self, request: RunnerRequest) -> Runner:
        raise TaskInputError("此翻译类型的执行器尚未接入。")


def register_bundled_adapters(directory: str | Path | None = None) -> list[str]:
    """Register every adapter under ``directory`` (default: the shipped ``adapters/``).

    Safe to call more than once: types already registered are kept.  A broken
    adapter is logged and skipped, never stops the app from starting.
    Returns the keys registered by this call.
    """
    root = BUNDLED_ADAPTERS_DIR if directory is None else Path(directory)
    found, problems = discover_adapters(root)
    for problem in problems:
        logger.warning(f"[扩展] 跳过随应用提供的适配器：{problem}")
    registered = {surface.key for surface in all_surfaces()}
    added: list[str] = []
    for adapter_dir, manifest in found:
        if manifest.key in registered:
            continue
        try:
            register_surface(AdapterSurface(manifest, InProcessAdapterHost(adapter_dir, manifest)))
        except ValueError as exc:
            logger.warning(f"[扩展] 无法注册 {manifest.key}：{exc}")
            continue
        registered.add(manifest.key)
        added.append(manifest.key)
    if added:
        logger.info(f"[扩展] 已注册随应用提供的翻译类型：{', '.join(added)}")
    return added


__all__ = [
    "BILINGUAL_OPTION",
    "AdapterFileItem",
    "AdapterScanResult",
    "AdapterSkippedItem",
    "AdapterSurface",
    "list_files",
    "register_bundled_adapters",
]
