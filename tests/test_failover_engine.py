"""Runtime failover behaviour of the wrapping translation engine."""

from __future__ import annotations

import pytest

from core.failover_engine import FailoverTranslationEngine
from settings import ModelConnection


class _HttpError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class _StubEngine:
    """Fails a fixed number of times, then succeeds."""

    def __init__(self, label: str, error: Exception | None = None) -> None:
        self.label = label
        self.error = error
        self.calls = 0

    def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return {text: f"{self.label}:{text}" for text in texts}


def _pool(*specs: tuple[str, str]) -> list[ModelConnection]:
    return [
        ModelConnection(label=label, provider="custom_openai", base_url=url)
        for label, url in specs
    ]


def _run(engine: FailoverTranslationEngine) -> dict[str, str]:
    return engine.translate_batch(["hello"], "en", "prompt", source_lang="zh")


def test_a_healthy_connection_is_used_without_switching():
    pool = _pool(("A", "https://a.example/v1"), ("B", "https://b.example/v1"))
    engines = {pool[0].id: _StubEngine("A"), pool[1].id: _StubEngine("B")}
    engine = FailoverTranslationEngine(
        build_engine_for=lambda conn: engines[conn.id], candidates=pool
    )
    assert _run(engine) == {"hello": "A:hello"}
    assert engines[pool[1].id].calls == 0


def test_a_rejected_credential_moves_to_the_next_connection():
    pool = _pool(("A", "https://a.example/v1"), ("B", "https://b.example/v1"))
    engines = {
        pool[0].id: _StubEngine("A", _HttpError(401)),
        pool[1].id: _StubEngine("B"),
    }
    switches: list[tuple[str, str, str]] = []
    engine = FailoverTranslationEngine(
        build_engine_for=lambda conn: engines[conn.id],
        candidates=pool,
        on_switch=lambda old, new, kind, _err: switches.append(
            (old.label, new.label, kind)
        ),
    )
    assert _run(engine) == {"hello": "B:hello"}
    assert switches == [("A", "B", "credential")]
    assert engine.current_connection.label == "B"


def test_a_dead_endpoint_skips_the_other_account_on_that_server():
    pool = _pool(
        ("A1", "https://a.example/v1"),
        ("A2", "https://a.example/v1"),
        ("B", "https://b.example/v1"),
    )
    engines = {
        pool[0].id: _StubEngine("A1", _HttpError(503)),
        pool[1].id: _StubEngine("A2"),
        pool[2].id: _StubEngine("B"),
    }
    engine = FailoverTranslationEngine(
        build_engine_for=lambda conn: engines[conn.id], candidates=pool
    )
    assert _run(engine) == {"hello": "B:hello"}
    # The second account on the dead server must never be contacted.
    assert engines[pool[1].id].calls == 0


def test_rate_limiting_does_not_burn_a_connection():
    pool = _pool(("A", "https://a.example/v1"), ("B", "https://b.example/v1"))
    engines = {
        pool[0].id: _StubEngine("A", _HttpError(429)),
        pool[1].id: _StubEngine("B"),
    }
    engine = FailoverTranslationEngine(
        build_engine_for=lambda conn: engines[conn.id], candidates=pool
    )
    with pytest.raises(_HttpError):
        _run(engine)
    assert engines[pool[1].id].calls == 0
    assert engine.current_connection.label == "A"


def test_exhausting_every_connection_raises_the_last_error():
    pool = _pool(("A", "https://a.example/v1"), ("B", "https://b.example/v1"))
    engines = {
        pool[0].id: _StubEngine("A", _HttpError(401)),
        pool[1].id: _StubEngine("B", _HttpError(403)),
    }
    engine = FailoverTranslationEngine(
        build_engine_for=lambda conn: engines[conn.id], candidates=pool
    )
    with pytest.raises(_HttpError):
        _run(engine)


def test_a_connection_is_not_retried_after_it_was_ruled_out():
    pool = _pool(
        ("A", "https://a.example/v1"),
        ("B", "https://b.example/v1"),
        ("C", "https://c.example/v1"),
    )
    engines = {
        pool[0].id: _StubEngine("A", _HttpError(401)),
        pool[1].id: _StubEngine("B", _HttpError(401)),
        pool[2].id: _StubEngine("C"),
    }
    engine = FailoverTranslationEngine(
        build_engine_for=lambda conn: engines[conn.id], candidates=pool
    )
    assert _run(engine) == {"hello": "C:hello"}
    # A second batch stays on C rather than walking the dead entries again.
    assert _run(engine) == {"hello": "C:hello"}
    assert engines[pool[0].id].calls == 1
    assert engines[pool[1].id].calls == 1


def test_an_empty_chain_is_rejected():
    with pytest.raises(ValueError):
        FailoverTranslationEngine(build_engine_for=lambda conn: None, candidates=[])
