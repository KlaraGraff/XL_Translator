"""Qt workers for filesystem and long-running native UI actions."""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from core.file_scanner import scan_path
from core.pdf_image_translation import scan_pdf_path
from core.word_document import scan_word_path
from core.tm_cleaner import apply_suggestions, run_cleaning
from core.engine_dispatcher import build_engine, get_batch_size
from core.model_roles import ROLE_CLEANER, resolve_effective_model_config, settings_for_text_role
from core.model_throughput import get_model_throughput
from core.update_checker import check_for_updates


class UpdateCheckWorker(QThread):
    """Run update checks away from the GUI thread."""

    resultReady = Signal(object)

    def run(self) -> None:
        self.resultReady.emit(check_for_updates())


class ScanWorker(QThread):
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
            self.finished.emit(items, str(source_root), "")
        except Exception as exc:  # noqa: BLE001 - converted to UI message.
            self.finished.emit([], "", str(exc))


class WordScanWorker(QThread):
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
            self.finished.emit(items, str(source_root), "")
        except Exception as exc:  # noqa: BLE001 - converted to UI message.
            self.finished.emit([], "", str(exc))


class PdfScanWorker(QThread):
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
            self.finished.emit(items, str(source_root), "")
        except Exception as exc:  # noqa: BLE001 - converted to UI message.
            self.finished.emit([], "", str(exc))


class TmCleanWorker(QThread):
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
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

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
                progress_callback=self.progress.emit,
                extra_prompt=self._settings.cleaner_prompt_extras.get(self._lang_pair, ""),
                full_override_prompt=self._settings.cleaner_full_prompt_overrides.get(
                    self._lang_pair,
                    "",
                ),
                custom_target_langs=self._settings.custom_target_langs,
                cancel_event=self._cancel_event,
            )
            if self._cancel_event.is_set():
                self.finished.emit(suggestions, "清洗已中止，未继续提交后续批次。", False)
                return
            if self._overwrite:
                applied = apply_suggestions(
                    suggestions,
                    auto_pin=self._settings.auto_pin_after_clean,
                )
                self.finished.emit(suggestions, f"已直接写入 {applied} 条清洗建议。", True)
                return
            self.finished.emit(suggestions, "", True)
        except Exception as exc:  # noqa: BLE001 - converted to UI message.
            self.finished.emit([], str(exc), False)
