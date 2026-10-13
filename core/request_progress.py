"""Per-batch request events of a translation run, and their bookkeeping.

``translate_texts`` reports progress only when a batch comes back.  These events
also say when each request leaves and how long it took, so a runner can log the
requests and tell the user how many are still waiting.  Progress itself is only
ever counted from returned batches.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

from loguru import logger

# 逐批请求事件（translate_texts 的 request_callback）。
BATCH_EVENT_PLAN = "plan"
BATCH_EVENT_SENT = "sent"
BATCH_EVENT_DONE = "done"


@dataclass(frozen=True)
class BatchRequestEvent:
    """One batch request's lifecycle, for the run log.

    ``progress_callback`` only fires when a batch comes back.  These events also
    say when each request leaves and how long it took, so a caller can log the
    requests and say how many are still waiting.

    ``plan``: ``batch_total`` / ``items`` / ``concurrency`` describe the run.
    ``sent``: batch ``index`` (1-based) with ``items`` entries is starting.
    ``done``: it returned after ``elapsed`` seconds; ``failed`` entries kept the
    source text (the API gave up on them), ``stopped`` were never sent because
    the user stopped the task.
    """

    kind: str
    index: int = 0
    batch_total: int = 0
    items: int = 0
    concurrency: int = 0
    elapsed: float = 0.0
    failed: int = 0
    stopped: int = 0


def notify_batch_event(callback: Callable[[BatchRequestEvent], None] | None, event: BatchRequestEvent) -> None:
    """Hand ``event`` to ``callback``; a broken log hook must never break translation."""
    if callback is None:
        return
    try:
        callback(event)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"批次请求事件回调出错：{exc!r}")


class InFlightRequestTracker:
    """Thread-safe bookkeeping of sent / returned batch requests."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._in_flight: dict[int, int] = {}
        self._done_items = 0
        self._reported_done = 0
        self._failed_items = 0
        self.batch_total = 0
        self.concurrency = 0
        self.planned_items = 0
        self._last_return_at = self._clock()

    def on_event(self, event: BatchRequestEvent) -> None:
        now = self._clock()
        with self._lock:
            if event.kind == BATCH_EVENT_PLAN:
                self.batch_total = event.batch_total
                self.concurrency = event.concurrency
                self.planned_items = event.items
                self._last_return_at = now
            elif event.kind == BATCH_EVENT_SENT:
                self._in_flight[event.index] = event.items
            elif event.kind == BATCH_EVENT_DONE:
                self._in_flight.pop(event.index, None)
                self._done_items += event.items
                self._failed_items += event.failed
                self._last_return_at = now

    def note_reported_done(self, done: int) -> None:
        """Take the plain ``progress_callback`` count when no events arrive."""
        with self._lock:
            self._reported_done = max(self._reported_done, int(done))

    @property
    def failed_items(self) -> int:
        with self._lock:
            return self._failed_items

    def done_items(self) -> int:
        with self._lock:
            return max(self._done_items, self._reported_done)

    def in_flight_count(self) -> int:
        with self._lock:
            return len(self._in_flight)

    def seconds_since_last_return(self) -> float:
        with self._lock:
            return max(0.0, self._clock() - self._last_return_at)
