"""Task-manager runner for the optional CAD translation plugin."""

from __future__ import annotations

import queue
import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Mapping

from loguru import logger

from core.runner_base import BaseTaskRunner
from core.cad_translation import (
    CadPipelineError,
    CadPipelineOptions,
    CadProgress,
    CadStopRequested,
    CadTranslationPipeline,
    read_profile_inputs,
)
from core.cad_review import pipeline_options_snapshot
from core.concurrent_calls import wait_result
from core.file_progress import FileProgressReporter, file_progress_id
from core.language_registry import get_target_lang_display
from core.output_name_translation import translate_names, output_stem_from_translation
from core.task_runner import (
    DoneMsg,
    ErrorMsg,
    LogMsg,
    ProgressMsg,
    StatusMsg,
    StoppedMsg,
    user_facing_reason,
)
from core.process_artifacts import ProcessArtifacts, find_process_dirs
from core.output_metadata import build_translation_metadata, write_translation_metadata


# 每个图纸内部的 5 步，归到所有翻译页面共用的阶段名（读取文件 / 翻译 / 生成文件）
# 下面：进度卡上的阶段名和 Excel / Word / PDF 一致，具体在做哪一步写在后半句里。
_PHASES = {
    "scan": ("读取文件", "正在读取图纸"),
    "match": ("翻译", "正在匹配记忆库与术语"),
    "translate": ("翻译", "正在翻译图纸文字"),
    "write": ("生成文件", "正在写回图纸"),
    "verify": ("生成文件", "正在校验结果"),
}

# 暂停真正生效时发出的状态行。api/task_manager.py 靠这个前缀确认「已暂停」，
# 改措辞时两边一起改（那边直接 import 这个常量）。
# 两处生效点：两张图纸之间，以及一张图纸翻译途中的两批请求之间（只有一张图纸时，
# 以前要等整张图翻完才停，等于点了没用）。
CAD_PAUSED_STATUS = "已暂停：当前文件已翻译完成，可继续翻译或停止任务。"
CAD_PAUSED_IN_FILE_STATUS = "已暂停：不再发出新的翻译请求，可以继续翻译或停止任务。"
CAD_PAUSED_STATUS_PREFIX = "已暂停"

# 摘要里给人看的状态；manifest / report 里仍存内部状态码。
_CAD_STATUS_LABELS = {
    "done": "已完成",
    "completed_with_issues": "已完成，部分内容需复核",
    "stopped": "已停止",
    "error": "出错",
    "succeeded": "已完成",
    "needs_review": "需复核",
    "failed": "失败",
}

# 单个图纸失败时给用户看的兜底句；原始异常写进日志文件。
_CAD_FILE_FAILURE_FALLBACK = "出现意外问题，此图纸未能翻译。"


def _cad_failure_message(exc: BaseException, *, fallback: str = _CAD_FILE_FAILURE_FALLBACK) -> str:
    """把 CAD 失败换成一句人话：管线自己的中文报错原样用，其它异常走 humanize。"""
    if isinstance(exc, CadPipelineError) and str(exc).strip():
        return str(exc).strip()
    return user_facing_reason(exc, fallback=fallback)


def _file_identity(source: Path) -> dict[str, str]:
    """Name + file_id that the workspace uses to match a result to its scan row.

    ``source_path`` is blanked by the API privacy filter, so without these the
    file list cannot show which drawing produced what.
    """
    return {"name": source.name, "file_id": file_progress_id(source)}


# 整批进度条的分母。step_done/step_total 只当比例用（界面和任务中心都只取比值）。
_BATCH_PROGRESS_SCALE = 10000


def _file_progress_weight(path: Path) -> float:
    """One drawing's share of the batch progress bar.

    0.2 + size in MB, where a DXF counts a fifth of its size: an ASCII DXF is
    several times larger than the same drawing as DWG, and reading it takes no
    converter.  The 0.2 floor keeps a tiny drawing from being invisible; the
    size term keeps a 40 MB drawing from getting the same share as a 100 KB one.
    """
    try:
        size_mb = path.stat().st_size / (1024 * 1024)
    except OSError:
        size_mb = 0.0
    if path.suffix.lower() == ".dxf":
        size_mb /= 5
    return 0.2 + size_mb


