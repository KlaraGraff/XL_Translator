"""Background, cancellable TM-cleaning runner for the unified task center."""

from __future__ import annotations

import queue
import threading
import time
from typing import Any

from core.engine_dispatcher import build_engine
from core.model_roles import ROLE_CLEANER, resolve_effective_model_config, settings_for_text_role
from core.model_throughput import get_model_throughput
from core.task_runner import DoneMsg, ErrorMsg, LogMsg, ProgressMsg, StatusMsg, StoppedMsg
from core.tm_cleaner import run_cleaning
from settings import AppSettings, provider_key_overrides


class TmCleaningTaskRunner:
    """Turn suggestion-only TM cleaning into a normal managed background task."""

    def __init__(
        self,
        *,
        lang_pair: str,
        settings: AppSettings,
        key_overrides: dict[str, str] | None = None,
        api_scheduler: Any = None,
    ) -> None:
        self._lang_pair = str(lang_pair or "").strip()
        self._settings = settings
        self._key_overrides = dict(key_overrides or {})
        self._api_scheduler = api_scheduler
        self._queue: queue.Queue[Any] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_with_overrides, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def needs_poll(self) -> bool:
        return bool(self._thread and self._thread.is_alive()) or not self._queue.empty()

    def get_message(self, timeout: float = 0.05) -> Any:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def _run_with_overrides(self) -> None:
        with provider_key_overrides(self._key_overrides):
            self._run()

    def _run(self) -> None:
        started_at = time.monotonic()
        try:
            self._queue.put(StatusMsg("状态：正在准备 TM 清洗建议..."))
            effective = resolve_effective_model_config(self._settings, ROLE_CLEANER)
            throughput = get_model_throughput(self._settings, effective)
            clean_settings = settings_for_text_role(self._settings, ROLE_CLEANER)
            suggestions = run_cleaning(
                self._lang_pair,
                build_engine(clean_settings),
                batch_size=throughput.batch_size or clean_settings.engine.batch_size,
                concurrency=throughput.concurrency,
                extra_prompt=self._settings.cleaner_prompt_extras.get(self._lang_pair, ""),
                full_override_prompt=self._settings.cleaner_full_prompt_overrides.get(
                    self._lang_pair, ""
                ),
                custom_target_langs=self._settings.custom_target_langs,
                cancel_event=self._stop_event,
                api_scheduler=self._api_scheduler,
                progress_callback=self._on_progress,
            )
            elapsed = time.monotonic() - started_at
            if self._stop_event.is_set():
                self._queue.put(
                    StoppedMsg(
                        message="TM 清洗已安全停止，未应用任何建议。",
                        kpi={"suggestion_count": len(suggestions), "elapsed_sec": elapsed},
                    )
                )
                return
            # Suggestions themselves include TM source/target contents and
            # must remain in the dedicated TM review workflow, not task logs.
            self._queue.put(
                DoneMsg(
                    output_dir="",
                    file_results=[],
                    elapsed_sec=elapsed,
                    tm_hit_count=0,
                    api_call_count=0,
                    kpi={"suggestion_count": len(suggestions), "elapsed_sec": elapsed},
                    language={"lang_pair": self._lang_pair},
                )
            )
        except Exception as exc:  # noqa: BLE001 - report via managed task SSE.
            self._queue.put(ErrorMsg(message=str(exc) or exc.__class__.__name__))

    def _on_progress(self, payload: dict[str, Any]) -> None:
        self._queue.put(
            ProgressMsg(
                phase_index=1,
                phase_total=1,
                phase_name="TM 清洗",
                step_done=int(payload.get("done") or 0),
                step_total=max(1, int(payload.get("total") or 1)),
            )
        )
        self._queue.put(LogMsg(level="INFO", message="TM 清洗批次进度已更新", visible=False))
