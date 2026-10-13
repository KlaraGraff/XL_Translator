"""逐批运行日志（core.request_log）：Excel、Word、CAD 共用同一套写法。

maple 2026-10-13：Excel、Word 翻译时日志只有「开始翻译」和「翻译完成」，中间看不出
进展，要和 CAD 一样写清每批的发出、返回和用时。进度条仍只按真实返回计。
"""

from __future__ import annotations

import threading
import time

from core.engine_dispatcher import translate_texts_with_sources
from core.language_preflight import TranslationLanguageResult
from core.request_log import (
    PDF_PAGE_WAIT_TEMPLATE,
    BatchRequestLog,
    RequestWaitNotice,
    is_request_log_line,
)
from core.request_progress import (
    BATCH_EVENT_DONE,
    BATCH_EVENT_PLAN,
    BATCH_EVENT_SENT,
    BatchRequestEvent,
    InFlightRequestTracker,
)
from core.word_batching import translate_word_texts
from engines.base_engine import TranslationEngine
from settings import WordBatchSettings


class _Lines:
    def __init__(self) -> None:
        self.items: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def __call__(self, level: str, message: str) -> None:
        with self._lock:
            self.items.append((level, message))

    @property
    def texts(self) -> list[str]:
        with self._lock:
            return [message for _, message in self.items]


def test_lines_use_the_page_unit():
    lines = _Lines()
    log = BatchRequestLog(lines, unit="处", subject="内容")
    log.on_event(BatchRequestEvent(BATCH_EVENT_PLAN, batch_total=3, items=25, concurrency=2))
    log.on_event(BatchRequestEvent(BATCH_EVENT_SENT, index=1, batch_total=3, items=10))
    log.on_event(BatchRequestEvent(BATCH_EVENT_DONE, index=1, batch_total=3, items=10, elapsed=3.24))
    log.on_event(BatchRequestEvent(BATCH_EVENT_DONE, index=2, batch_total=3, items=10, elapsed=4.0, failed=2))
    # 停止后整批没发出去：没有「返回」可写。
    log.on_event(BatchRequestEvent(BATCH_EVENT_DONE, index=3, batch_total=3, items=5, stopped=5))

    assert lines.items == [
        ("INFO", "开始翻译 25 处内容（相同内容只翻译一次）：分 3 批发送，每次同时发送 2 批"),
        ("INFO", "第 1/3 批已返回：10 处，用时 3.2 秒"),
        ("WARN", "第 2/3 批已返回：10 处中 2 处未获得译文，用时 4.0 秒"),
    ]
    assert log.tracker.done_items() == 25


def test_waiting_notice_only_while_batches_are_out():
    lines = _Lines()
    log = BatchRequestLog(lines, tick_seconds=0.01, wait_log_seconds=0.1)
    with log:
        log.on_event(BatchRequestEvent(BATCH_EVENT_PLAN, batch_total=2, items=8, concurrency=2))
        log.on_event(BatchRequestEvent(BATCH_EVENT_SENT, index=1, batch_total=2, items=4))
        log.on_event(BatchRequestEvent(BATCH_EVENT_SENT, index=2, batch_total=2, items=4))
        time.sleep(0.3)
        log.on_event(BatchRequestEvent(BATCH_EVENT_DONE, index=1, batch_total=2, items=4, elapsed=0.3))
        log.on_event(BatchRequestEvent(BATCH_EVENT_DONE, index=2, batch_total=2, items=4, elapsed=0.3))
        waiting_before_idle = [text for text in lines.texts if text.startswith("正在等待")]
        time.sleep(0.3)
    waiting = [text for text in lines.texts if text.startswith("正在等待")]
    assert waiting_before_idle and waiting_before_idle[0].startswith("正在等待 2 批请求返回，已等待 ")
    # 全部返回以后不再提示等待；退出 with 后提示线程已停。
    assert waiting == waiting_before_idle
    assert log._notice._ticker is None


class _SourceEngine(TranslationEngine):
    engine_name = "openai/test"

    def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
        return {text: f"{text}-tr" for text in texts}

    def translate_batch_with_sources(self, texts, target_lang, system_prompt, source_lang="auto"):
        return [
            TranslationLanguageResult(source_text=text, translation=f"{text}-tr", source_lang="zh")
            for text in texts
        ]


