"""CAD 翻译：请求在途时进度条继续推进，运行日志写清每批请求的发出与返回。

maple 2026-10-12 实测：同时发出的几十批请求差不多同时回来，进度先停住、再一下子
加满；运行日志从「术语表匹配 0 条」直接跳到「翻译完成」，中间看不出在等什么。
"""

from __future__ import annotations

import threading
import time

import core.cad_translation as cad_translation
from core.cad_translation import CadPipelineOptions, CadProgress, CadTranslationPipeline
from core.engine_dispatcher import translate_texts
from core.request_progress import (
    BATCH_EVENT_DONE,
    BATCH_EVENT_PLAN,
    BATCH_EVENT_SENT,
    BatchRequestEvent,
    InFlightRequestTracker,
)


def _dxf(words):
    return "".join(f"0\nTEXT\n1\n{word}\n" for word in words)


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_tracker_credits_requests_in_flight_but_never_completes_them():
    clock = _Clock()
    tracker = InFlightRequestTracker(clock=clock)
    tracker.on_event(BatchRequestEvent(BATCH_EVENT_PLAN, batch_total=3, items=30, concurrency=3))
    for index in (1, 2, 3):
        tracker.on_event(BatchRequestEvent(BATCH_EVENT_SENT, index=index, batch_total=3, items=10))
    assert tracker.estimated_items() == 0

    estimates = []
    for _ in range(6):
        clock.now += 10
        estimates.append(tracker.estimated_items())
    # 还没有任何请求返回：估计一路往上走，但远不到 30 条。
    assert estimates == sorted(estimates) and estimates[0] > 0
    assert estimates[-1] < 30 * 0.9
    assert tracker.done_items() == 0

    tracker.on_event(BatchRequestEvent(BATCH_EVENT_DONE, index=1, batch_total=3, items=10, elapsed=60.0))
    assert tracker.done_items() == 10
    # 有了实际用时：在途的两批按 60 秒估，等得再久也只算到 9 成。
    clock.now += 1000
    assert 10 < tracker.estimated_items() <= 10 + 20 * 0.9 + 1e-9
    tracker.on_event(BatchRequestEvent(BATCH_EVENT_DONE, index=2, batch_total=3, items=10, elapsed=61.0, failed=4))
    tracker.on_event(BatchRequestEvent(BATCH_EVENT_DONE, index=3, batch_total=3, items=10, elapsed=62.0))
    assert tracker.estimated_items() == 30
    assert tracker.failed_items == 4
    assert tracker.in_flight_count() == 0


class _Engine:
    engine_name = "openai/test"

    def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
        return {text: f"{text}-译" for text in texts}


def test_translate_texts_reports_plan_sent_and_done_for_every_batch():
    events: list[BatchRequestEvent] = []
    lock = threading.Lock()

    def record(event):
        with lock:
            events.append(event)

    texts = [f"Item {index}" for index in range(25)]
    result = translate_texts(texts, _Engine(), "zh", "prompt", 10, 2, source_lang="en", request_callback=record)

    assert len(result) == 25
    assert events[0].kind == BATCH_EVENT_PLAN
    assert (events[0].batch_total, events[0].items, events[0].concurrency) == (3, 25, 2)
    sent = sorted(event.index for event in events if event.kind == BATCH_EVENT_SENT)
    done = [event for event in events if event.kind == BATCH_EVENT_DONE]
    assert sent == [1, 2, 3]
    assert sorted(event.index for event in done) == [1, 2, 3]
    assert sum(event.items for event in done) == 25
    assert all(event.failed == 0 and event.elapsed >= 0 for event in done)


def test_a_broken_request_hook_does_not_break_translation():
    def broken(event):
        raise RuntimeError("hook")

    result = translate_texts(["A", "B"], _Engine(), "zh", "prompt", 1, 2, source_lang="en", request_callback=broken)
    assert result == {"A": "A-译", "B": "B-译"}


