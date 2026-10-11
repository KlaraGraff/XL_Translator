"""Fold the model-request steps of one runner phase into a single progress value.

The 翻译 phase of Excel and Word is more than the main batches: before them the
source language is detected (one request per file), after them leftover source
text is repaired unit by unit, Word retries and re-checks single paragraphs, and
Excel translates sheet names.  Each of those waits on the model, often for
longer than the batches themselves.  Reporting only the batches left the bar
frozen at ~80% while those requests ran, sometimes for most of the task.

``PhaseProgress`` gives each step a fixed share of the phase and reports
``done / total`` inside that share.  A step that does not run this time is
simply passed over, and the value never goes backwards, so a step that turns
out to be empty costs a small jump instead of a stall.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence

from core.task_runner_messages import ProgressMsg

# step_done / step_total are only used to compute a percentage downstream, so a
# fixed fine-grained scale is enough and keeps every step on one axis.
_RESOLUTION = 1000


class PhaseProgress:
    def __init__(
        self,
        emit: Callable[[ProgressMsg], None],
        *,
        phase_index: int,
        phase_total: int,
        phase_name: str,
        steps: Sequence[tuple[str, float]],
    ) -> None:
        total_share = sum(share for _, share in steps) or 1.0
        self._emit = emit
        self._phase_index = phase_index
        self._phase_total = phase_total
        self._phase_name = phase_name
        self._ranges: dict[str, tuple[float, float]] = {}
        start = 0.0
        for name, share in steps:
            width = share / total_share
            self._ranges[name] = (start, width)
            start += width
        self._lock = threading.Lock()
        self._reached = -1

    def report(self, step: str, done: int, total: int) -> None:
        """Report ``done`` of ``total`` units of ``step`` (an unknown step is ignored)."""
        span = self._ranges.get(step)
        if span is None:
            return
        start, width = span
        fraction = start + width * min(1.0, max(0.0, done / max(1, total)))
        self._send(int(round(fraction * _RESOLUTION)))

    def finish(self) -> None:
        self._send(_RESOLUTION)

    def _send(self, value: int) -> None:
        with self._lock:
            # Concurrent batch callbacks can arrive out of order; never step back,
            # and skip repeats (the Word recovery wait polls every 0.1 s).
            value = min(_RESOLUTION, max(0, value))
            if value <= self._reached:
                return
            self._reached = value
            self._emit(
                ProgressMsg(
                    phase_index=self._phase_index,
                    phase_total=self._phase_total,
                    phase_name=self._phase_name,
                    step_done=value,
                    step_total=_RESOLUTION,
                )
            )
