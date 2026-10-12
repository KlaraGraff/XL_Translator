"""Progress of a batch run that counts the requests still in flight.

``translate_texts`` reports progress only when a batch comes back.  With many
batches sent at once they tend to come back together, so the count sits still
for a minute and then jumps most of the way.  This tracker listens to the
per-batch ``BatchRequestEvent`` stream and adds an estimate for each request
still waiting, based on how long the finished ones took, so a progress bar can
move while the requests are out.  The estimate for an unfinished request never
reaches its full size: only a real return completes it.
"""

from __future__ import annotations

import math
import statistics
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

# 逐批请求事件（translate_texts 的 request_callback）。
BATCH_EVENT_PLAN = "plan"
BATCH_EVENT_SENT = "sent"
BATCH_EVENT_DONE = "done"


@dataclass(frozen=True)
class BatchRequestEvent:
    """One batch request's lifecycle, for run logs and in-flight progress.

    ``progress_callback`` only fires when a batch comes back, so with many
    batches in flight at once the count sits still and then jumps.  These events
    also say when each request leaves and how long it took, which is what a
    caller needs to log the requests and to estimate the ones still in flight.

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


# Share of a request's size an unfinished request can be credited with.
_IN_FLIGHT_CAP = 0.9
# Before anything has come back there is no duration to go by; this only sets
# how fast the estimate starts to creep, and it never reaches the cap.
_PRIOR_SECONDS = 20.0
_RECENT_DURATIONS = 20


class InFlightRequestTracker:
    """Thread-safe bookkeeping of sent / returned batch requests."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._in_flight: dict[int, tuple[float, int]] = {}
        self._durations: deque[float] = deque(maxlen=_RECENT_DURATIONS)
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
                self._in_flight[event.index] = (now, event.items)
            elif event.kind == BATCH_EVENT_DONE:
                self._in_flight.pop(event.index, None)
                self._done_items += event.items
                self._failed_items += event.failed
                self._last_return_at = now
                # A stopped batch was never sent; its "duration" says nothing
                # about the service.
                if event.stopped < event.items and event.elapsed > 0:
                    self._durations.append(event.elapsed)

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

    def estimated_items(self) -> float:
        """Returned items plus a partial credit for each request still out."""
        now = self._clock()
        with self._lock:
            done = max(self._done_items, self._reported_done)
            if not self._in_flight:
                return float(done)
            typical = statistics.median(self._durations) if self._durations else None
            credit = 0.0
            for started, items in self._in_flight.values():
                elapsed = max(0.0, now - started)
                if typical:
                    # Most of the way by the typical duration, then a slow
                    # approach to the cap for the slow ones.
                    share = 1.0 - math.exp(-1.5 * elapsed / max(0.5, typical))
                else:
                    # Nothing has come back yet: creep without a known end.
                    share = elapsed / (elapsed + _PRIOR_SECONDS)
                credit += items * _IN_FLIGHT_CAP * share
            return done + credit
