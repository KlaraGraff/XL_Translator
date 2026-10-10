"""Task-manager runner for the optional CAD translation plugin."""

from __future__ import annotations

import queue
import hashlib
import json
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from core.cad_translation import (
    CadPipelineOptions,
    CadProgress,
    CadTranslationPipeline,
)
from core.file_progress import FileProgressReporter
from core.language_registry import get_target_lang_display
from core.output_name_translation import translate_names, output_stem_from_translation
from core.task_runner import DoneMsg, ErrorMsg, LogMsg, ProgressMsg, StatusMsg, StoppedMsg


_PHASES = {
    "scan": (1, "读取与扫描"),
    "match": (2, "匹配记忆/术语"),
    "translate": (3, "AI 翻译"),
    "write": (4, "写回 DWG"),
    "verify": (5, "重新读取与校验"),
}


def _target_filename_label(target_lang: str) -> str:
    """Return a stable, filesystem-safe target-language suffix."""
    label = get_target_lang_display(str(target_lang or "").strip()) or str(target_lang or "").strip()
    safe = "".join(char for char in label if char.isalnum() or char in {"_", "-", "."})
    return safe or "中文"


class CadTaskRunner:
    """Run one or more CAD files while exposing the shared runner protocol."""

    def __init__(
        self,
        files: list[Any],
        *,
        source_root: Path,
        output_dir: Path,
        converter: Any = None,
        memory_lookup: Any = None,
        translator: Any = None,
        filename_translator: Any = None,
        translate_output_filename: bool = False,
        glossary: Mapping[str, str] | None = None,
        options: CadPipelineOptions | None = None,
        resume_output_dir: Path | None = None,
        translation_identity: Mapping[str, Any] | None = None,
    ) -> None:
        self._files = files
        self._source_root = Path(source_root)
        # ``output_dir`` is already the unique task root selected by the
        # shared output-directory builder.  Keep the deliverable files at
        # that root, like Excel/Word/PDF; private CAD evidence is hidden
        # below it and never becomes a second visible output hierarchy.
        requested_output_dir = Path(output_dir)
        self._resume_output_dir = Path(resume_output_dir).expanduser() if resume_output_dir else None
        if self._resume_output_dir and requested_output_dir.expanduser().resolve() == self._resume_output_dir.resolve():
            # Keep the direct runner API safe for callers that reuse the old
            # output path.  The task manager normally passes a fresh shared
            # output root already, so this branch is legacy compatibility.
            base = requested_output_dir.parent / f"{requested_output_dir.name}_resume_{time.strftime('%Y%m%d_%H%M%S')}"
            candidate = base
            suffix = 2
            while candidate.exists():
                candidate = requested_output_dir.parent / f"{base.name}_{suffix}"
                suffix += 1
            self._output_dir = candidate
        else:
            self._output_dir = requested_output_dir
        self._run_id = f"run-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        self._run_dir = self._output_dir / ".cad-work" / self._run_id
        self._translated_dir = self._output_dir
        self._review_dir = self._output_dir / ".cad-review"
        self._publication_dir = self._output_dir
        self._converter = converter
        self._memory_lookup = memory_lookup
        self._translator = translator
        self._filename_translator = filename_translator
        self._translate_output_filename = bool(translate_output_filename)
        self._glossary = dict(glossary or {})
        self._options = options or CadPipelineOptions()
        self._translation_identity = dict(translation_identity or {})
        self._checkpoint_path = self._run_dir / ".cad-checkpoint.json"
        self._legacy_checkpoint_path = self._output_dir / ".cad-checkpoint.json"
        self._last_stage: str | None = None
        self._pause_acknowledged = False
        self._queue: queue.Queue[Any] = queue.Queue()
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._file_progress = FileProgressReporter(self._queue, files, self._source_root)
        self._file_progress.emit_many("waiting", "extract")

    def start(self) -> None:
        self._stop_event.clear()
        self._pause_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="cad-translation")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._pause_event.clear()

    def pause(self) -> None:
        self._pause_event.set()

    def resume(self) -> None:
        self._pause_event.clear()
        self._pause_acknowledged = False

    def end_paused(self) -> None:
        self.stop()

    def needs_poll(self) -> bool:
        return bool(self._thread and self._thread.is_alive()) or not self._queue.empty()

    def get_message(self, timeout: float = 0.05) -> Any:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def _progress(self, progress: CadProgress, *, file_path: Path | None = None) -> None:
        phase_index, phase_name = _PHASES.get(progress.stage, (1, progress.stage))
        # ProgressMsg is shared with the other surfaces.  Encode CAD batch
        # progress as completed local phase units so the task manager's normal
        # phase weighting remains untouched for Excel/Word/PDF.
        file_count = max(1, len(self._files))
        index = next((i for i, item in enumerate(self._files) if file_path is not None and Path(item.path) == Path(file_path)), 0)
        local_percent = max(0.0, min(100.0, float(progress.percent)))
        batch_done = int(round(index * 100 + local_percent))
        batch_total = file_count * 100
        if self._last_stage and self._last_stage != progress.stage:
            previous_index, previous_name = _PHASES.get(self._last_stage, (1, self._last_stage))
            self._queue.put(ProgressMsg(
                phase_index=previous_index,
                phase_total=5,
                phase_name=previous_name,
                step_done=batch_total,
                step_total=batch_total,
            ))
        self._last_stage = progress.stage
        self._queue.put(
            ProgressMsg(
                phase_index=phase_index,
                phase_total=5,
                phase_name=phase_name,
                step_done=batch_done,
                step_total=batch_total,
            )
        )
        self._queue.put(StatusMsg(phase_desc=f"状态：[阶段 {phase_index}/5] {progress.message or phase_name}"))
        if file_path is not None:
            self._file_progress.emit(
                file_path,
                "running",
                progress.stage,
                completed=max(0, int(progress.done)),
                total=max(1, int(progress.total)),
            )

    def _log(self, level: str, message: str) -> None:
        self._queue.put(LogMsg(level=level, message=message))

    def _source_hash(self, source: Path) -> str:
        digest = hashlib.sha256()
        with source.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _options_fingerprint(self) -> str:
        payload = {
            "options": {key: value for key, value in vars(self._options).items()},
            "glossary": self._glossary,
            "translation_identity": self._translation_identity,
            "translate_output_filename": self._translate_output_filename,
        }
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()

    def _load_checkpoint(self) -> dict[str, Any]:
        if not self._resume_output_dir:
            return {}
        candidates = [self._resume_output_dir / ".cad-checkpoint.json", self._resume_output_dir.parent / ".cad-checkpoint.json"]
        candidates.extend(self._resume_output_dir.rglob(".cad-checkpoint.json"))
        for path in candidates:
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    return value
            except (OSError, ValueError, TypeError):
                continue
        return {}

    def _write_checkpoint(self, entries: Mapping[str, Any]) -> None:
        self._checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"schema_version": 1, "options_fingerprint": self._options_fingerprint(), "files": entries}, ensure_ascii=False, indent=2)
        self._checkpoint_path.write_text(payload, encoding="utf-8")
        # Keep the historical root-level location readable by older resume
        # callers that pass translated/ or CAD翻译输出 as their source.
        self._legacy_checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        self._legacy_checkpoint_path.write_text(payload, encoding="utf-8")

    def _relocate_file_artifacts(self, result: Any) -> Any:
        """Move per-file evidence out of ``translated/`` into ``review/``.

        The pipeline writes companion files beside its CAD output so its
        atomic publication guarantees remain unchanged.  The task runner is
        the batch boundary, so it moves those companions after a successful
        file and rewrites the embedded references to their final paths.
        """
        try:
            relative_output = result.output.relative_to(self._publication_dir)
            review_parent = self._review_dir / relative_output.parent
        except ValueError:
            review_parent = self._review_dir
        review_parent.mkdir(parents=True, exist_ok=True)
        evidence_source = result.output.parent / f"{result.output.stem}.cad-work"
        evidence_target: Path | None = None
        if evidence_source.is_dir():
            evidence_target = review_parent / evidence_source.name
            collision_index = 2
            while evidence_target.exists() and evidence_target.resolve() != evidence_source.resolve():
                evidence_target = review_parent / f"{result.output.stem}.cad-work_{collision_index}"
                collision_index += 1
            if evidence_target.resolve() != evidence_source.resolve():
                shutil.move(str(evidence_source), str(evidence_target))
        paths = {
            "manifest": Path(result.manifest),
            "report": Path(result.report),
            "summary": Path(result.summary) if result.summary else None,
            "work_dxf": Path(result.work_dxf) if result.work_dxf else None,
        }
        moved: dict[str, Path | None] = {}
        for key, source in paths.items():
            if source is None or not source.is_file():
                moved[key] = source
                continue
            target = review_parent / source.name
            collision_index = 2
            while target.exists() and target.resolve() != source.resolve():
                target = review_parent / f"{result.output.stem}.{key}_{collision_index}{source.suffix}"
                collision_index += 1
            if target.resolve() != source.resolve():
                shutil.move(str(source), str(target))
            moved[key] = target

        if moved.get("manifest") and moved["manifest"].is_file():
            try:
                manifest = json.loads(moved["manifest"].read_text(encoding="utf-8"))
                if isinstance(manifest, dict):
                    manifest.update({
                        "output_dir": str(self._output_dir),
                        "translated_dir": str(self._translated_dir),
                        "publication_dir": str(self._publication_dir),
                        "review_dir": str(self._review_dir),
                        "work_evidence_dir": str(evidence_target or ""),
                        "manifest": str(moved["manifest"]),
                        "report": str(moved.get("report") or ""),
                        "summary": str(moved.get("summary") or ""),
                        "work_dxf": str(moved.get("work_dxf") or "") if moved.get("work_dxf") else None,
                    })
                    moved["manifest"].write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            except (OSError, ValueError, TypeError):
                pass
        if moved.get("report") and moved["report"].is_file():
            try:
                report = json.loads(moved["report"].read_text(encoding="utf-8"))
                if isinstance(report, dict):
                    report.update({
                        "output_dir": str(self._output_dir),
                        "translated_dir": str(self._translated_dir),
                        "publication_dir": str(self._publication_dir),
                        "review_dir": str(self._review_dir),
                        "work_evidence_dir": str(evidence_target or ""),
                        "manifest": str(moved.get("manifest") or ""),
                        "report": str(moved["report"]),
                        "summary": str(moved.get("summary") or ""),
                        "work_dxf": str(moved.get("work_dxf") or "") if moved.get("work_dxf") else None,
                    })
                    moved["report"].write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            except (OSError, ValueError, TypeError):
                pass
        result.manifest = moved.get("manifest") or result.manifest
        result.report = moved.get("report") or result.report
        result.summary = moved.get("summary") or result.summary
        result.work_dxf = moved.get("work_dxf") or result.work_dxf
        result.work_evidence_dir = evidence_target
        return result

    def _write_batch_artifacts(self, *, status: str, files: list[dict[str, Any]], issues: list[dict[str, Any]]) -> dict[str, str]:
        """Publish stable batch-level manifest/report/summary entry points."""
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._translated_dir.mkdir(parents=True, exist_ok=True)
        self._review_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 2,
            "status": status,
            "run_id": self._run_id,
            "output_dir": str(self._output_dir),
            "translated_dir": str(self._translated_dir),
            "publication_dir": str(self._publication_dir),
            "review_dir": str(self._review_dir),
            "checkpoint_path": str(self._checkpoint_path),
            "files": files,
            "issues": issues,
        }
        manifest_path = self._output_dir / "manifest.json"
        report_path = self._output_dir / "report.json"
        summary_path = self._output_dir / "summary.md"
        manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        report_path.write_text(json.dumps({**payload, "report": str(report_path), "manifest": str(manifest_path)}, ensure_ascii=False, indent=2), encoding="utf-8")
        lines = ["# CAD 翻译摘要", "", f"- 状态：{status}", f"- 结果目录：`{self._output_dir}`", f"- 已生成：{len([item for item in files if item.get('status') in {'succeeded', 'needs_review'}])} 个文件", f"- 需复核：{len(issues)} 项", "", "| 文件 | 状态 | 输出 |", "|---|---|---|"]
        for item in files:
            lines.append(f"| {Path(str(item.get('source_path') or item.get('output_path') or 'CAD')).name} | {item.get('status', '')} | {item.get('output_path', '')} |")
        summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return {"output_dir": str(self._output_dir), "translated_dir": str(self._translated_dir), "review_dir": str(self._review_dir), "manifest_path": str(manifest_path), "report_path": str(report_path), "summary_path": str(summary_path), "checkpoint_path": str(self._checkpoint_path)}

    def _run(self) -> None:
        started = time.monotonic()
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._run_dir.mkdir(parents=True, exist_ok=True)
        self._translated_dir.mkdir(parents=True, exist_ok=True)
        self._review_dir.mkdir(parents=True, exist_ok=True)
        results: list[dict[str, Any]] = []
        issues: list[dict[str, Any]] = []
        translated_count = 0
        memory_hits = 0
        api_calls = 0
        used_outputs: set[Path] = set()
        previous = self._load_checkpoint()
        previous_files = previous.get("files", {}) if previous.get("options_fingerprint") == self._options_fingerprint() else {}
        checkpoint_files: dict[str, Any] = {}
        target_label = _target_filename_label(self._options.target_lang)
        filename_translations: dict[str, str] = {}
        if self._translate_output_filename and self._filename_translator is not None:
            stems = list(dict.fromkeys(Path(item.path).stem for item in self._files))
            filename_translations = translate_names(
                self._filename_translator,
                stems,
                self._options.target_lang,
                self._options.source_lang,
                kind="CAD 输出文件名",
            )
        try:
            self._log("INFO", f"扫描到 {len(self._files)} 个 CAD 文件")
            for item in self._files:
                while self._pause_event.is_set() and not self._stop_event.is_set():
                    if not self._pause_acknowledged:
                        self._queue.put(StatusMsg(phase_desc="任务已暂停；当前文件已完成。"))
                        self._pause_acknowledged = True
                    time.sleep(0.1)
                source = Path(item.path)
                if self._stop_event.is_set():
                    self._file_progress.emit(source, "stopped", "stopped", result={"status": "stopped"})
                    continue
                try:
                    relative = source.relative_to(self._source_root) if self._source_root.is_dir() else Path(source.name)
                except ValueError:
                    relative = Path(source.name)
                translated_stem = output_stem_from_translation(
                    source.stem, filename_translations.get(source.stem, source.stem)
                )
                # A drawing number is an identity anchor; retain the source name
                # if the model drops any digit sequence while translating it.
                source_numbers = re.findall(r"\d+", source.stem)
                if source_numbers and any(number not in translated_stem for number in source_numbers):
                    translated_stem = source.stem
                base_output = self._publication_dir / relative.parent / f"{translated_stem}_{target_label}{source.suffix.lower()}"
                output = base_output
                collision_index = 2
                while output in used_outputs or output.exists():
                    output = base_output.with_name(f"{base_output.stem}_{collision_index}{base_output.suffix}")
                    collision_index += 1
                used_outputs.add(output)
                source_key = str(source.resolve())
                source_hash = self._source_hash(source)
                prior = previous_files.get(source_key) if isinstance(previous_files, dict) else None
                if isinstance(prior, dict) and prior.get("source_sha256") == source_hash:
                    prior_output = Path(str(prior.get("output_path") or ""))
                    prior_manifest = Path(str(prior.get("manifest_path") or ""))
                    if prior_output.is_file() and prior_manifest.is_file():
                        output.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(prior_output, output)
                        copied_paths: dict[str, str] = {}
                        try:
                            output_relative = output.relative_to(self._publication_dir)
                            review_parent = self._review_dir / output_relative.parent
                        except ValueError:
                            review_parent = self._review_dir
                        review_parent.mkdir(parents=True, exist_ok=True)
                        for key in ("manifest_path", "report_path", "summary_path", "work_dxf"):
                            old = Path(str(prior.get(key) or ""))
                            if old.is_file():
                                target = review_parent / old.name
                                collision_index = 2
                                while target.exists() and target.resolve() != old.resolve():
                                    target = review_parent / f"{output.stem}.{key}_{collision_index}{old.suffix}"
                                    collision_index += 1
                                if target.resolve() != old.resolve():
                                    shutil.copy2(old, target)
                                copied_paths[key] = str(target)
                        old_evidence = Path(str(prior.get("work_evidence_dir") or ""))
                        if not old_evidence.is_dir():
                            old_evidence = prior_output.parent / f"{prior_output.stem}.cad-work"
                        if old_evidence.is_dir():
                            evidence_target = review_parent / old_evidence.name
                            collision_index = 2
                            while evidence_target.exists() and evidence_target.resolve() != old_evidence.resolve():
                                evidence_target = review_parent / f"{output.stem}.cad-work_{collision_index}"
                                collision_index += 1
                            if evidence_target.resolve() != old_evidence.resolve():
                                shutil.copytree(old_evidence, evidence_target)
                            copied_paths["work_evidence_dir"] = str(evidence_target)
                        file_result = dict(prior.get("result") or {})
                        file_result.update({"source_path": str(source), "output_path": str(output), "status": "succeeded", "resumed": True, **copied_paths})
                        results.append(file_result)
                        self._file_progress.emit(source, "generated", "verify", completed=1, total=1, result=file_result)
                        self._log("INFO", f"{source.name}：复用已验证的上次结果")
                        checkpoint_files[source_key] = {**prior, "output_path": str(output), **copied_paths, "result": file_result}
                        self._write_checkpoint(checkpoint_files)
                        continue
                self._file_progress.emit(source, "running", "extract")
                def progress(value: CadProgress, path: Path = source) -> None:
                    self._progress(value, file_path=path)

                pipeline = CadTranslationPipeline(
                    converter=self._converter,
                    memory_lookup=self._memory_lookup,
                    translator=self._translator,
                    progress=progress,
                )
                try:
                    result = pipeline.translate_file(
                    source,
                    output,
                    options=self._options,
                    glossary=self._glossary,
                    stop_requested=self._stop_event.is_set,
                )
                    if self._options.copy_related_files:
                        for related in source.parent.glob(f"{source.stem}.*"):
                            if related == source or related.suffix.lower() in {".dwg", ".dxf"}:
                                continue
                            target = result.output.parent / related.name
                            if not target.exists():
                                shutil.copy2(related, target)
                except Exception as exc:  # one bad drawing must not erase the batch
                    message = str(exc) or exc.__class__.__name__
                    self._file_progress.emit(source, "failed", "error", result={"status": "failed", "message": message})
                    results.append({"source_path": str(source), "status": "failed", "message": message})
                    issues.append({"file": source.name, "stage": "cad", "message": message})
                    self._log("ERROR", f"{source.name}：{message}")
                    continue
                result = self._relocate_file_artifacts(result)
                stats = result.stats
                translated_count += stats.changed
                memory_hits += stats.memory_hits
                api_calls += stats.ai_translated
                file_result = {
                    "source_path": str(result.source),
                    "output_path": str(result.output),
                    "work_dxf": str(result.work_dxf),
                    "manifest_path": str(result.manifest),
                    "report_path": str(result.report),
                    "summary_path": str(result.summary) if result.summary else "",
                    "work_evidence_dir": str(getattr(result, "work_evidence_dir", "") or ""),
                    "status": "needs_review"
                    if result.unresolved
                    or stats.replacement_characters
                    or stats.residual_foreign_text_count
                    or stats.unsupported_entities
                    or stats.unknown_entity_count
                    else "succeeded",
                    "stats": {
                        "candidate_entities": stats.candidate_entities,
                        "changed": stats.changed,
                        "memory_hits": stats.memory_hits,
                        "glossary_hits": stats.glossary_hits,
                        "ai_translated": stats.ai_translated,
                        "untranslated": stats.untranslated,
                        "replacement_characters": stats.replacement_characters,
                        "residual_foreign_text_count": stats.residual_foreign_text_count,
                        "unsupported_entities": stats.unsupported_entities,
                        "unknown_entity_count": stats.unknown_entity_count,
                    },
                    "unresolved": result.unresolved,
                }
                results.append(file_result)
                checkpoint_files[source_key] = {"source_sha256": source_hash, "output_path": str(result.output), "manifest_path": str(result.manifest), "report_path": str(result.report), "summary_path": str(result.summary) if result.summary else "", "work_dxf": str(result.work_dxf), "work_evidence_dir": str(getattr(result, "work_evidence_dir", "") or ""), "result": file_result}
                self._write_checkpoint(checkpoint_files)
                if result.unresolved:
                    issues.extend({"file": source.name, **entry} for entry in result.unresolved)
                if stats.replacement_characters:
                    issues.append({"file": source.name, "reason": "replacement_characters", "count": stats.replacement_characters})
                if stats.residual_foreign_text_count:
                    issues.append({"file": source.name, "reason": "residual_foreign_text", "count": stats.residual_foreign_text_count})
                if stats.unsupported_entities or stats.unknown_entity_count:
                    issues.append({"file": source.name, "reason": "unsupported_entities", "count": stats.unsupported_entities + stats.unknown_entity_count})
                self._file_progress.emit(source, "generated", "verify", completed=1, total=1, result=file_result)
                self._log("INFO", f"{source.name}：已生成翻译 DWG")
            elapsed = round(time.monotonic() - started, 3)
            batch_status = "stopped" if self._stop_event.is_set() else ("completed_with_issues" if issues else "done")
            batch_paths = self._write_batch_artifacts(status=batch_status, files=results, issues=issues)
            for item in results:
                item.update({key: value for key, value in batch_paths.items() if key in {"output_dir", "translated_dir", "review_dir"}})
            if self._stop_event.is_set():
                self._queue.put(
                    StoppedMsg(
                        message="任务已停止；已完成文件和断点均已保留。",
                        **batch_paths,
                        files=results,
                        issues=issues,
                    )
                )
            else:
                self._queue.put(
                    DoneMsg(
                        **batch_paths,
                        file_results=results,
                        files=results,
                        elapsed_sec=elapsed,
                        tm_hit_count=memory_hits,
                        api_call_count=api_calls,
                        issues=issues,
                        kpi={"translated_entities": translated_count, "file_count": len(results)},
                    )
                )
        except Exception as exc:  # runner-level failures still become a task result
            batch_paths = self._write_batch_artifacts(status="error", files=results, issues=issues)
            self._queue.put(ErrorMsg(message=f"CAD 翻译流程失败：{exc}", **batch_paths))
