"""Qt workers for filesystem and long-running native UI actions."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Hashable, Iterable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from core.file_scanner import scan_path
from core.pdf_image_translation import scan_pdf_path
from core.word_document import scan_word_path
from core.tm_cleaner import apply_suggestions, run_cleaning
from core.engine_dispatcher import build_engine, get_batch_size
from core.model_roles import ROLE_CLEANER, resolve_effective_model_config, settings_for_text_role
from core.model_throughput import get_model_throughput
from core.update_checker import check_for_updates


@dataclass(frozen=True)
class TaskResourceReservation:
    """Immutable view of one task's reserved upstream resources."""

    owner_key: str
    owner_label: str
    resources: frozenset[Hashable]
    conservative: bool = False


class TaskResourceLease:
    """Idempotent handle returned by :class:`TaskResourceRegistry`."""

    def __init__(self, registry: "TaskResourceRegistry", token: str):
        self._registry = registry
        self._token = token
        self._release_lock = threading.Lock()
        self._released = False

    @property
    def released(self) -> bool:
        with self._release_lock:
            return self._released

    def release(self) -> bool:
        """Release once and report whether this call owned the release."""

        with self._release_lock:
            if self._released:
                return False
            self._released = True
        self._registry._release(self._token)
        return True


class TaskResourceRegistry:
    """Atomically reserve model API groups for native background tasks.

    An empty or unknown resource set is treated conservatively: it conflicts
    with every other task. Known disjoint API groups may run concurrently.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._reservations: dict[str, TaskResourceReservation] = {}

    def acquire(
        self,
        *,
        owner_key: str,
        owner_label: str,
        resources: Iterable[Hashable] | None,
    ) -> TaskResourceLease | None:
        resource_set = frozenset(resources or ())
        candidate = TaskResourceReservation(
            owner_key=str(owner_key or "").strip(),
            owner_label=str(owner_label or "").strip() or "其他任务",
            resources=resource_set,
            conservative=not resource_set,
        )
        with self._lock:
            if any(self._conflicts(candidate, held) for held in self._reservations.values()):
                return None
            token = uuid.uuid4().hex
            self._reservations[token] = candidate
        return TaskResourceLease(self, token)

    def reservations(self) -> tuple[TaskResourceReservation, ...]:
        with self._lock:
            return tuple(self._reservations.values())

    def release_all(self) -> None:
        with self._lock:
            self._reservations.clear()

    def _release(self, token: str) -> None:
        with self._lock:
            self._reservations.pop(token, None)

    @staticmethod
    def _conflicts(
        candidate: TaskResourceReservation,
        held: TaskResourceReservation,
    ) -> bool:
        return bool(
            candidate.conservative
            or held.conservative
            or candidate.resources.intersection(held.resources)
        )


class DaemonWorker(QObject):
    """QObject signal surface backed by a daemon Python thread.

    A blocked filesystem or network operation must not make Qt abort while its
    owning window is being destroyed. Cancellation remains cooperative, and
    ``wait`` is always controlled by the caller's explicit timeout.
    """

    threadFinished = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cancel_event = threading.Event()
        self._thread_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._cancel_event.clear()
            thread = threading.Thread(target=self._run_guarded, daemon=True)
            self._thread = thread
        thread.start()

    def _run_guarded(self) -> None:
        try:
            self.run()
        finally:
            with self._thread_lock:
                self._thread = None
            self._safe_emit(self.threadFinished)

    def run(self) -> None:
        raise NotImplementedError

    def cancel(self) -> None:
        self._cancel_event.set()

    def quit(self) -> None:
        self.cancel()

    def requestInterruption(self) -> None:  # noqa: N802 - QThread compatibility.
        self.cancel()

    def isInterruptionRequested(self) -> bool:  # noqa: N802 - QThread compatibility.
        return self._cancel_event.is_set()

    def isRunning(self) -> bool:  # noqa: N802 - QThread compatibility.
        with self._thread_lock:
            return self._thread is not None and self._thread.is_alive()

    def wait(self, timeout_ms: int = 0) -> bool:
        with self._thread_lock:
            thread = self._thread
        if thread is None:
            return True
        if thread is threading.current_thread():
            return False
        thread.join(max(0, int(timeout_ms)) / 1000)
        return not thread.is_alive()

    def dispose(self, timeout_ms: int = 250) -> bool:
        """Cancel and detach without waiting longer than the caller allows."""

        self.cancel()
        stopped = self.wait(timeout_ms)
        if self.parent() is not None:
            self.setParent(None)
        if stopped:
            self.deleteLater()
        return stopped

    @staticmethod
    def _safe_emit(signal, *args: object) -> None:
        try:
            signal.emit(*args)
        except RuntimeError:
            # The window can be destroyed while an uncancellable call unwinds.
            pass


class UpdateCheckWorker(DaemonWorker):
    """Run update checks away from the GUI thread."""

    resultReady = Signal(object)

    def run(self) -> None:
        result = check_for_updates()
        if not self._cancel_event.is_set():
            self._safe_emit(self.resultReady, result)


class CallableWorker(DaemonWorker):
    """Run one blocking callable without occupying the Qt event loop."""

    resultReady = Signal(object)

    def __init__(self, fn: Callable[[], object], parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:
        try:
            result = self._fn()
        except Exception as exc:  # noqa: BLE001 - delivered to the UI callback.
            result = exc
        if not self._cancel_event.is_set():
            self._safe_emit(self.resultReady, result)


class ScanWorker(DaemonWorker):
    """Run source scanning away from the GUI thread."""

    finished = Signal(object, str, str)

    def __init__(self, raw_path: str, parent=None):
        super().__init__(parent)
        self._raw_path = raw_path

    def run(self) -> None:
        try:
            input_path = Path(self._raw_path).expanduser()
            items = scan_path(input_path)
            source_root = input_path if input_path.is_dir() else input_path.parent
            if not self._cancel_event.is_set():
                self._safe_emit(self.finished, items, str(source_root), "")
        except Exception as exc:  # noqa: BLE001 - converted to UI message.
            if not self._cancel_event.is_set():
                self._safe_emit(self.finished, [], "", str(exc))


class WordScanWorker(DaemonWorker):
    """Run Word source scanning away from the GUI thread."""

    finished = Signal(object, str, str)

    def __init__(self, raw_path: str, parent=None):
        super().__init__(parent)
        self._raw_path = raw_path

    def run(self) -> None:
        try:
            input_path = Path(self._raw_path).expanduser()
            items = scan_word_path(input_path)
            source_root = input_path if input_path.is_dir() else input_path.parent
            if not self._cancel_event.is_set():
                self._safe_emit(self.finished, items, str(source_root), "")
        except Exception as exc:  # noqa: BLE001 - converted to UI message.
            if not self._cancel_event.is_set():
                self._safe_emit(self.finished, [], "", str(exc))


class PdfScanWorker(DaemonWorker):
    """Run PDF source scanning away from the GUI thread."""

    finished = Signal(object, str, str)

    def __init__(self, raw_path: str, parent=None, *, include_images: bool = False):
        super().__init__(parent)
        self._raw_path = raw_path
        self._include_images = include_images

    def run(self) -> None:
        try:
            input_path = Path(self._raw_path).expanduser()
            items = scan_pdf_path(input_path, include_images=self._include_images)
            source_root = input_path if input_path.is_dir() else input_path.parent
            if not self._cancel_event.is_set():
                self._safe_emit(self.finished, items, str(source_root), "")
        except Exception as exc:  # noqa: BLE001 - converted to UI message.
            if not self._cancel_event.is_set():
                self._safe_emit(self.finished, [], "", str(exc))


class TmCleanWorker(DaemonWorker):
    """Run TM deep cleaning away from the GUI thread."""

    progress = Signal(object)
    finished = Signal(object, str, bool)

    def __init__(
        self,
        *,
        lang_pair: str,
        settings,
        overwrite: bool,
        parent=None,
    ):
        super().__init__(parent)
        self._lang_pair = lang_pair
        self._settings = settings
        self._overwrite = overwrite

    def run(self) -> None:
        try:
            clean_settings = settings_for_text_role(self._settings, ROLE_CLEANER)
            engine = build_engine(clean_settings)
            config = resolve_effective_model_config(self._settings, ROLE_CLEANER)
            throughput = get_model_throughput(self._settings, config)
            suggestions = run_cleaning(
                self._lang_pair,
                engine,
                batch_size=throughput.batch_size or get_batch_size(clean_settings),
                concurrency=throughput.concurrency,
                progress_callback=lambda payload: self._safe_emit(self.progress, payload),
                extra_prompt=self._settings.cleaner_prompt_extras.get(self._lang_pair, ""),
                full_override_prompt=self._settings.cleaner_full_prompt_overrides.get(
                    self._lang_pair,
                    "",
                ),
                custom_target_langs=self._settings.custom_target_langs,
                cancel_event=self._cancel_event,
            )
            if self._cancel_event.is_set():
                self._safe_emit(
                    self.finished,
                    suggestions,
                    "清洗已中止，未继续提交后续批次。",
                    False,
                )
                return
            if self._overwrite:
                applied = apply_suggestions(
                    suggestions,
                    auto_pin=self._settings.auto_pin_after_clean,
                )
                self._safe_emit(
                    self.finished,
                    suggestions,
                    f"已直接写入 {applied} 条清洗建议。",
                    True,
                )
                return
            self._safe_emit(self.finished, suggestions, "", True)
        except Exception as exc:  # noqa: BLE001 - converted to UI message.
            self._safe_emit(self.finished, [], str(exc), False)
