"""Audit 高-1: once the user presses stop, recovery calls must not queue for a
slot, call the model, or keep retrying a rate limit.

Covers 补译复核 pair arbitration, Word 语义仲裁 and Word 页眉页脚.
"""

import threading
import time

from core.api_scheduler import WeightedApiScheduler
from core.coverage_arbitration import ArbitrationPair
from core.coverage_review import VERDICT_EQUIVALENT, run_pair_arbitration_batch
from core.word_task_runner import (
    _SEMANTIC_VERDICT_UNCERTAIN,
    _run_header_footer_batch,
    _run_semantic_arbitration,
)


class _Engine:
    engine_name = "custom_openai"

    def __init__(self, error: Exception | None = None) -> None:
        self.calls = 0
        self.error = error

    def chat(self, system_prompt: str, user_payload: str) -> str:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return "{}"


PAIRS = [ArbitrationPair(id="p1", source="合同总价", candidate="Prix total du contrat")]


def _calls():
    """(name, call(engine, scheduler, should_stop), expected result on stop)."""
    return [
        (
            "pair_arbitration",
            lambda engine, scheduler, stop: run_pair_arbitration_batch(
                engine, PAIRS, target_lang="fr", source_lang="zh",
                api_scheduler=scheduler, should_stop=stop,
            ),
            lambda result: result == {"p1": VERDICT_EQUIVALENT},
        ),
        (
            "semantic_arbitration",
            lambda engine, scheduler, stop: _run_semantic_arbitration(
                engine, "合同总价", "Prix total du contrat", target_lang="fr", source_lang="zh",
                api_scheduler=scheduler, should_stop=stop,
            ),
            lambda result: result.verdict == _SEMANTIC_VERDICT_UNCERTAIN,
        ),
        (
            "header_footer",
            lambda engine, scheduler, stop: _run_header_footer_batch(
                engine, ["第 1 页 共 3 页"], source_lang="zh", target_lang="fr",
                api_scheduler=scheduler, should_stop=stop,
            ),
            lambda result: result == [],
        ),
    ]


def test_stop_while_waiting_for_a_slot_returns_without_calling_the_model():
    for name, call, ok in _calls():
        scheduler = WeightedApiScheduler(1)
        held = scheduler.acquire_lease(1)  # 槽位被别的请求占着
        stop = threading.Event()
        engine = _Engine()
        result: list = []
        worker = threading.Thread(target=lambda: result.append(call(engine, scheduler, stop.is_set)))
        worker.start()
        time.sleep(0.2)
        stop.set()
        worker.join(timeout=1.0)
        scheduler.release(held)
        assert not worker.is_alive(), f"{name} kept waiting for a slot after stop"
        assert engine.calls == 0, name
        assert ok(result[0]), (name, result[0])


def test_rate_limit_after_stop_is_not_retried():
    for name, call, ok in _calls():
        scheduler = WeightedApiScheduler(4)
        stopped = {"value": False}
        engine = _Engine(RuntimeError("429 Too Many Requests: concurrency limit exceeded"))
        original_chat = engine.chat

        def chat_then_stop(system_prompt, user_payload, _chat=original_chat):
            stopped["value"] = True  # 用户在这次请求途中按了停止
            return _chat(system_prompt, user_payload)

        engine.chat = chat_then_stop
        started = time.monotonic()
        result = call(engine, scheduler, lambda: stopped["value"])
        assert engine.calls == 1, (name, engine.calls)
        assert time.monotonic() - started < 1.0, name
        assert ok(result), (name, result)


def test_without_stop_the_calls_still_reach_the_model():
    for name, call, _ok in _calls():
        engine = _Engine()
        call(engine, WeightedApiScheduler(2), lambda: False)
        assert engine.calls == 1, name
