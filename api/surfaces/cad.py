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
from core.cad_translation import (
    CadPipelineError,
    CadPipelineOptions,
    SubprocessCadConverter,
    scan_cad_paths,
)
from core.task_runner import user_facing_reason
from core.engine_dispatcher import build_role_engine
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
)


class CadSurface(Surface):
    key = "cad"
    label = "CAD drawing translation"
    page_key = "cad_translate"
    uses_translation_memory = True
    scan_honours_selection = True
    # CAD pauses at the current-file boundary: the task shows 正在暂停 until
    # the runner acknowledges that boundary.
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
        status = probe_status()
        converter_path = str(status.get("converter") or "").strip()
        converter = SubprocessCadConverter(converter_path) if converter_path else None
        scan_options = CadPipelineOptions(include_block_text=options.cad_include_block_text)
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

        summaries = []
        for candidate in scan_inputs:
            if under(candidate, output_root):
                continue
            try:
                summaries.extend(scan_cad_paths([candidate], converter=converter, options=scan_options))
            except Exception as exc:  # one malformed drawing must not abort the batch preflight
                logger.warning("CAD 预检跳过文件 {}：{}", candidate, exc)
        return [
            type("CadFile", (), {"path": summary.path, "name": summary.filename, "format": summary.format})()
            for summary in summaries
        ]

    def scan_response(self, request: Any, roots: list[Path]) -> dict[str, Any] | None:
        """CAD lists text and candidate counts and the converter status instead
        of the shared Excel/Word/PDF scan shape."""
        status = probe_status()
        converter_path = str(status.get("converter") or "").strip()
        converter = SubprocessCadConverter(converter_path) if converter_path else None
        cad_skipped: list[dict[str, str]] = []
        try:
            summaries = scan_cad_paths(
                roots,
                converter=converter,
                options=CadPipelineOptions(
                    source_lang=str(request.source_language or "auto"),
                    target_lang=str(request.target_language or "zh"),
                    include_block_text=bool(request.include_block_text),
                ),
                skipped=cad_skipped,
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
                "file_id": file_progress_id(item.path),
            }
            for item in summaries
        ]
        return {
            "items": items,
            "skipped": cad_skipped,
            # Keep the capability at the response root for the CAD view;
            # ``risk.capability`` remains for older scan consumers.
            "capability": status,
            "summary": {
                "file_count": len(items),
                "text_entity_count": sum(item["text_count"] for item in items),
                "candidate_count": sum(item["candidate_count"] for item in items),
            },
            "risk": {
                "capability": status,
                "message": "还没有连接图纸转换工具：DXF 可以直接翻译，DWG 需要先在「插件管理」里连接转换工具。"
                if not converter
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
                "翻译 DWG 图纸需要先在「插件管理」里连接图纸转换工具。",
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
            raise TaskInputError("翻译 DWG 图纸需要先在「插件管理」里连接图纸转换工具。", reason="cad_converter_missing")
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
        from core.engine_dispatcher import get_system_prompt, translate_texts
        system_prompt = get_system_prompt(settings, target_lang=settings.target_lang, source_lang=source_lang, page_key="cad")

        def translate_batch(
            texts: list[str],
            glossary: dict[str, str],
            progress: Callable[[int, int], None] | None = None,
        ) -> dict[str, str]:
            prompt = system_prompt
            if glossary:
                prompt += "\n固定术语（必须遵守）：\n" + "\n".join(f"{k} = {v}" for k, v in glossary.items())
            return translate_texts(
                texts,
                engine,
                settings.target_lang,
                prompt,
                max(1, int(settings.engine.batch_size)),
                max(1, int(settings.engine.concurrency)),
                # CAD preflight may keep mixed English/French text under
                # automatic detection.  Passing ``zh`` here silently
                # changes the model instruction and can produce a wrong
                # translation; the engine accepts ``auto`` and should
                # receive the user's actual selection.
                source_lang=source_lang,
                api_scheduler=api_schedulers.get("translation"),
                # 逐批回调：CAD 进度条在「AI 翻译」阶段跟着批次走，而不是停住再跳。
                progress_callback=progress,
            )

        source_pair = str(source_lang or "").strip()
        memory_pairs = (
            [f"{source_pair}-{settings.target_lang}"]
            if source_pair and source_pair != "auto"
            else [f"{candidate}-{settings.target_lang}" for candidate in ("en", "fr", "zh")]
        )

        def memory_lookup(texts: list[str]) -> dict[str, str | None]:
            hits: dict[str, str | None] = {text: None for text in texts}
            remaining = list(texts)
            for pair in memory_pairs:
                if not remaining:
                    break
                current = tm_manager.lookup_batch(remaining, pair)
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
            ),
            resume_output_dir=options.resume_output_dir,
            translate_output_filename=options.cad_translate_output_filename,
            translation_identity={
                "model": getattr(settings.engine, "cloud_model", ""),
                "provider": getattr(settings.engine, "cloud_provider", ""),
                "target_lang": settings.target_lang,
                "source_lang": source_lang,
            },
        )


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
        raise TaskInputError(f"CAD 术语库文件「{candidate.name}」读不出来，请检查格式（每行「原文,译文」或 JSON 对象）。") from exc