def _target_filename_label(target_lang: str) -> str:
    """Return a stable, filesystem-safe target-language suffix."""
    label = get_target_lang_display(str(target_lang or "").strip()) or str(target_lang or "").strip()
    safe = "".join(char for char in label if char.isalnum() or char in {"_", "-", "."})
    return safe or "中文"


class CadTaskRunner(BaseTaskRunner):
    """Run one or more CAD files while exposing the shared runner protocol."""

    _thread_name = "cad-translation"
    # CAD calls the translation engine through a closure the task manager
    # builds, which already carries the task's connection keys.
    _uses_key_overrides = False

    def __init__(
        self,
        files: list[Any],
        *,
        source_root: Path,
        output_dir: Path,
        converter: Any = None,
        memory_lookup: Any = None,
        term_memory: Any = None,
        translator: Any = None,
        language_translator: Any = None,
        profile_requester: Any = None,
        profile_concurrency: int = 1,
        filename_translator: Any = None,
        translate_output_filename: bool = False,
        glossary: Mapping[str, str] | None = None,
        options: CadPipelineOptions | None = None,
        resume_output_dir: Path | None = None,
        translation_identity: Mapping[str, Any] | None = None,
        dxf_cache: Any = None,
    ) -> None:
        self._files = files
        self._dxf_cache = dxf_cache
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
        self._process_artifacts = ProcessArtifacts.create("cad", self._output_dir, self._run_id)
        self._run_dir = self._process_artifacts.work_dir
        self._translated_dir = self._output_dir
        self._review_dir = self._run_dir / ".cad-review"
        self._publication_dir = self._output_dir
        self._converter = converter
        self._memory_lookup = memory_lookup
        # 记忆库（core.term_session.DocumentTermMemory）：每张图纸一个会话，收尾合并成
        # 任务详情里的「记忆库」一节。
        self._term_memory = term_memory
        self._translator = translator
        self._language_translator = language_translator
        self._profile_requester = profile_requester
        self._profile_concurrency = max(1, int(profile_concurrency or 1))
        self._filename_translator = filename_translator
        self._translate_output_filename = bool(translate_output_filename)
        self._glossary = dict(glossary or {})
        self._options = options or CadPipelineOptions()
        self._translation_identity = dict(translation_identity or {})
        self._checkpoint_path = self._run_dir / ".cad-checkpoint.json"
        self._last_stage: str | None = None
        self._queue: queue.Queue[Any] = queue.Queue()
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._file_progress = FileProgressReporter(self._queue, files, self._source_root)
        self._file_progress.emit_many("waiting", "extract")
        # 每个文件在整批进度里占的份额（见 _file_progress_weight）和它之前所有文件的
        # 份额之和；_batch_shown 是已经报出去的最大值，进度只进不退。
        weights = [_file_progress_weight(Path(item.path)) for item in files]
        total_weight = sum(weights) or 1.0
        offsets: list[float] = []
        running = 0.0
        for weight in weights:
            offsets.append(running)
            running += weight
        self._file_weights = [weight / total_weight for weight in weights]
        self._file_offsets = [offset / total_weight for offset in offsets]
        self._batch_shown = 0
        self._last_label = ""

    def _before_start(self) -> None:
        self._pause_event.clear()

    def stop(self) -> None:
        super().stop()
        self._pause_event.clear()

    def pause(self) -> None:
        self._pause_event.set()

    def resume(self) -> None:
        self._pause_event.clear()

    def end_paused(self) -> None:
        self.stop()

    def _wait_while_paused(self, status: str) -> None:
        """Block the runner thread while paused; return on resume or stop."""
        # 「已暂停」每次停下都要说一句：界面收到它才从「正在暂停」变成「已暂停」、才能点继续。
        # 这个标记只属于这一次等待；放在对象上由 resume() 清掉的话，「继续」恰好落在
        # 说出这句和记下「已说过」之间，下一次暂停就再也不说，任务卡在「正在暂停」。
        announced = False
        while self._pause_event.is_set() and not self._stop_event.is_set():
            if not announced:
                self._queue.put(StatusMsg(phase_desc=status))
                announced = True
            time.sleep(0.1)

    def _progress(self, progress: CadProgress, *, file_path: Path | None = None) -> None:
        stage_name, activity = _PHASES.get(progress.stage, ("翻译", ""))
        # 一批里每个文件都会把 5 个阶段从头走一遍。如果照 Excel/Word 的做法上报
        # 「阶段 i/5 + 阶段内进度」，界面按阶段折算出的总进度会在第二个文件回到
        # 「读取与扫描」时倒退一大截。CAD 这里直接上报整批的总进度：
        # step_done/step_total = 已完成文件 + 当前文件内的加权进度（CadProgress.percent
        # 已按各格式的阶段权重折算好，见 cad_stage_weights），phase_index=0 表示「没有
        # 跨文件的阶段划分」，界面就直接用这个比例画进度条。phase_name 写成「阶段名 ·
        # 具体一步：第 i / N 个文件」，界面取「 · 」前面的部分当阶段名，后半句当进度卡标题。
        # 文件之间不再平分：每个文件按大小占一份（_file_progress_weight），大图纸不会和
        # 小图纸各占一样多。
        file_count = max(1, len(self._files))
        index = next((i for i, item in enumerate(self._files) if file_path is not None and Path(item.path) == Path(file_path)), 0)
        local_percent = max(0.0, min(100.0, float(progress.percent)))
        detail = f"{activity}：第 {index + 1} / {file_count} 个文件" if file_count > 1 else activity
        label = f"{stage_name} · {detail}" if detail else stage_name
        self._last_stage = progress.stage
        self._put_batch_progress(index, local_percent / 100.0, label)
        # 具体说明（「发现 N 个可翻译文字实体」等）进运行日志，不再用状态消息覆盖进度卡标题——
        # 以前标题会在「AI 翻译」和「状态：[阶段 3/5] 翻译完成」之间来回跳。
        if progress.message:
            prefix = f"{Path(file_path).name}：" if file_path is not None else ""
            self._log(progress.level or "INFO", f"{prefix}{progress.message}")
        if file_path is not None:
            # 文件状态用全页共享的 "active"（见 ui/src/file-progress.ts），以前写成
            # "running"，界面认不出来，状态芯片上直接显示 undefined。
            self._file_progress.emit(
                file_path,
                "active",
                progress.stage,
                completed=max(0, int(progress.done)),
                total=max(1, int(progress.total)),
            )

    def _put_batch_progress(self, index: int, fraction: float, label: str | None = None) -> None:
        """Report the whole batch's progress: files before ``index`` plus ``fraction`` of it."""
        if self._file_weights:
            index = max(0, min(index, len(self._file_weights) - 1))
            share = self._file_offsets[index] + self._file_weights[index] * max(0.0, min(1.0, fraction))
        else:
            share = 1.0
        done = min(_BATCH_PROGRESS_SCALE, int(round(share * _BATCH_PROGRESS_SCALE)))
        # 只进不退：失败或沿用上次结果的文件之后，下一个文件从自己的起点开始报。
        done = max(done, self._batch_shown)
        self._batch_shown = done
        if label is not None:
            self._last_label = label
        self._queue.put(
            ProgressMsg(
                phase_index=0,
                phase_total=0,
                phase_name=self._last_label,
                step_done=done,
                step_total=_BATCH_PROGRESS_SCALE,
            )
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
        # New runs keep checkpoints in the shared process store.  Search only
        # runs associated with this output root; the root-level legacy files
        # remain readable for pre-process-store jobs.
        candidates: list[Path] = []
        try:
            candidates.extend(path / ".cad-checkpoint.json" for path in find_process_dirs(self._resume_output_dir, kind="cad"))
        except (OSError, TypeError, ValueError):
            pass
        candidates.extend([
            self._resume_output_dir / ".cad-checkpoint.json",
            self._resume_output_dir.parent / ".cad-checkpoint.json",
        ])
        try:
            candidates.extend(self._resume_output_dir.rglob(".cad-checkpoint.json"))
        except OSError:
            pass
        for path in candidates:
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    return value
            except (OSError, ValueError, TypeError):
                continue
        return {}

    def _publish_result(self, result: Any, output: Path) -> Any:
        """Atomically publish only the final CAD file into the task output."""
        work_output = Path(result.output)
        evidence_dir = work_output.parent / f"{work_output.stem}.cad-work"
        # A drawing that passed verification has no evidence folder left (the
        # pipeline removes its DXF copies and the then-empty folder).
        result.work_evidence_dir = evidence_dir if evidence_dir.is_dir() else None
        output.parent.mkdir(parents=True, exist_ok=True)
        staged = output.with_name(f".{output.name}.{self._run_id}.publish")
        if staged.exists():
            staged.unlink()
        shutil.copy2(result.output, staged)
        os.replace(staged, output)
        result.output = output
        # Companion reports stay in the process store, but their output link
        # must point at the published user deliverable rather than the private
        # staging copy used by the pipeline.
        for report_path in (Path(result.manifest), Path(result.report)):
            try:
                payload = json.loads(report_path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    payload["output"] = str(output)
                    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            except (OSError, ValueError, TypeError):
                pass
        return result

    def _write_checkpoint(self, entries: Mapping[str, Any]) -> None:
        self._checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"schema_version": 1, "options_fingerprint": self._options_fingerprint(), "files": entries}, ensure_ascii=False, indent=2)
        self._checkpoint_path.write_text(payload, encoding="utf-8")
        # Legacy root-level checkpoints are read for compatibility, but new
        # runs never place private state in the user-visible output folder.

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
        if evidence_source.is_dir() and not any(evidence_source.iterdir()):
            # Nothing to keep: do not move an empty folder into the review area.
            evidence_source.rmdir()
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
        self._run_dir.mkdir(parents=True, exist_ok=True)
        self._review_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 2,
            "status": status,
            "run_id": self._run_id,
            # 译文对照表「重新写回」按这份设置写，和第一次翻译的写法一致（见 core.cad_review）。
            "pipeline_options": pipeline_options_snapshot(self._options),
            "output_dir": str(self._output_dir),
            "translated_dir": str(self._translated_dir),
            "publication_dir": str(self._publication_dir),
            "review_dir": str(self._review_dir),
            "checkpoint_path": str(self._checkpoint_path),
            "files": files,
            "issues": issues,
        }
        manifest_path = self._run_dir / "manifest.json"
        report_path = self._run_dir / "report.json"
        summary_path = self._run_dir / "summary.md"
        manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        report_path.write_text(json.dumps({**payload, "report": str(report_path), "manifest": str(manifest_path)}, ensure_ascii=False, indent=2), encoding="utf-8")
        lines = ["# CAD 翻译摘要", "", f"- 状态：{_CAD_STATUS_LABELS.get(status, status)}", f"- 结果目录：`{self._output_dir}`", f"- 已生成：{len([item for item in files if item.get('status') in {'succeeded', 'needs_review'}])} 个文件", f"- 需复核：{len(issues)} 项", "", "| 文件 | 状态 | 输出 |", "|---|---|---|"]
        for item in files:
            lines.append(f"| {Path(str(item.get('source_path') or item.get('output_path') or 'CAD')).name} | {_CAD_STATUS_LABELS.get(str(item.get('status', '')), item.get('status', ''))} | {item.get('output_path', '')} |")
        summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return {"output_dir": str(self._output_dir), "translated_dir": str(self._translated_dir), "review_dir": str(self._review_dir), "manifest_path": str(manifest_path), "report_path": str(report_path), "summary_path": str(summary_path), "checkpoint_path": str(self._checkpoint_path)}

    def _run(self) -> None:
        started = time.monotonic()
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._translated_dir.mkdir(parents=True, exist_ok=True)
        self._review_dir.mkdir(parents=True, exist_ok=True)
        results: list[dict[str, Any]] = []
        issues: list[dict[str, Any]] = []
        translated_count = 0
        kept_count = 0
        memory_hits = 0
        api_calls = 0
        failed_files = False
        used_outputs: set[Path] = set()
        previous = self._load_checkpoint()
        previous_files = previous.get("files", {}) if previous.get("options_fingerprint") == self._options_fingerprint() else {}
        checkpoint_files: dict[str, Any] = {}
        target_label = _target_filename_label(self._options.target_lang)
        filename_translations: dict[str, str] = {}
        if self._translate_output_filename and self._filename_translator is not None:
            stems = list(dict.fromkeys(Path(item.path).stem for item in self._files))
            try:
                filename_translations = translate_names(
                    self._filename_translator,
                    stems,
                    self._options.target_lang,
                    self._options.source_lang,
                    kind="图纸文件名",
                )
            except Exception as exc:
                # A filename provider outage must not strand an active
                # process run; preserve source stems and continue the CAD
                # translation itself.
                logger.warning(f"[CAD] 输出文件名翻译失败：{exc!r}")
                self._log(
                    "WARN",
                    "图纸文件名未能翻译，已保留原文件名："
                    + user_facing_reason(exc, fallback="本次未获得可用的译名。"),
                )
        # 「自动识别场景」：后面几张图纸的图纸说明提前同时请求，翻到哪张时直接取用，
        # 不用每张图纸都先停下来等一次通读。接着上次继续的批次不提前请求：沿用
        # 上次结果的图纸用不着说明，提前请求就白花了。
        prefetch = (
            _ProfilePrefetch(
                self._profile_requester,
                [Path(item.path) for item in self._files[1:]],
                options=self._options,
                dxf_cache=self._dxf_cache,
                max_workers=self._profile_concurrency,
                stop_requested=self._stop_event.is_set,
                paused=self._pause_event.is_set,
            )
            if self._profile_requester is not None and len(self._files) > 1 and not previous_files
            else None
        )
        try:
            self._log("INFO", f"共 {len(self._files)} 个图纸文件")
            for file_index, item in enumerate(self._files):
                self._wait_while_paused(CAD_PAUSED_STATUS)
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
                        write_translation_metadata(
                            output,
                            build_translation_metadata(
                                surface="cad",
                                source_path=source,
                                target_lang=self._options.target_lang,
                                source_lang=self._options.source_lang,
                                parameters={
                                    "pipeline": pipeline_options_snapshot(self._options),
                                    "translation": self._translation_identity,
                                },
                                output_mode=str(self._translation_identity.get("output_mode") or "task_folder"),
                                fallback_used=str(self._translation_identity.get("fallback_used") or ""),
                            ),
                        )
                        copied_paths: dict[str, str] = {}
                        try:
                            output_relative = output.relative_to(self._publication_dir)
                            review_parent = self._review_dir / output_relative.parent
                        except ValueError:
                            review_parent = self._review_dir
                        review_parent.mkdir(parents=True, exist_ok=True)
                        for key in ("manifest_path", "report_path", "summary_path", "work_dxf"):
                            recorded = str(prior.get(key) or "")
                            old = Path(recorded)
                            if recorded and old.is_file():
                                target = review_parent / old.name
                                collision_index = 2
                                while target.exists() and target.resolve() != old.resolve():
                                    target = review_parent / f"{output.stem}.{key}_{collision_index}{old.suffix}"
                                    collision_index += 1
                                if target.resolve() != old.resolve():
                                    shutil.copy2(old, target)
                                copied_paths[key] = str(target)
                        # 空字符串变成 Path("") 就是当前目录：在应用里可能是 /，会把整个磁盘复制过来。
                        recorded_evidence = str(prior.get("work_evidence_dir") or "")
                        old_evidence = Path(recorded_evidence) if recorded_evidence else None
                        if old_evidence is None or not old_evidence.is_dir():
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
                        file_result.update({**_file_identity(source), "source_path": str(source), "output_path": str(output), "status": "succeeded", "resumed": True, **copied_paths})
                        results.append(file_result)
                        self._file_progress.emit(source, "generated", "verify", completed=1, total=1, result=file_result)
                        self._put_batch_progress(file_index, 1.0)
                        self._log("INFO", f"{source.name}：沿用上次的翻译结果")
                        checkpoint_files[source_key] = {**prior, "output_path": str(output), **copied_paths, "result": file_result}
                        self._write_checkpoint(checkpoint_files)
                        continue
                self._file_progress.emit(source, "active", "scan")
                def progress(value: CadProgress, path: Path = source) -> None:
                    self._progress(value, file_path=path)

                pipeline = CadTranslationPipeline(
                    converter=self._converter,
                    memory_lookup=self._memory_lookup,
                    term_memory=self._term_memory,
                    translator=self._translator,
                    language_translator=self._language_translator,
                    profile_requester=(
                        prefetch.requester_for(source) if prefetch is not None else self._profile_requester
                    ),
                    progress=progress,
                    pause_gate=lambda: self._wait_while_paused(CAD_PAUSED_IN_FILE_STATUS),
                    dxf_cache=self._dxf_cache,
                )
                try:
                    work_output = self._run_dir / relative.parent / output.name
                    result = pipeline.translate_file(
                    source,
                    work_output,
                    options=self._options,
                    glossary=self._glossary,
                    stop_requested=self._stop_event.is_set,
                )
                    result = self._publish_result(result, output)
                    metadata = build_translation_metadata(
                        surface="cad",
                        source_path=source,
                        target_lang=self._options.target_lang,
                        source_lang=self._options.source_lang,
                        parameters={
                            "pipeline": pipeline_options_snapshot(self._options),
                            "translation": self._translation_identity,
                        },
                        output_mode=str(self._translation_identity.get("output_mode") or "task_folder"),
                        fallback_used=str(self._translation_identity.get("fallback_used") or ""),
                    )
                    if not write_translation_metadata(result.output, metadata):
                        logger.debug("CAD 输出不支持写入 XL_Translator 文件属性：%s", result.output)
                    if self._options.copy_related_files:
                        for related in source.parent.glob(f"{source.stem}.*"):
                            if related == source or related.suffix.lower() in {".dwg", ".dxf"}:
                                continue
                            target = result.output.parent / related.name
                            if not target.exists():
                                shutil.copy2(related, target)
                except CadStopRequested:
                    # 用户点了停止：当前文件记为"已停止"，不是"失败"，也不进问题清单。
                    self._file_progress.emit(source, "stopped", "stopped", result={"status": "stopped"})
                    results.append({**_file_identity(source), "source_path": str(source), "status": "stopped", "message": "已停止"})
                    continue
                except Exception as exc:  # one bad drawing must not erase the batch
                    failed_files = True
                    logger.opt(exception=exc).warning(f"[CAD] {source.name} 翻译失败：{exc!r}")
                    message = _cad_failure_message(exc)
                    self._file_progress.emit(source, "failed", "error", result={"status": "failed", "message": message})
                    results.append({**_file_identity(source), "source_path": str(source), "status": "failed", "message": message})
                    issues.append({"file": source.name, "stage": "cad", "message": message, "problem": message})
                    self._log("ERROR", f"{source.name}：{message}")
                    # 这张图纸已经处理完（失败也是结果）：进度到它的终点，下一张接着走。
                    self._put_batch_progress(file_index, 1.0)
                    continue
                stats = result.stats
                translated_count += stats.changed
                kept_count += getattr(stats, "kept_units", 0)
                memory_hits += stats.memory_hits
                api_calls += stats.ai_translated
                file_result = {
                    **_file_identity(source),
                    "source_path": str(result.source),
                    "output_path": str(result.output),
                    "work_dxf": str(result.work_dxf),
                    "manifest_path": str(result.manifest),
                    "report_path": str(result.report),
                    "summary_path": str(result.summary) if result.summary else "",
                    "work_evidence_dir": str(getattr(result, "work_evidence_dir", "") or ""),
                    "status": "needs_review"
                    if result.unresolved
                    or getattr(result, "term_review", None)
                    or stats.replacement_characters
                    or stats.residual_foreign_text_count
                    or stats.unsupported_entities
                    or stats.unknown_entity_count
                    or stats.layout_unresolved
                    else "succeeded",
                    "stats": {
                        "candidate_entities": stats.candidate_entities,
                        "changed": stats.changed,
                        "kept_units": getattr(stats, "kept_units", 0),
                        "memory_hits": stats.memory_hits,
                        "glossary_hits": stats.glossary_hits,
                        "ai_translated": stats.ai_translated,
                        "untranslated": stats.untranslated,
                        "replacement_characters": stats.replacement_characters,
                        "residual_foreign_text_count": stats.residual_foreign_text_count,
                        "unsupported_entities": stats.unsupported_entities,
                        "unknown_entity_count": stats.unknown_entity_count,
                        "layout_fitted": stats.layout_fitted,
                        "layout_unresolved": stats.layout_unresolved,
                        "term_not_applied": getattr(stats, "term_not_applied", 0),
                    },
                    "unresolved": result.unresolved,
                }
                results.append(file_result)
                checkpoint_files[source_key] = {"source_sha256": source_hash, "output_path": str(result.output), "manifest_path": str(result.manifest), "report_path": str(result.report), "summary_path": str(result.summary) if result.summary else "", "work_dxf": str(result.work_dxf), "work_evidence_dir": str(getattr(result, "work_evidence_dir", "") or ""), "result": file_result}
                self._write_checkpoint(checkpoint_files)
                # 每一条都让这张图纸的状态变成 needs_review，severity 跟着明说：界面只认
                # severity 判断是不是待办，不带它时徽章会退回「已完成」，和文件状态对不上。
                if result.unresolved:
                    issues.extend(
                        {"file": source.name, **entry, "severity": "needs_review", "problem": f"未能翻译：{entry.get('reason') or '未获得译文'}"}
                        for entry in result.unresolved
                    )
                if stats.replacement_characters:
                    issues.append({"file": source.name, "severity": "needs_review", "reason": "replacement_characters", "count": stats.replacement_characters, "problem": f"有 {stats.replacement_characters} 处文字出现乱码"})
                for entry in getattr(result, "term_review", None) or ():
                    issues.append({
                        "file": source.name,
                        "source": entry.get("source", ""),
                        "reason": "term_not_applied",
                        "severity": "needs_review",
                        "problem": "未采用设定译法",
                        "status": entry.get("reason", ""),
                    })
                if stats.residual_foreign_text_count:
                    issues.append({"file": source.name, "severity": "needs_review", "reason": "residual_foreign_text", "count": stats.residual_foreign_text_count, "problem": f"有 {stats.residual_foreign_text_count} 处译文里仍有原文"})
                if stats.layout_unresolved:
                    issues.append({"file": source.name, "severity": "needs_review", "reason": "layout_overflow", "count": stats.layout_unresolved, "problem": f"有 {stats.layout_unresolved} 处译文缩到最小比例仍会压到旁边的文字或伸出图框"})
                if stats.unsupported_entities or stats.unknown_entity_count:
                    issues.append({"file": source.name, "severity": "needs_review", "reason": "unsupported_entities", "count": stats.unsupported_entities + stats.unknown_entity_count, "problem": f"有 {stats.unsupported_entities + stats.unknown_entity_count} 处图纸对象暂不支持翻译，已原样保留"})
                self._file_progress.emit(source, "generated", "verify", completed=1, total=1, result=file_result)
                self._log("INFO", f"{source.name}：已生成翻译图纸")
            elapsed = round(time.monotonic() - started, 3)
            batch_status = "stopped" if self._stop_event.is_set() else ("completed_with_issues" if issues else "done")
            batch_paths = self._write_batch_artifacts(status=batch_status, files=results, issues=issues)
            process_status = (
                "cancelled" if self._stop_event.is_set()
                else "failed" if failed_files and not any(item.get("status") in {"succeeded", "needs_review"} for item in results)
                else "needs_review" if issues
                else "succeeded"
            )
            self._process_artifacts.finish(process_status)
            for item in results:
                item.update({key: value for key, value in batch_paths.items() if key in {"output_dir", "translated_dir", "review_dir"}})
            if self._stop_event.is_set():
                completed_count = sum(1 for item in results if item.get("status") in {"succeeded", "needs_review"})
                # 第一张图写完之前就停下时，没有断点可续（断点按文件记），输出目录也会
                # 因为是空的被收掉，界面不会给「从断点继续」——说明文字要跟着说实话。
                stop_message = (
                    "已停止；已翻译的图纸均已保留，可使用「继续上次翻译」继续。"
                    if completed_count
                    else "任务已停止，尚无图纸完成翻译；原图纸未改动，如有需要可重新开始翻译。"
                )
                self._queue.put(
                    StoppedMsg(
                        message=stop_message,
                        **batch_paths,
                        files=results,
                        issues=issues,
                        terms=self._finish_terms(),
                        # 与 Excel/Word 同一口径：还有几个文件没跑完，界面据此写「还有 N 个文件没跑到」。
                        kpi={"unstarted_file_count": sum(
                            1 for item in self._files
                            if not any(
                                entry.get("source_path") == str(Path(item.path))
                                and entry.get("status") in {"succeeded", "needs_review"}
                                for entry in results
                            )
                        )},
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
                        kpi={"translated_entities": translated_count, "kept_entities": kept_count, "file_count": len(results)},
                        terms=self._finish_terms(),
                    )
                )
        except Exception as exc:  # runner-level failures still become a task result
            batch_paths = self._write_batch_artifacts(status="error", files=results, issues=issues)
            self._process_artifacts.finish("failed")
            logger.opt(exception=exc).error(f"[CAD] 翻译流程失败：{exc!r}")
            self._queue.put(
                ErrorMsg(
                    message="CAD 翻译未能完成："
                    + _cad_failure_message(exc, fallback="出现了未预期的问题。"),
                    **batch_paths,
                    terms=self._finish_terms(),
                )
            )
        finally:
            if prefetch is not None:
                prefetch.close()


