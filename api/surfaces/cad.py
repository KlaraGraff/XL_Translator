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
)
from api.task_contracts import Runner, TaskInputError, TaskOptions
from core import bilingual_writer, tm_manager
from core.cad_task_runner import CadTaskRunner
from core.document_profile import (
    append_profile,
    is_context_sensitive,
    request_document_profile,
)
from core.cad_translation import (
    CadPipelineError,
    CadPipelineOptions,
    SubprocessCadConverter,
    list_cad_files,
    parse_lock_patterns,
    scan_cad_paths,
)
from core.cad_cache import default_cad_cache
from core.task_runner import user_facing_reason
from core.engine_dispatcher import TranslationBatchRunStats, build_role_engine
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
)


def cad_layout_options(options: TaskOptions) -> dict[str, Any]:
    """任务选项里排版、显示模式、锁定名单和译文字体对应的 ``CadPipelineOptions`` 参数。"""
    from core.cad_layout import FitSettings

    fit = FitSettings(options.cad_fit_default_scale, options.cad_fit_min_scale).normalized()
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
    }


class CadSurface(Surface):
    key = "cad"
    label = "CAD drawing translation"
    page_key = "cad_translate"
    uses_translation_memory = True
    scan_honours_selection = True
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
                "fit_default_scale": options.cad_fit_default_scale,
                "fit_min_scale": options.cad_fit_min_scale,
                "display_mode": options.cad_display_mode,
                "locked_blocks": options.cad_locked_blocks,
                "locked_layers": options.cad_locked_layers,
                "translation_font": options.cad_translation_font,
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
        engine = build_role_engine(
            settings,
            "translation",
            connection_ids=tuple(chains.get("translation") or ()),
        )
        from core.engine_dispatcher import (
            get_system_prompt,
            smart_context_enabled,
            translate_texts,
            translate_texts_with_sources,
        )
        system_prompt = get_system_prompt(settings, target_lang=settings.target_lang, source_lang=source_lang, page_key="cad")
        smart_mode = smart_context_enabled(settings, "cad")

        def profile_requester(
            texts: list[str], log: Callable[[str, str], None], *, neighbor_rows: list[str] = ()
        ) -> str:
            return request_document_profile(
                engine.chat,
                texts,
                target_language=get_target_lang_display(settings.target_lang),
                log=log,
                neighbor_rows=neighbor_rows,
            )

        def translate_batch(
            texts: list[str],
            glossary: dict[str, str],
            progress: Callable[[int, int], None] | None = None,
            should_stop: Callable[[], bool] | None = None,
            error_callback: Callable[[str], None] | None = None,
            request_callback: Callable[[Any], None] | None = None,
            context: str = "",
        ) -> dict[str, str]:
            prompt = append_profile(system_prompt, context)
            if glossary:
                prompt += "\n固定术语（必须遵守）：\n" + "\n".join(f"{k} = {v}" for k, v in glossary.items())
            concurrency = max(1, int(settings.engine.concurrency))
            stats = TranslationBatchRunStats()
            results = translate_texts(
                texts,
                engine,
                settings.target_lang,
                prompt,
                max(1, int(settings.engine.batch_size)),
                concurrency,
                # CAD preflight may keep mixed English/French text under
                # automatic detection.  Passing ``zh`` here silently
                # changes the model instruction and can produce a wrong
                # translation; the engine accepts ``auto`` and should
                # receive the user's actual selection.
                source_lang=source_lang,
                api_scheduler=api_schedulers.get("translation"),
                # 逐批回调：CAD 进度条在「AI 翻译」阶段跟着批次走，而不是停住再跳。
                progress_callback=progress,
                # 停止在批次之间生效，不必等整张图纸的请求都发完。
                should_stop=should_stop,
                # 拆小重试、整批放弃、限流降速这些情况写进运行日志（与 Excel 一致）。
                error_callback=error_callback,
                # 每批请求的发出与返回：写进运行日志。
                request_callback=request_callback,
                stats=stats,
                # 接口临时故障时同一批的重发次数（翻译参数 → CAD → 失败重试次数）。
                transient_retries=settings.request_retry.cad,
            )
            # 接口放弃的批次、停止时没发出去的批次，translate_texts 按原文占位交回。
            # 那不是译文：以前管线把它们当成「已翻译」计数，日志和报告都说全翻好了。
            # 摘掉之后管线记为「未获得译文」，图纸里保留原文，结果标为需复核。
            for text in stats.untranslated_sources | stats.stopped_sources:
                if results.get(text) == text:
                    results.pop(text, None)
            return results

        def translate_batch_with_sources(
            texts: list[str],
            glossary: dict[str, str],
            progress: Callable[[int, int], None] | None = None,
            should_stop: Callable[[], bool] | None = None,
            error_callback: Callable[[str], None] | None = None,
            context: str = "",
        ) -> dict[str, Any]:
            """程序判断不了语种的文字：翻译的同时让模型回报原文语种（与 Excel/Word 自动识别相同的协议）。"""
            prompt = append_profile(system_prompt, context)
            if glossary:
                prompt += "\n固定术语（必须遵守）：\n" + "\n".join(f"{k} = {v}" for k, v in glossary.items())
            stats = TranslationBatchRunStats()
            results = translate_texts_with_sources(
                texts,
                engine,
                settings.target_lang,
                prompt,
                max(1, int(settings.engine.batch_size)),
                max(1, int(settings.engine.concurrency)),
                progress,
                error_callback,
                should_stop=should_stop,
                api_scheduler=api_schedulers.get("translation"),
                stats=stats,
                transient_retries=settings.request_retry.cad,
            )
            # 接口放弃或停止时没发出去的条目按原文占位交回，那不是译文，也不是语种判断。
            failed = stats.untranslated_sources | stats.stopped_sources
            return {text: (item.translation, item.source_lang) for text, item in results.items() if text not in failed}

        source_pair = str(source_lang or "").strip()
        target_base = str(settings.target_lang or "").split("-", 1)[0].lower()
        memory_pairs = (
            [f"{source_pair}-{settings.target_lang}"]
            if source_pair and source_pair != "auto"
            # 原文和译文同一种语言的组合查不出东西（英译英），不必去查。
            else [f"{candidate}-{settings.target_lang}" for candidate in ("en", "fr", "zh") if candidate != target_base]
        )

        def memory_lookup(texts: list[str]) -> dict[str, str | None]:
            hits: dict[str, str | None] = {text: None for text in texts}
            remaining = list(texts)
            for pair in memory_pairs:
                if not remaining:
                    break
                current = tm_manager.lookup_batch(remaining, pair)
                if smart_mode:
                    # 「自动识别场景」：「甲」「FM」这类短词只认用户定下的记忆，自动存下的可能来自别的场景。
                    short = [text for text in remaining if is_context_sensitive(text)]
                    if short:
                        current.update(tm_manager.lookup_batch(short, pair, locked_only=True))
                for text, value in current.items():
                    if value and hits.get(text) in (None, ""):
                        hits[text] = value
                remaining = [text for text in remaining if not hits.get(text)]
            return hits

        return CadTaskRunner(
            files,
            source_root=request.source_root,
            output_dir=output_dir,
            converter=SubprocessCadConverter(converter_path) if converter_path else None,
            memory_lookup=memory_lookup if options.cad_use_memory else None,
            translator=translate_batch,
            language_translator=translate_batch_with_sources,
            profile_requester=profile_requester if smart_mode else None,
            # 几张图纸的图纸说明提前同时请求，沿用翻译的并发设置；本地模型一次一个。
            profile_concurrency=(
                max(1, int(settings.engine.concurrency)) if settings.engine.mode != "local" else 1
            ),
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
                **cad_layout_options(options),
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