def test_pipeline_moves_while_requests_are_out_and_logs_each_request(tmp_path, monkeypatch):
    monkeypatch.setattr(cad_translation, "_TICK_SECONDS", 0.01)
    monkeypatch.setattr(cad_translation, "_WAIT_LOG_SECONDS", 0.15)
    words = [f"Word{index}" for index in range(8)]
    source = tmp_path / "plan.dxf"
    source.write_text(_dxf(words), encoding="utf-8")
    progress: list[CadProgress] = []
    lock = threading.Lock()

    def on_progress(item):
        with lock:
            progress.append(item)

    def translate(texts, glossary, progress=None, should_stop=None, error_callback=None, request_callback=None):
        request_callback(BatchRequestEvent(BATCH_EVENT_PLAN, batch_total=2, items=len(texts), concurrency=2))
        for index in (1, 2):
            request_callback(BatchRequestEvent(BATCH_EVENT_SENT, index=index, batch_total=2, items=4))
        # 两批请求都还在路上：这段时间里进度条要自己往前走。
        time.sleep(0.4)
        error_callback("一批翻译出错，已拆分为更小的批次重试：服务暂时不可用。")
        request_callback(BatchRequestEvent(BATCH_EVENT_DONE, index=1, batch_total=2, items=4, elapsed=0.4))
        progress(4, 8)
        request_callback(BatchRequestEvent(BATCH_EVENT_DONE, index=2, batch_total=2, items=4, elapsed=0.4, failed=1))
        progress(8, 8)
        return {text: f"{text}译" for text in texts[1:]}

    CadTranslationPipeline(converter=None, translator=translate, progress=on_progress).translate_file(
        source, tmp_path / "plan_zh.dxf", options=CadPipelineOptions(target_lang="zh", source_lang="auto")
    )

    translate_events = [item for item in progress if item.stage == "translate"]
    percents = [item.percent for item in translate_events]
    assert percents == sorted(percents), "进度不能倒退"
    # 第一批返回之前（done 仍为 0）进度条已经走了好几步。
    before_return = [item for item in translate_events if item.done == 0]
    assert len({item.percent for item in before_return}) >= 4
    assert max(item.percent for item in before_return) > 10.0
    assert max(item.percent for item in before_return) < 90.0

    messages = [(item.level, item.message) for item in progress if item.message]
    texts = [message for _, message in messages]
    assert "开始翻译 8 条文字（相同内容只翻译一次）：分 2 批发送，每次同时发送 2 批" in texts
    assert any(text.startswith("正在等待 2 批请求返回，已等待 ") for text in texts)
    assert ("WARN", "一批翻译出错，已拆分为更小的批次重试：服务暂时不可用。") in messages
    assert ("INFO", "第 1/2 批已返回：4 条，用时 0.4 秒") in messages
    assert ("WARN", "第 2/2 批已返回：4 条中 1 条未获得译文，用时 0.4 秒") in messages
    summary = [(level, text) for level, text in messages if text.startswith("模型翻译结束：")]
    assert summary and summary[0][0] == "WARN"
    assert summary[0][1].startswith("模型翻译结束：7 条获得译文，1 条未获得译文，用时 ")
    assert progress[-1].percent == 100


def test_pipeline_without_request_events_keeps_old_behaviour(tmp_path):
    source = tmp_path / "old.dxf"
    source.write_text(_dxf(["Alpha", "Bravo"]), encoding="utf-8")
    progress: list[CadProgress] = []
    CadTranslationPipeline(
        converter=None, translator=lambda texts, glossary: {text: f"{text}译" for text in texts}, progress=progress.append
    ).translate_file(source, tmp_path / "old_zh.dxf", options=CadPipelineOptions(target_lang="zh", source_lang="auto"))
    messages = [item.message for item in progress if item.message]
    assert any(message.startswith("模型翻译完成：2 条获得译文，用时 ") for message in messages)
    assert not any(message.startswith("开始翻译") for message in messages)


def test_cad_translator_does_not_count_failed_batches_as_translations(tmp_path, monkeypatch):
    """接口放弃的一批按原文占位交回：以前 CAD 当成「已翻译」，日志和报告都说全翻好了。"""
    from types import SimpleNamespace
    from unittest.mock import patch

    import settings as app_settings
    from api.task_contracts import TaskOptions
    from api.task_manager import TranslationTaskManager
    from settings import AppSettings

    monkeypatch.setattr(app_settings, "APP_DATA_DIR", tmp_path / "app-data")
    settings = AppSettings()
    manager = TranslationTaskManager(settings_loader=lambda: settings)
    source = tmp_path / "sample.dxf"
    source.write_text(_dxf(["Door"]), encoding="utf-8")

    class HalfBroken:
        engine_name = "openai/test"

        def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
            if "Broken" in texts:
                raise ValueError("响应解析失败")
            return {text: f"{text}-译" for text in texts}

    with (
        patch("api.surfaces.cad.probe_status", return_value={"converter": ""}),
        patch("api.surfaces.cad.build_role_engine", return_value=HalfBroken()),
        patch("core.engine_dispatcher.get_system_prompt", return_value="CAD prompt"),
        patch("core.engine_dispatcher._backoff_sleep", lambda *args, **kwargs: None),
    ):
        runner = manager._build_runner(
            surface="cad",
            files=[SimpleNamespace(path=source, name=source.name, format="dxf")],
            settings=settings,
            source_root=tmp_path,
            options=TaskOptions(),
            source_lang="en",
            key_overrides={},
        )
        errors: list[str] = []
        result = runner._translator(["Door", "Broken"], {}, error_callback=errors.append)

    assert result == {"Door": "Door-译"}
    assert errors, "失败原因要进运行日志"