class _ProfilePrefetch:
    """「自动识别场景」：提前、同时请求后面几张图纸的图纸说明。

    图纸一张接一张翻译，每张开译前要先等模型通读、写好说明。这里在任务开始时
    就把后面每张图纸的通读请求按并发设置同时发出去；翻到那张时说明多半已经
    回来了，直接取用，并把当时的运行日志原样补上。

    提前读到的文字和翻译时取的不一样（或者没法提前读，比如没有转换缓存的
    DWG）时，就按原来的方式当场请求，结果不受影响。
    """

    def __init__(
        self,
        requester: Any,
        sources: list[Path],
        *,
        options: CadPipelineOptions,
        dxf_cache: Any,
        max_workers: int,
        stop_requested: Any,
        paused: Any,
    ) -> None:
        self._requester = requester
        self._options = options
        self._dxf_cache = dxf_cache
        self._stop_requested = stop_requested
        self._paused = paused
        # 读 DXF 很吃内存和 CPU：一次只读一张，模型请求才同时发。
        self._read_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, int(max_workers or 1)), thread_name_prefix="cad-profile"
        )
        self._futures: dict[str, Future] = {
            str(source): self._executor.submit(self._prefetch, source) for source in sources
        }

    def _dxf_for(self, source: Path) -> Path | None:
        if source.suffix.lower() == ".dxf":
            return source
        if self._dxf_cache is not None:
            return self._dxf_cache.lookup(source)
        return None

    def _prefetch(self, source: Path) -> tuple[list[str], list[str], str, list[tuple[str, str]]] | None:
        while self._paused() and not self._stop_requested():
            time.sleep(0.1)
        if self._stop_requested():
            return None
        dxf = self._dxf_for(source)
        if dxf is None:
            return None
        with self._read_lock:
            if self._stop_requested():
                return None
            inputs = read_profile_inputs(dxf, self._options)
        if inputs is None:
            return None
        texts, rows = inputs
        logs: list[tuple[str, str]] = []
        profile = self._requester(
            texts,
            lambda level, message: logs.append((level, message)),
            neighbor_rows=rows,
        )
        return texts, rows, str(profile or ""), logs

    def requester_for(self, source: Path) -> Any:
        """The ``profile_requester`` for this drawing's pipeline."""
        future = self._futures.get(str(source))

        def requester(texts: list[str], log: Any, *, neighbor_rows: Any = ()) -> str:
            prefetched = None
            if future is not None:
                try:
                    prefetched = wait_result(future, should_stop=self._stop_requested, default=None)
                except Exception as exc:  # noqa: BLE001 - fall back to asking now.
                    logger.debug(f"[CAD] 提前请求图纸说明失败：{exc!r}")
            if prefetched is not None:
                early_texts, early_rows, profile, logs = prefetched
                if early_texts == list(texts) and early_rows == list(neighbor_rows):
                    for level, message in logs:
                        log(level, message)
                    return profile
            return self._requester(texts, log, neighbor_rows=neighbor_rows)

        return requester

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
