"""Audit 高-6 / 高-7: rate limiting must not throw away or multiply paid work."""

import threading
from unittest.mock import patch

import pytest

import core.api_concurrency_control as concurrency_control
import core.engine_dispatcher as dispatcher
from core.api_concurrency_control import ApiKeyTemporarilyUnavailableError
from engines.base_engine import TranslationEngine


class _KeyDiesMidway(TranslationEngine):
    """Translates every batch except the one holding ``t60``."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.translated: set[str] = set()

    @property
    def engine_name(self) -> str:
        return "fake/cloud"

    def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
        if "t60" in texts:
            raise ApiKeyTemporarilyUnavailableError("limited")
        with self.lock:
            self.translated.update(texts)
        return {text: "X" + text for text in texts}


def test_a_fatal_rate_limit_hands_back_the_batches_already_paid_for():
    engine = _KeyDiesMidway()
    texts = [f"t{i}" for i in range(121)]
    with pytest.raises(ApiKeyTemporarilyUnavailableError) as raised:
        dispatcher.translate_texts(texts, engine, "en", "sp", 10, 2)
    partial = raised.value.partial_results
    # 每一条成功翻译（已计费）的内容都随异常带了出来，没有一条丢掉。
    assert set(partial) == engine.translated
    assert partial and all(partial[text] == "X" + text for text in partial)
    assert "t60" not in partial


class _AlwaysRateLimited(TranslationEngine):
    def __init__(self) -> None:
        self.calls = 0

    @property
    def engine_name(self) -> str:
        return "fake/cloud"

    def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
        self.calls += 1
        raise RuntimeError("429 Too Many Requests: rate limit exceeded, too many concurrent requests")


def test_a_rate_limited_batch_is_not_split_into_more_requests():
    engine = _AlwaysRateLimited()
    stats = dispatcher.TranslationBatchRunStats()
    with patch.object(dispatcher, "_backoff_sleep", lambda *a, **k: None), patch.object(
        concurrency_control, "_interruptible_sleep", lambda *a, **k: None
    ):
        try:
            dispatcher.translate_texts([f"t{i}" for i in range(20)], engine, "en", "sp", 20, 1, stats=stats)
        except ApiKeyTemporarilyUnavailableError:
            pass
    # 拆小批次救不了限流：只按退避节奏重试同一批，不拆成更多请求。
    assert stats.retry_count == 0
    assert engine.calls <= 1 + dispatcher._MAX_CONCURRENCY_RETRY_ROUNDS
