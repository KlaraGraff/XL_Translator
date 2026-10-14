"""CAD drawing translation (.dwg via the ODA converter, .dxf directly)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from loguru import logger

from api.cad_plugin import probe_status
from api.surfaces.base import (
    RunnerRequest,
    Surface,
    require_target_lang,
    require_translation_model,
    suffixes,
)
from api.task_contracts import Runner, TaskInputError, TaskOptions
from core import bilingual_writer
from core.cad_task_runner import CadTaskRunner
from core.cad_translation import (
    CAD_OUTPUT_VERSIONS,
    CAD_SUPPORTED_EXTENSIONS,
    CadPipelineError,
    CadPipelineOptions,
    SubprocessCadConverter,
    list_cad_files,
    parse_lock_patterns,
    scan_cad_paths,
)
from core.cad_cache import default_cad_cache
from core.task_runner import user_facing_reason
from core.text_translation_service import TextTranslationService
from core.file_progress import file_progress_id
from core.language_registry import get_target_lang_display
from settings import AppSettings

_CAD_OPTION_FIELDS = (
    "cad_use_terminology",
    "cad_keep_work_dxf",
    "cad_copy_related_files",
    "cad_verify_roundtrip",
    "cad_scan_replacement_chars",
    "cad_include_block_text",
    "cad_glossary_path",
    "cad_use_memory",
    "cad_check_entity_counts",
    "cad_scan_residual",
    "cad_translate_output_filename",
    "cad_fit_layout",
    "cad_fit_default_scale",
    "cad_fit_min_scale",
    "cad_display_mode",
    "cad_locked_blocks",
    "cad_locked_layers",
    "cad_translation_font",
    "cad_output_version",
)


def cad_fit_settings(options: TaskOptions, target_lang: str) -> Any:
    """任务的字号比例：界面按目标语言传了就用传来的，没传的那项取目标语言对应的默认值。"""
    from core.cad_layout import FitSettings, default_fit_settings

    default = default_fit_settings(target_lang)
    return FitSettings(
        default.default_scale if options.cad_fit_default_scale is None else options.cad_fit_default_scale,
        default.min_scale if options.cad_fit_min_scale is None else options.cad_fit_min_scale,
    ).normalized()


def cad_layout_options(options: TaskOptions, target_lang: str = "") -> dict[str, Any]:
    """任务选项里排版、显示模式、锁定名单和译文字体对应的 ``CadPipelineOptions`` 参数。"""
    fit = cad_fit_settings(options, target_lang)
    display_mode = options.cad_display_mode
    if display_mode not in {"translation", "source_first", "translation_first"}:
        display_mode = "translation"
    return {
        "fit_layout": bool(options.cad_fit_layout),
        "fit_default_scale": fit.default_scale,
        "fit_min_scale": fit.min_scale,
        "display_mode": display_mode,
        "locked_blocks": parse_lock_patterns(options.cad_locked_blocks),
        "locked_layers": parse_lock_patterns(options.cad_locked_layers),
        "translation_font": "original" if options.cad_translation_font == "original" else "compatible",
        "output_version": options.cad_output_version if options.cad_output_version in CAD_OUTPUT_VERSIONS else "source",
    }


class CadSurface(Surface):
    key = "cad"
    label = "CAD drawing translation"
    page_key = "cad_translate"
    uses_translation_memory = True
    scan_honours_selection = True
    display_label = {"zh": "CAD 图纸", "en": "CAD drawings", "fr": "Plans CAO"}
    file_noun = {"zh": "DWG / DXF 图纸", "en": "DWG / DXF drawings", "fr": "plans DWG / DXF"}
    icon = "cad"
    file_extensions = suffixes(CAD_SUPPORTED_EXTENSIONS)
    # 显示模式：仅译文 / 原文在上 / 译文在上。CAD 的进度按整体百分比给（没有 phase_weights），
    # 所以也不列阶段名。
    output_bilingual = True
    output_translation_only = True
    # CAD pauses between drawings, or inside one drawing between two batches
    # of translation requests: the task shows 正在暂停 until the runner
    # acknowledges that it has stopped sending new requests.
    can_pause = True
    pause_mode = "file_boundary"
    can_resume = True

    def scan(
        self,
        root: Path,
        options: TaskOptions,
        *,
        selected_paths: set[str] | None = None,
    ) -> list[Any]:
        # Starting a task only lists the drawings: nothing is opened or
        # converted here.  The runner reads (and for DWG converts) each drawing
        # itself, with run-log lines and progress; doing it here as well made
        # the start button hang for as long as converting every DWG took.
        output_root = Path(options.cad_output_dir).expanduser().resolve() if options.cad_output_dir else None

        def under(path: Path, parent: Path | None) -> bool:
            if parent is None:
                return False
            try:
                path.resolve().relative_to(parent)
                return True
            except ValueError:
                return False

        if selected_paths:
            scan_inputs = [Path(path) for path in sorted(selected_paths)]
        elif root.is_dir():
            scan_inputs = [
                candidate
                for candidate in sorted(root.rglob("*"))
                if candidate.is_file()
                and candidate.suffix.lower() in {".dwg", ".dxf"}
                and not under(candidate, output_root)
                and not any(
                    part in {"CAD翻译输出", ".cad-work", ".cad-review"}
                    or (part.endswith("_翻译输出") or "_翻译输出_" in part)
                    for part in candidate.relative_to(root).parts
                )
            ]
        else:
            scan_inputs = [root]

        listed = [
            path
            for path in list_cad_files(candidate for candidate in scan_inputs if not under(candidate, output_root))
            if not under(path, output_root)
        ]
        return [
            type("CadFile", (), {"path": path, "name": path.name, "format": path.suffix.lower().lstrip(".")})()
            for path in listed
        ]

    def scan_response(self, request: Any, roots: list[Path]) -> dict[str, Any] | None:
        """CAD lists text and candidate counts and the converter status instead
        of the shared Excel/Word/PDF scan shape.

        DXF drawings are counted here.  DWG drawings are only listed: their
        counts are ``None`` with ``counts_pending`` set and are counted by the
        task, which converts them with progress and run-log lines.
        """
        status = probe_status()
        converter_path = str(status.get("converter") or "").strip()
        cad_skipped: list[dict[str, str]] = []
        try:
            summaries = scan_cad_paths(
                roots,
                converter_available=bool(converter_path),
                options=CadPipelineOptions(
                    source_lang=str(request.source_language or "auto"),
                    target_lang=str(request.target_language or "zh"),
                    include_block_text=bool(request.include_block_text),
                    locked_blocks=parse_lock_patterns(getattr(request, "locked_blocks", "")),
                    locked_layers=parse_lock_patterns(getattr(request, "locked_layers", "")),
                ),
                skipped=cad_skipped,
                # 已经逐张统计过（转换过）的 DWG 直接从缓存里数，重扫时不用再等一遍。
                cache=default_cad_cache(),
            )
        except CadPipelineError as exc:
            raise HTTPException(422, str(exc) or "图纸扫描失败。") from exc
        except Exception as exc:
            logger.opt(exception=exc).warning(f"[CAD] 图纸扫描失败：{exc!r}")
            raise HTTPException(
                422, user_facing_reason(exc, fallback="图纸扫描失败，出现了未预期的问题。")
            ) from exc
        items = [
            {
                "path": str(item.path),
                "name": item.filename,
                "format": item.format,
                "text_count": item.text_entity_count,
                "candidate_count": item.candidate_count,
                "needs_conversion": item.needs_conversion,
                "uncertain_count": item.uncertain_count,
                "counts_pending": item.counts_pending,
                "size": _file_size(item.path),
                "file_id": file_progress_id(item.path),
            }
            for item in summaries
        ]
        known = [item for item in items if not item["counts_pending"]]
        return {
            "items": items,
            "skipped": cad_skipped,
            # Keep the capability at the response root for the CAD view;
            # ``risk.capability`` remains for older scan consumers.
            "capability": status,
            "summary": {
                "file_count": len(items),
                # Only the drawings counted here; DWG drawings are counted by the task.
                "text_entity_count": sum(item["text_count"] for item in known),
                "candidate_count": sum(item["candidate_count"] for item in known),
                "counts_pending_file_count": len(items) - len(known),
            },
            "risk": {
                "capability": status,
                "message": "尚未连接图纸转换工具：DXF 可直接翻译，DWG 需先在「插件」中连接转换工具。"
                if not converter_path
                else "",
            },
            "previous_output": None,
        }

    def validate_preflight(
        self, *, files: list[Any], settings: AppSettings, options: TaskOptions
    ) -> None:
        if not files:
            raise TaskInputError("请至少选择一个 DWG 或 DXF 文件。")
        status = probe_status()
        has_dwg = any(Path(getattr(item, "path", "")).suffix.lower() == ".dwg" for item in files)
        if has_dwg and not bool(status.get("enabled")):
            raise TaskInputError(
                "翻译 DWG 图纸前，请先在「插件」中连接图纸转换工具。",
                reason="cad_capability_missing",
            )
        require_target_lang(getattr(settings, "target_lang", ""), "CAD")
        require_translation_model(settings)

    def snapshot(
        self, *, settings: AppSettings, files: list[Any], options: TaskOptions
    ) -> dict[str, object]:
        glossary_signature = ""
        if options.cad_glossary_path:
            glossary_path = Path(options.cad_glossary_path).expanduser()
            try:
                glossary_signature = hashlib.sha256(glossary_path.read_bytes()).hexdigest()
            except OSError:
                glossary_signature = "missing"
        fit = cad_fit_settings(options, settings.target_lang)
        return {
            "selected_file_count": len(files),
            "cad_output_dir": options.cad_output_dir or "",
            "cad_checks": {
                "verify_roundtrip": options.cad_verify_roundtrip,
                "scan_replacement_chars": options.cad_scan_replacement_chars,
                "check_entity_counts": options.cad_check_entity_counts,
                "scan_residual": options.cad_scan_residual,
            },
            "cad_options": {
                "use_terminology": options.cad_use_terminology,
                "keep_work_dxf": options.cad_keep_work_dxf,
                "copy_related_files": options.cad_copy_related_files,
                "include_block_text": options.cad_include_block_text,
                "glossary_path": options.cad_glossary_path or "",
                "glossary_sha256": glossary_signature,
                "fit_layout": options.cad_fit_layout,
                "fit_default_scale": fit.default_scale,
                "fit_min_scale": fit.min_scale,
                "display_mode": options.cad_display_mode,
                "locked_blocks": options.cad_locked_blocks,
                "locked_layers": options.cad_locked_layers,
                "translation_font": options.cad_translation_font,
                "output_version": options.cad_output_version,
            },
            "tm": {"enabled": options.cad_use_memory, "priority": "user_memory_first"},
            "target_lang_display": get_target_lang_display(settings.target_lang),
        }

    def fingerprint_extras(self, *, files: list[Any], options: TaskOptions) -> dict[str, object]:
        # A drawing edited between preflight and start is a different task even
        # at the same path, so hash the content, not just the name.
        cad_files = []
        for item in files:
            path = Path(getattr(item, "path", ""))
            try:
                stat = path.stat()
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                cad_files.append({"path": str(path.resolve()), "sha256": digest, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
            except OSError:
                cad_files.append({"path": str(path.resolve()), "missing": True})
        return {
            "cad_content_identity": cad_files,
            "cad_options": {key: getattr(options, key) for key in _CAD_OPTION_FIELDS},
        }

    def owner_label(self, snapshot: dict[str, object]) -> str:
        return f"{self.label} → {get_target_lang_display(str(snapshot.get('target_lang') or ''))}"

    def build_runner(self, request: RunnerRequest) -> Runner:
        files = request.files
        settings = request.settings
        options = request.options
        source_lang = request.source_lang
        api_schedulers = request.api_schedulers
        chains = request.connection_chains
        status = probe_status()
        converter_path = str(status.get("converter") or "").strip()
        has_dwg = any(Path(getattr(item, "path", "")).suffix.lower() == ".dwg" for item in files)
        if has_dwg and not converter_path:
            raise TaskInputError("翻译 DWG 图纸前，请先在「插件」中连接图纸转换工具。", reason="cad_converter_missing")
        # All document surfaces publish into one unique, timestamped
        # task root.  CAD used to keep a permanent ``CAD翻译输出`` folder
        # with nested translated/review/run directories, which made a
        # second run overwrite or mix with the first one.  Reuse the
        # shared builder used by Excel, Word and PDF instead.
        cad_output = getattr(settings, "cad_output", None)
        configured_output_dir = options.cad_output_dir
        if not configured_output_dir and cad_output is not None and getattr(cad_output, "use_custom_output_dir", False):
            configured_output_dir = str(getattr(cad_output, "custom_output_dir", "") or "").strip() or None
        output_dir = bilingual_writer.build_output_dir(request.source_root, configured_output_dir)
        # 引擎、提示词、分批请求和记忆库的接线在 core/text_translation_service.py，与 PDF「原位
        # 替换」共用。速率与重试按「翻译参数 → CAD」（core/surface_rate.py）。
        service = TextTranslationService(
            settings,
            surface_key="cad",
            rate_surface="cad",
            target_lang=settings.target_lang,
            # CAD preflight may keep mixed English/French text under
            # automatic detection.  Passing ``zh`` here silently changes the
            # model instruction and can produce a wrong translation; the
            # engine accepts ``auto`` and should receive the user's actual
            # selection.
            source_lang=source_lang,
            connection_ids=tuple(chains.get("translation") or ()),
            api_scheduler=api_schedulers.get("translation"),
            marked_in="cad_review",
        )
        # 开始任务时就建好引擎：连接设置有问题时任务直接报错，不等到第一张图纸。
        engine = service.engine()
        smart_mode = service.smart_mode

        def profile_requester(
            texts: list[str], log: Callable[[str, str], None], *, neighbor_rows: list[str] = ()
        ) -> str:
            return service.document_profile(texts, log=log, neighbor_rows=neighbor_rows)

        def translate_batch(
            texts: list[str],
            glossary: dict[str, str],
            progress: Callable[[int, int], None] | None = None,
            should_stop: Callable[[], bool] | None = None,
            error_callback: Callable[[str], None] | None = None,
            request_callback: Callable[[Any], None] | None = None,
            context: str = "",
            term_session: Any = None,
        ) -> dict[str, str]:
            # 接口放弃、停止时没发出去的条目不在结果里：管线记为「未获得译文」，图纸里保留
            # 原文，结果标为需复核（不再当成「已翻译」计数）。
            return service.translate(
                texts,
                glossary=glossary,
                context=context,
                term_session=term_session,
                progress=progress,
                should_stop=should_stop,
                error_callback=error_callback,
                request_callback=request_callback,
            )

        def translate_batch_with_sources(
            texts: list[str],
            glossary: dict[str, str],
            progress: Callable[[int, int], None] | None = None,
            should_stop: Callable[[], bool] | None = None,
            error_callback: Callable[[str], None] | None = None,
            context: str = "",
            term_session: Any = None,
        ) -> dict[str, Any]:
            """程序判断不了语种的文字：翻译的同时让模型回报原文语种（与 Excel/Word 自动识别相同的协议）。"""
            return service.translate_with_sources(
                texts,
                glossary=glossary,
                context=context,
                term_session=term_session,
                progress=progress,
                should_stop=should_stop,
                error_callback=error_callback,
            )

        # 记忆库与 Excel / Word 同一套规则：整段复用（手动维护的优先）、句中采用设定译法、
        # 按备注场景选用、核对后重译、自动保存短词（core.term_session）。
        term_memory = service.term_memory() if options.cad_use_memory else None

        return CadTaskRunner(
            files,
            source_root=request.source_root,
            output_dir=output_dir,
            converter=SubprocessCadConverter(converter_path) if converter_path else None,
            memory_lookup=service.memory_lookup if options.cad_use_memory else None,
            term_memory=term_memory,
            translator=translate_batch,
            language_translator=translate_batch_with_sources,
            profile_requester=profile_requester if smart_mode else None,
            # 几张图纸的图纸说明提前同时请求，沿用翻译的并发设置；本地模型一次一个。
            profile_concurrency=service.rate.concurrency if service.rate.mode != "local" else 1,
            filename_translator=engine,
            glossary=load_cad_glossary(options.cad_glossary_path) if options.cad_use_terminology else {},
            options=CadPipelineOptions(
                source_lang=source_lang,
                target_lang=settings.target_lang,
                skip_target_language=options.untranslated_only,
                keep_work_dxf=options.cad_keep_work_dxf,
                include_block_text=options.cad_include_block_text,
                verify_roundtrip=options.cad_verify_roundtrip,
                scan_replacement_chars=options.cad_scan_replacement_chars,
                copy_related_files=options.cad_copy_related_files,
                check_entity_counts=options.cad_check_entity_counts,
                scan_residual=options.cad_scan_residual,
                **cad_layout_options(options, settings.target_lang),
            ),
            # 扫描页逐张统计时转换好的 DWG 存在这里，任务直接复用，不再转一遍。
            dxf_cache=default_cad_cache(),
            resume_output_dir=options.resume_output_dir,
            translate_output_filename=options.cad_translate_output_filename,
            translation_identity={
                "model": getattr(settings.engine, "cloud_model", ""),
                "provider": getattr(settings.engine, "cloud_provider", ""),
                "target_lang": settings.target_lang,
                "source_lang": source_lang,
            },
        )


def _file_size(path: Path) -> int | None:
    try:
        return Path(path).stat().st_size
    except OSError:
        return None


def load_cad_glossary(path: str | None) -> dict[str, str]:
    """Load an optional simple JSON/CSV terminology map."""
    if not str(path or "").strip():
        return {}
    candidate = Path(str(path)).expanduser()
    if not candidate.is_file():
        raise TaskInputError(f"找不到 CAD 术语库文件「{candidate.name}」，它可能已被移动或删除。")
    try:
        if candidate.suffix.lower() == ".json":
            value = json.loads(candidate.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("JSON 术语库必须是原文到译文的对象。")
            return {
                str(key): str(item)
                for key, item in value.items()
                if str(key).strip() and str(item).strip()
            }
        glossary: dict[str, str] = {}
        for row in candidate.read_text(encoding="utf-8-sig").splitlines():
            if not row.strip() or row.lstrip().startswith("#"):
                continue
            source, separator, target = row.partition("\t")
            if not separator:
                source, separator, target = row.partition(",")
            if separator and source.strip() and target.strip():
                glossary[source.strip()] = target.strip()
        return glossary
    except (OSError, UnicodeError, ValueError) as exc:
        raise TaskInputError(f"无法读取 CAD 术语库文件「{candidate.name}」，请检查格式（每行「原文,译文」或 JSON 对象）。") from exc
