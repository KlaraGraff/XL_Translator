"""A translation page registered at run time, for tests only (key ``ext:sample``).

It walks the generic path a format adapter will take: the registry
(``register_surface``), the descriptor (``GET /api/surfaces``), scan, start,
the runner messages and the task center, with no compiled table on either
side.  The runner calls no model: it copies each ``.txt`` file to the output
folder as is (upper-cased when the descriptor switch ``uppercase`` is on),
so a test can tell the switch value travelled with the request.

Never imported by the app: ``tests/`` is not shipped.  Register it from a
test, or start a sidecar for browser tests with
``.runtime/self-tests/ext-phase-a/serve.py --sample``.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from api.surfaces import get_surface, register_surface
from api.surfaces.base import PHASE_READ, PHASE_WRITE, OptionDef, RunnerRequest, Surface, require_target_lang
from api.task_contracts import Runner, TaskInputError, TaskOptions
from core import bilingual_writer
from core.file_progress import FileProgressReporter
from core.runner_base import BaseTaskRunner
from core.task_runner_messages import DoneMsg, ProgressMsg, StoppedMsg
from settings import AppSettings

SAMPLE_KEY = "ext:sample"


@dataclass
class SampleFileItem:
    path: Path
    name: str
    size_kb: float
    relative_path: str = ""
    format: str = "txt"


@dataclass
class SampleScanResult:
    root: Path
    items: list[SampleFileItem] = field(default_factory=list)
    skipped: list[Any] = field(default_factory=list)

    @property
    def summary(self) -> dict[str, int]:
        return {"scanned_count": len(self.items), "selected_count": len(self.items), "skipped_count": 0}

    @property
    def risk(self) -> dict[str, object]:
        return {}


def _scan(root: Path) -> list[SampleFileItem]:
    root = Path(root)
    paths = [root] if root.is_file() else sorted(root.rglob("*.txt"))
    base = root.parent if root.is_file() else root
    return [
        SampleFileItem(
            path=path,
            name=path.name,
            size_kb=round(path.stat().st_size / 1024, 1),
            relative_path=str(path.relative_to(base)),
        )
        for path in paths
        if path.suffix.lower() == ".txt" and "_翻译输出_" not in str(path)
    ]


class SampleRunner(BaseTaskRunner):
    _thread_name = "sample-translation"

    def __init__(self, request: RunnerRequest, *, delay: float = 0.0) -> None:
        self._init_runner_shell()
        self._files = list(request.files)
        self._source_root = request.source_root
        self._target_lang = request.settings.target_lang
        self._uppercase = bool((request.options.options or {}).get("uppercase"))
        self._delay = delay
        self._paused = threading.Event()
        self._key_overrides = request.key_overrides
        self._file_progress = FileProgressReporter(self._queue, self._files, request.source_root)

    # Pauses before the next file (``pause_mode = "immediate"``: the task shows
    # 已暂停 at once, the file in progress still finishes).
    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    def _run(self) -> None:
        started = time.monotonic()
        output_dir = bilingual_writer.build_output_dir(self._source_root)
        output_dir.mkdir(parents=True, exist_ok=True)
        total = len(self._files)
        self._file_progress.emit_many("waiting", "extract")
        self._queue.put(ProgressMsg(1, 2, "读取文件", total, total))
        results: list[dict[str, Any]] = []
        for index, item in enumerate(self._files, start=1):
            while self._paused.is_set() and not self._stop_event.is_set():
                time.sleep(0.05)
            if self._stop_event.is_set():
                self._file_progress.emit_many("unstarted", "not_started")
                self._queue.put(StoppedMsg(message="已停止", output_dir=str(output_dir), files=results))
                return
            self._file_progress.emit(item.path, "running", "generate")
            body = Path(item.path).read_text(encoding="utf-8")
            target = output_dir / f"{Path(item.path).stem}_{self._target_lang}.txt"
            target.write_text(body.upper() if self._uppercase else body, encoding="utf-8")
            if self._delay:
                time.sleep(self._delay)
            result = {"name": item.name, "output_path": str(target), "success": True, "status": "completed"}
            results.append(result)
            self._file_progress.emit(item.path, "generated", "done", result=result)
            self._log("OK", f"{item.name}：已写出")
            self._queue.put(ProgressMsg(2, 2, "生成文件", index, total))
        self._queue.put(
            DoneMsg(
                output_dir=str(output_dir),
                file_results=results,
                elapsed_sec=round(time.monotonic() - started, 2),
                tm_hit_count=0,
                api_call_count=0,
                files=results,
            )
        )


class SampleSurface(Surface):
    key = SAMPLE_KEY
    builtin = False
    label = "Sample translation"
    page_key = "adapter"
    target_lang_setting = "per_surface"
    display_label = {"zh": "示例文本", "en": "Sample text", "fr": "Texte d'exemple"}
    file_noun = {"zh": "文本文件", "en": "text files", "fr": "fichiers texte"}
    icon = "book"
    file_extensions = ("txt",)
    output_translation_only = True
    can_pause = True
    pause_mode = "immediate"
    can_resume = True
    phase_weights = (10, 90)
    phase_names = (PHASE_READ, PHASE_WRITE)
    options = (
        OptionDef(
            "uppercase",
            {"zh": "转为大写", "en": "Upper case", "fr": "Majuscules"},
            default=False,
            help={
                "zh": "写出时把文字转为大写（仅用于测试开关能否随任务发出）。",
                "en": "Write the text in upper case (tests that switches reach the task).",
                "fr": "Écrire le texte en majuscules (vérifie que les options arrivent à la tâche).",
            },
        ),
    )

    #: Seconds each file takes, so a browser test can watch a running task.
    delay: float = 0.0

    def scan(self, root: Path, options: TaskOptions, *, selected_paths=None) -> list[Any]:
        return _scan(root)

    def scan_sources(self, root: Path, request: Any) -> Any:
        return SampleScanResult(root=Path(root), items=_scan(root))

    def validate_preflight(self, *, files: list[Any], settings: AppSettings, options: TaskOptions) -> None:
        require_target_lang(settings.target_lang, "示例文本")
        if not files:
            raise TaskInputError("没有可处理的文本文件。")

    def fingerprint_extras(self, *, files: list[Any], options: TaskOptions) -> dict[str, object]:
        return {"options": dict(options.options or {})}

    def build_runner(self, request: RunnerRequest) -> Runner:
        return SampleRunner(request, delay=self.delay)


def register_sample_surface(*, delay: float = 0.0) -> SampleSurface:
    """Register ``ext:sample`` once; later calls return the registered one."""
    try:
        surface = get_surface(SAMPLE_KEY)
    except TaskInputError:
        surface = register_surface(SampleSurface())
    assert isinstance(surface, SampleSurface)
    surface.delay = delay
    return surface
