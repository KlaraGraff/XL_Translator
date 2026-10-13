"""Run-log lines for the model requests of one translation step.

Excel, Word and CAD all send their text to the model in batches.  The task
log used to say only when that step started and when it ended, so a large
file sat on 「开始翻译」 for minutes with no sign of what it was waiting for.
``BatchRequestLog`` turns the ``BatchRequestEvent`` stream of
``translate_texts`` / ``translate_word_texts`` into the same lines on every
page: how the text is split, every batch that comes back with its time, and
how many batches are still out when none has returned for a while.

It only writes the log.  Progress stays where it was, counted from returned
batches (``InFlightRequestTracker.done_items``), never from requests still on
the way (maple 2026-10-12：路上也加是造假).
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable

from core.prompt_cache import UsageTotals, current_meter, format_usage_line
from core.request_progress import (
    BATCH_EVENT_DONE,
    BATCH_EVENT_PLAN,
    BatchRequestEvent,
    InFlightRequestTracker,
)

# 多久没有请求返回就在运行日志里说一声（每隔 TICK_SECONDS 看一次）。
TICK_SECONDS = 0.5
WAIT_LOG_SECONDS = 30.0

LogFn = Callable[[str, str], None]  # (level, message)

BATCH_WAIT_TEMPLATE = "正在等待 {waiting} 批请求返回，已等待 {seconds} 秒"
# PDF 整页图像翻译按页发请求。
PDF_PAGE_WAIT_TEMPLATE = "正在等待 {waiting} 页生成结果，已等待 {seconds} 秒"
_REQUEST_LINE_RE = re.compile(
    r"开始翻译 \d+ \S+（相同内容只翻译一次）：分 \d+ 批发送"
    r"|第 \d+/\d+ 批已返回："
    r"|正在等待 \d+ 批请求返回"
)


class BatchRequestLog:
    """Write one translation step's batch requests to the run log.

    ``unit`` counts the entries (Excel / Word「处」, CAD「条」); ``subject`` names
    them in the opening line (「开始翻译 8 条文字」/「开始翻译 8 处内容」).
    Use it as a context manager around the translate call so the waiting
    notice runs only while requests can be out.
    """

    def __init__(
        self,
        log: LogFn,
        *,
        unit: str = "条",
        subject: str = "文字",
        tracker: InFlightRequestTracker | None = None,
        tick_seconds: float = TICK_SECONDS,
        wait_log_seconds: float = WAIT_LOG_SECONDS,
        thread_name: str = "translate-wait-log",
    ) -> None:
        self._log = log
        self._unit = unit
        self._subject = subject
        self.tracker = tracker or InFlightRequestTracker()
        self._notice = RequestWaitNotice(
            log,
            self.tracker,
            tick_seconds=tick_seconds,
            wait_log_seconds=wait_log_seconds,
            thread_name=thread_name,
        )
        self._usage_meter = None
        self._usage_start = UsageTotals()

    def on_event(self, event: BatchRequestEvent) -> None:
        """The ``request_callback`` of the translate call (any thread)."""
        self.tracker.on_event(event)
        unit = self._unit
        if event.kind == BATCH_EVENT_PLAN:
            self._log(
                "INFO",
                f"开始翻译 {event.items} {unit}{self._subject}（相同内容只翻译一次）："
                f"分 {event.batch_total} 批发送，每次同时发送 {event.concurrency} 批",
            )
        elif event.kind == BATCH_EVENT_DONE and event.stopped < event.items:
            # 整批因为停止没发出去的不记：它没有「返回」可说。
            seconds = f"{event.elapsed:.1f}"
            head = f"第 {event.index}/{event.batch_total} 批已返回："
            if event.failed:
                self._log(
                    "WARN",
                    f"{head}{event.items} {unit}中 {event.failed} {unit}未获得译文，用时 {seconds} 秒",
                )
            else:
                self._log("INFO", f"{head}{event.items} {unit}，用时 {seconds} 秒")

    def start(self) -> None:
        # 这一步发出的请求读回了多少缓存命中：开始时记下任务计数器的读数，结束时写差值。
        self._usage_meter = current_meter()
        if self._usage_meter is not None:
            self._usage_start = self._usage_meter.totals()
        self._notice.start()

    def stop(self) -> None:
        self._notice.stop()
        meter, self._usage_meter = self._usage_meter, None
        if meter is not None:
            line = format_usage_line(meter.totals().minus(self._usage_start))
            if line:
                self._log("INFO", line)

    def __enter__(self) -> BatchRequestLog:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()


def is_request_log_line(message: str) -> bool:
    """True for the batch lines ``BatchRequestLog`` writes (plan, returns, waiting).

    A runner that relays another runner's log selectively (the PDF「转 Word 翻译」
    route relays only warnings from the Word run) uses this to pass these on too.
    """
    return bool(_REQUEST_LINE_RE.match(str(message or "")))


class RequestWaitNotice:
    """Log a line while requests are out and none has come back for a while.

    ``tracker`` is the step's ``InFlightRequestTracker``; ``template`` gets
    ``waiting`` (requests still out) and ``seconds`` (since the last return).
    """

    def __init__(
        self,
        log: LogFn,
        tracker: InFlightRequestTracker,
        *,
        template: str = BATCH_WAIT_TEMPLATE,
        tick_seconds: float = TICK_SECONDS,
        wait_log_seconds: float = WAIT_LOG_SECONDS,
        thread_name: str = "translate-wait-log",
    ) -> None:
        self._log = log
        self._tracker = tracker
        self._template = template
        self._tick_seconds = tick_seconds
        self._wait_log_seconds = wait_log_seconds
        self._thread_name = thread_name
        self._stop = threading.Event()
        self._ticker: threading.Thread | None = None

    def start(self) -> None:
        if self._ticker is not None:
            return
        self._stop.clear()
        self._ticker = threading.Thread(target=self._tick, name=self._thread_name, daemon=True)
        self._ticker.start()

    def stop(self) -> None:
        self._stop.set()
        if self._ticker is not None:
            self._ticker.join(timeout=2)
            self._ticker = None

    def __enter__(self) -> RequestWaitNotice:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    def _tick(self) -> None:
        next_wait_log = self._wait_log_seconds
        while not self._stop.wait(self._tick_seconds):
            waiting = self._tracker.in_flight_count()
            quiet = self._tracker.seconds_since_last_return()
            if not waiting or quiet < next_wait_log:
                if quiet < self._wait_log_seconds:
                    next_wait_log = self._wait_log_seconds
                continue
            self._log("INFO", self._template.format(waiting=waiting, seconds=int(quiet)))
            next_wait_log = quiet + self._wait_log_seconds
