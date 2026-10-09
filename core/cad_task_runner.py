"""Task-manager runner for the optional CAD translation plugin."""

from __future__ import annotations

import queue
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from core.cad_translation import (
    CadPipelineOptions,
    CadProgress,
    CadTranslationPipeline,
)
from core.file_progress import FileProgressReporter
from core.task_runner import DoneMsg, ErrorMsg, LogMsg, ProgressMsg, StatusMsg, StoppedMsg


_PHASES = {
    "scan": (1, "读取与扫描"),
    "match": (2, "匹配记忆/术语"),
    "translate": (3, "AI 翻译"),
    "write": (4, "写回 DWG"),
    "verify": (5, "重新读取与校验"),
}


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
        glossary: Mapping[str, str] | None = None,
        options: CadPipelineOptions | None = None,
    ) -> None:
        self._files = files
        self._source_root = Path(source_root)
        self._output_dir = Path(output_dir)
        self._converter = converter
        self._memory_lookup = memory_lookup
        self._translator = translator
        self._glossary = dict(glossary or {})
        self._options = options or CadPipelineOptions()
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
        self._queue.put(
            ProgressMsg(
                phase_index=phase_index,
                phase_total=5,
                phase_name=phase_name,
                step_done=max(0, int(progress.done)),
                step_total=max(1, int(progress.total)),
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

    def _run(self) -> None:
        started = time.monotonic()
        self._output_dir.mkdir(parents=True, exist_ok=True)
        results: list[dict[str, Any]] = []
        issues: list[dict[str, Any]] = []
        translated_count = 0
        memory_hits = 0
        api_calls = 0
        used_outputs: set[Path] = set()
        try:
            self._log("INFO", f"扫描到 {len(self._files)} 个 CAD 文件")
            for item in self._files:
                while self._pause_event.is_set() and not self._stop_event.is_set():
                    time.sleep(0.1)
                source = Path(item.path)
                if self._stop_event.is_set():
                    self._file_progress.emit(source, "stopped", "stopped", result={"status": "stopped"})
                    continue
                try:
                    relative = source.relative_to(self._source_root) if self._source_root.is_dir() else Path(source.name)
                except ValueError:
                    relative = Path(source.name)
                output = self._output_dir / relative.parent / f"{source.stem}_中文{source.suffix.lower()}"
                if output in used_outputs:
                    message = "批次中存在同名 CAD 文件，已拒绝覆盖输出。"
                    self._file_progress.emit(source, "failed", "write", result={"status": "failed", "message": message})
                    results.append({"source_path": str(source), "status": "failed", "message": message})
                    issues.append({"file": source.name, "stage": "write", "message": message})
                    continue
                used_outputs.add(output)
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
            if self._stop_event.is_set():
                self._queue.put(
                    StoppedMsg(
                        message="任务已停止；已完成文件和断点均已保留。",
                        output_dir=str(self._output_dir),
                        files=results,
                        issues=issues,
                    )
                )
            else:
                self._queue.put(
                    DoneMsg(
                        output_dir=str(self._output_dir),
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
            self._queue.put(ErrorMsg(message=f"CAD 翻译流程失败：{exc}"))
