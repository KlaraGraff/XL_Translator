"""The thread-and-queue shell every task runner shares.

A runner does its work on one background thread and reports through a
message queue: ``api.task_manager`` pumps ``get_message()`` while
``needs_poll()`` is true and turns each message into a task event.  Excel,
Word, PDF, CAD and TM cleaning used to each carry their own copy of that
shell; it lives here now so a new surface only writes ``_run()``.

Subclasses set ``self._queue``, ``self._thread`` and ``self._stop_event`` in
their ``__init__`` (``_init_runner_shell()`` does exactly that) and implement
``_run()``.  Optional capabilities stay optional: the task manager offers
pause, resume and end-paused only to runners that define ``pause()``,
``resume()`` and ``end_paused()``.
"""

from __future__ import annotations

import queue
import threading
from typing import Any

from loguru import logger

from core.prompt_cache import usage_metering
from core.task_runner_messages import LogMsg
from settings import provider_key_overrides


class BaseTaskRunner:
    """Background thread + message queue; subclasses implement ``_run()``."""

    # ``None`` keeps Python's default ``Thread-N`` name.
    _thread_name: str | None = None
    # Run ``_run()`` inside ``provider_key_overrides(self._key_overrides)`` so
    # the task's frozen connection keys win over keys.json for its lifetime.
    _uses_key_overrides = True

    _queue: queue.Queue
    _thread: threading.Thread | None
    _stop_event: threading.Event

    def _init_runner_shell(self) -> None:
        self._queue = queue.Queue()
        self._thread = None
        self._stop_event = threading.Event()

    # ── lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        self._before_start()
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._thread_main, daemon=True, name=self._thread_name
        )
        self._thread.start()

    def _before_start(self) -> None:
        """Reset subclass state (pause flags, stop reasons) before a start."""

    def _thread_main(self) -> None:
        # 任务里所有模型请求读回的缓存命中数，由 core.request_log 按翻译步骤写进日志。
        with usage_metering():
            if self._uses_key_overrides:
                with provider_key_overrides(getattr(self, "_key_overrides", None) or {}):
                    self._run()
            else:
                self._run()

    def _run(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        self._stop_event.set()

    def stop_requested(self) -> bool:
        return self._stop_event.is_set()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ── messages ─────────────────────────────────────────────────────────

    def needs_poll(self) -> bool:
        """Thread alive OR messages still queued, so a final DoneMsg is never missed."""
        return self.is_running() or not self._queue.empty()

    def get_message(self, timeout: float = 0.05) -> Any:
        """Next queued message, or ``None`` when nothing arrives within ``timeout``."""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def _log(self, level: str, message: str) -> None:
        self._queue.put(LogMsg(level=level, message=message))
        logger.info(f"[{level}] {message}")

    def _retire_stale_terms(self) -> None:
        """本次重译的过期整段词条，在新译文入库前删掉（见 TaskTermSession.retire_stale_entries）。"""
        session = getattr(self, "_term_session", None)
        if session is not None:
            session.retire_stale_entries()

    def _finish_terms(self) -> dict[str, object]:
        """记忆库词条的采用情况（结果里的 ``terms``）；整个任务只算一次。

        停止、失败、完成三条收尾路都会走到这里；使用次数只能记一次。任务没走到
        查记忆库那一步时返回空字典。
        """
        cached = getattr(self, "_term_result", None)
        if cached is not None:
            return cached
        session = getattr(self, "_term_session", None)
        memory = getattr(self, "_term_memory", None)
        result: dict[str, object] = {}
        if memory is not None:
            # 一份文件一个会话的路径（CAD、PDF 原位替换）：见 core.term_session.DocumentTermMemory。
            try:
                result = memory.finish()
            except Exception as exc:  # noqa: BLE001 - statistics never fail a finished task
                logger.warning(f"汇总记忆库结果失败：{exc!r}")
                result = {}
        elif session is not None:
            pair_for_text = getattr(self, "_term_pair_for_text", None) or (lambda _text: None)
            result = session.finish(
                pair_for_text=pair_for_text,
                locations_for_text=getattr(self, "_term_locations_for_text", None),
            )
        self._term_result = result
        return result