def test_auto_language_path_reports_batches_too():
    events: list[BatchRequestEvent] = []
    lock = threading.Lock()

    def record(event):
        with lock:
            events.append(event)

    texts = [f"条目{index}" for index in range(25)]
    result = translate_texts_with_sources(texts, _SourceEngine(), "en", "prompt", 10, 2, request_callback=record)

    assert len(result) == 25
    assert events[0].kind == BATCH_EVENT_PLAN
    assert (events[0].batch_total, events[0].items, events[0].concurrency) == (3, 25, 2)
    done = [event for event in events if event.kind == BATCH_EVENT_DONE]
    assert sorted(event.index for event in done) == [1, 2, 3]
    assert sum(event.items for event in done) == 25


class _WordEngine(TranslationEngine):
    engine_name = "fake/word"

    def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
        if "坏段" in texts:
            raise RuntimeError("bad")
        return {text: f"译:{text}" for text in texts}


def test_word_batches_report_plan_sent_done_and_failures():
    events: list[BatchRequestEvent] = []
    lock = threading.Lock()

    def record(event):
        with lock:
            events.append(event)

    settings = WordBatchSettings()
    settings.max_paragraphs_per_batch = 4
    texts = [f"第{index}段" for index in range(7)] + ["坏段"]
    translate_word_texts(texts, _WordEngine(), "fr", "system", settings, concurrency=2, request_callback=record)

    plan = [event for event in events if event.kind == BATCH_EVENT_PLAN]
    assert len(plan) == 1 and (plan[0].batch_total, plan[0].items, plan[0].concurrency) == (2, 8, 2)
    assert sorted(event.index for event in events if event.kind == BATCH_EVENT_SENT) == [1, 2]
    done = {event.index: event for event in events if event.kind == BATCH_EVENT_DONE}
    assert sorted(done) == [1, 2]
    # 第 2 批拆小重试后只剩「坏段」没译出来。
    assert (done[1].failed, done[2].failed) == (0, 1)
    assert all(event.stopped == 0 for event in done.values())


def test_word_batches_not_sent_after_stop_count_as_stopped():
    events: list[BatchRequestEvent] = []
    settings = WordBatchSettings()
    settings.max_paragraphs_per_batch = 2
    stop = threading.Event()

    class StopAfterFirst(_WordEngine):
        def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
            stop.set()
            return super().translate_batch(texts, target_lang, system_prompt, source_lang)

    translate_word_texts(
        ["一", "二", "三", "四"], StopAfterFirst(), "fr", "system", settings,
        concurrency=1, should_stop=stop.is_set, request_callback=events.append,
    )
    done = [event for event in events if event.kind == BATCH_EVENT_DONE]
    assert [(event.index, event.items, event.stopped) for event in done] == [(1, 2, 0)]


def test_request_lines_are_recognised_for_relaying():
    # PDF「转 Word 翻译」只转发 Word 流程的警告和这几类逐批日志。
    lines = _Lines()
    log = BatchRequestLog(lines, unit="处", subject="内容", tick_seconds=0.01, wait_log_seconds=0.05)
    with log:
        log.on_event(BatchRequestEvent(BATCH_EVENT_PLAN, batch_total=1, items=3, concurrency=1))
        log.on_event(BatchRequestEvent(BATCH_EVENT_SENT, index=1, batch_total=1, items=3))
        time.sleep(0.15)
        log.on_event(BatchRequestEvent(BATCH_EVENT_DONE, index=1, batch_total=1, items=3, elapsed=0.1))
    assert len(lines.texts) >= 3
    assert all(is_request_log_line(text) for text in lines.texts)
    for other in ("读取文件：说明.docx（1/1）", "开始翻译 3 处内容", "模型翻译完成，共 3 处", ""):
        assert not is_request_log_line(other)


def test_pdf_pages_use_their_own_waiting_line():
    lines = _Lines()
    tracker = InFlightRequestTracker()
    tracker.on_event(BatchRequestEvent(BATCH_EVENT_PLAN, batch_total=2, items=2, concurrency=2))
    tracker.on_event(BatchRequestEvent(BATCH_EVENT_SENT, index=1, items=1))
    tracker.on_event(BatchRequestEvent(BATCH_EVENT_SENT, index=2, items=1))
    with RequestWaitNotice(lines, tracker, template=PDF_PAGE_WAIT_TEMPLATE, tick_seconds=0.01, wait_log_seconds=0.1):
        time.sleep(0.25)
    assert lines.texts and lines.texts[0].startswith("正在等待 2 页生成结果，已等待 ")
