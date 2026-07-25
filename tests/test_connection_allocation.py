"""Allocation order and failure-driven failover across a connection pool."""

from __future__ import annotations

import pytest

from core.connection_pool import (
    FAILURE_CREDENTIAL,
    FAILURE_ENDPOINT,
    FAILURE_TRANSIENT,
    allocate_connection,
    classify_connection_failure,
    failover_candidates,
    should_switch_connection,
)
from settings import ModelConnection


def _pool(*specs: tuple[str, str]) -> list[ModelConnection]:
    return [
        ModelConnection(label=label, provider="custom_openai", base_url=base_url)
        for label, base_url in specs
    ]


class _HttpError(Exception):
    def __init__(self, status_code: int, message: str = "") -> None:
        super().__init__(message or f"HTTP {status_code}")
        self.status_code = status_code


def test_a_lone_task_takes_the_primary_even_with_spreading_on():
    pool = _pool(("A", "https://a.example/v1"), ("B", "https://b.example/v1"))
    allocation = allocate_connection(pool, busy_connection_ids=frozenset(), spread=True)
    assert allocation.connection.label == "A"
    assert allocation.shared is False


def test_second_concurrent_task_moves_to_the_next_free_entry():
    pool = _pool(("A", "https://a.example/v1"), ("B", "https://b.example/v1"))
    allocation = allocate_connection(
        pool, busy_connection_ids=frozenset({pool[0].id}), spread=True
    )
    assert allocation.connection.label == "B"
    assert allocation.shared is False


def test_spreading_off_keeps_every_task_on_the_primary():
    pool = _pool(("A", "https://a.example/v1"), ("B", "https://b.example/v1"))
    allocation = allocate_connection(
        pool, busy_connection_ids=frozenset({pool[0].id}), spread=False
    )
    assert allocation.connection.label == "A"
    # The caller still needs to know the connection is shared so it can warn.
    assert allocation.shared is True


def test_more_tasks_than_entries_falls_back_to_the_primary_and_reports_sharing():
    pool = _pool(("A", "https://a.example/v1"), ("B", "https://b.example/v1"))
    allocation = allocate_connection(
        pool,
        busy_connection_ids=frozenset({pool[0].id, pool[1].id}),
        spread=True,
    )
    assert allocation.connection.label == "A"
    assert allocation.shared is True


def test_candidates_start_at_the_allocated_entry():
    pool = _pool(
        ("A", "https://a.example/v1"),
        ("B", "https://b.example/v1"),
        ("C", "https://c.example/v1"),
    )
    allocation = allocate_connection(
        pool, busy_connection_ids=frozenset({pool[0].id}), spread=True
    )
    assert [conn.label for conn in allocation.candidates] == ["B", "C", "A"]


def test_empty_pool_is_rejected():
    with pytest.raises(ValueError):
        allocate_connection([], spread=True)


@pytest.mark.parametrize(
    "exc, expected",
    [
        (_HttpError(401), FAILURE_CREDENTIAL),
        (_HttpError(403), FAILURE_CREDENTIAL),
        (_HttpError(402), FAILURE_CREDENTIAL),
        (Exception("Invalid API key provided"), FAILURE_CREDENTIAL),
        (Exception("余额不足，请充值"), FAILURE_CREDENTIAL),
        (_HttpError(429), FAILURE_TRANSIENT),
        (_HttpError(503), FAILURE_ENDPOINT),
        (_HttpError(502), FAILURE_ENDPOINT),
        (ConnectionError("Connection refused"), FAILURE_ENDPOINT),
        (Exception("Failed to establish a new connection"), FAILURE_ENDPOINT),
        (Exception("something odd happened"), FAILURE_TRANSIENT),
    ],
)
def test_failure_classification(exc, expected):
    assert classify_connection_failure(exc) == expected


def test_only_endpoint_and_credential_failures_move_the_task():
    assert should_switch_connection(FAILURE_ENDPOINT) is True
    assert should_switch_connection(FAILURE_CREDENTIAL) is True
    # Rate limiting is handled by backoff in place, not by burning a connection.
    assert should_switch_connection(FAILURE_TRANSIENT) is False


def test_a_dead_endpoint_skips_every_account_on_that_server():
    pool = _pool(
        ("A1", "https://a.example/v1"),
        ("A2", "https://a.example/v1"),
        ("B", "https://b.example/v1"),
    )
    remaining = failover_candidates(
        pool, failed_connection_id=pool[0].id, failure_kind=FAILURE_ENDPOINT
    )
    assert [conn.label for conn in remaining] == ["B"]


def test_a_rejected_credential_still_tries_the_other_account_on_that_server():
    pool = _pool(
        ("A1", "https://a.example/v1"),
        ("A2", "https://a.example/v1"),
        ("B", "https://b.example/v1"),
    )
    remaining = failover_candidates(
        pool, failed_connection_id=pool[0].id, failure_kind=FAILURE_CREDENTIAL
    )
    assert [conn.label for conn in remaining] == ["A2", "B"]


def test_already_exhausted_entries_are_not_retried():
    pool = _pool(
        ("A", "https://a.example/v1"),
        ("B", "https://b.example/v1"),
        ("C", "https://c.example/v1"),
    )
    remaining = failover_candidates(
        pool,
        failed_connection_id=pool[1].id,
        failure_kind=FAILURE_CREDENTIAL,
        exhausted_connection_ids=frozenset({pool[0].id}),
    )
    assert [conn.label for conn in remaining] == ["C"]
