"""Connection allocation and failover decisions for one role's pool.

A pool is an ordered list of whole endpoints.  Two questions live here:

* which entry a starting task should occupy, and
* where to go next when the one in use stops working.

Both answers are pure functions over the pool plus the state passed in, so the
task manager can decide without this module reaching into settings or tasks.
"""

from __future__ import annotations

from dataclasses import dataclass

from config import normalize_cloud_base_url
from settings import ModelConnection

# A dead endpoint takes every account on it down with it, so the next
# candidate has to be a different Base URL.  A rejected credential only
# burns its own entry.  Rate limits and blips are not a pool problem at all:
# the dispatcher already backs off and retries in place.
FAILURE_ENDPOINT = "endpoint"
FAILURE_CREDENTIAL = "credential"
FAILURE_TRANSIENT = "transient"

_CREDENTIAL_STATUS = {401, 402, 403}
_ENDPOINT_STATUS = {404, 500, 502, 503, 504}

_CREDENTIAL_MARKERS = (
    "invalid api key",
    "incorrect api key",
    "unauthorized",
    "forbidden",
    "insufficient_quota",
    "billing hard limit",
    "余额不足",
    "额度不足",
    "未授权",
)

_ENDPOINT_MARKERS = (
    "connection refused",
    "connection error",
    "name or service not known",
    "temporary failure in name resolution",
    "nodename nor servname",
    "failed to establish a new connection",
    "connect timeout",
    "bad gateway",
    "service unavailable",
    "无法连接",
    "连接被拒绝",
)


def _status_code(exc: BaseException) -> int:
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
    try:
        return int(status_code)
    except (TypeError, ValueError):
        return 0


def classify_connection_failure(exc: BaseException) -> str:
    """Classify why a request failed, in pool terms."""
    status = _status_code(exc)
    if status in _CREDENTIAL_STATUS:
        return FAILURE_CREDENTIAL
    if status == 429:
        return FAILURE_TRANSIENT
    if status in _ENDPOINT_STATUS:
        return FAILURE_ENDPOINT

    message = str(exc or "").casefold()
    if any(marker in message for marker in _CREDENTIAL_MARKERS):
        return FAILURE_CREDENTIAL
    if any(marker in message for marker in _ENDPOINT_MARKERS):
        return FAILURE_ENDPOINT
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return FAILURE_ENDPOINT
    return FAILURE_TRANSIENT


def should_switch_connection(failure_kind: str) -> bool:
    """Return whether a failure justifies leaving the current connection."""
    return failure_kind in {FAILURE_ENDPOINT, FAILURE_CREDENTIAL}


def endpoint_identity(connection: ModelConnection) -> str:
    """Return what "the same server" means when skipping a dead endpoint.

    Raw Base URLs cannot be compared: two providers with SDK-owned endpoints
    both leave the field empty without sharing a server, and one server can be
    written several ways.  Resolve the URL first and fall back to the provider
    name when no URL exists at all.
    """
    normalized = normalize_cloud_base_url(
        connection.provider,
        connection.base_url,
    ).rstrip("/")
    if normalized:
        return normalized
    raw = str(connection.base_url or "").strip().rstrip("/")
    if raw:
        return raw
    return f"provider:{str(connection.provider or '').strip()}"


@dataclass(frozen=True)
class ConnectionAllocation:
    connection: ModelConnection
    # Every entry the task may fall back to, in the order it should try them.
    candidates: tuple[ModelConnection, ...]
    shared: bool


def allocate_connection(
    connections: list[ModelConnection],
    *,
    busy_connection_ids: frozenset[str] = frozenset(),
    spread: bool = False,
) -> ConnectionAllocation:
    """Choose the entry a starting task should occupy.

    With spreading off, every task takes the primary and the rest of the pool
    exists only as failover.  With it on, a task takes the first entry nobody
    is using, so a task running on its own still lands on the primary.
    """
    usable = [conn for conn in connections if conn is not None]
    if not usable:
        raise ValueError("连接池为空，无法分配连接。")

    if not spread:
        chosen = usable[0]
        return ConnectionAllocation(
            connection=chosen,
            candidates=tuple(usable),
            shared=chosen.id in busy_connection_ids,
        )

    for connection in usable:
        if connection.id not in busy_connection_ids:
            return ConnectionAllocation(
                connection=connection,
                candidates=_ordered_from(usable, connection),
                shared=False,
            )

    # Everything is occupied; fall back to the primary and let the caller warn
    # about the shared connection rather than refusing to start.
    chosen = usable[0]
    return ConnectionAllocation(
        connection=chosen,
        candidates=tuple(usable),
        shared=True,
    )


def _ordered_from(
    connections: list[ModelConnection],
    start: ModelConnection,
) -> tuple[ModelConnection, ...]:
    """Return the pool starting at ``start``, wrapping around."""
    index = next(
        (position for position, conn in enumerate(connections) if conn.id == start.id),
        0,
    )
    return tuple(connections[index:] + connections[:index])


def failover_candidates(
    candidates: tuple[ModelConnection, ...] | list[ModelConnection],
    *,
    failed_connection_id: str,
    failure_kind: str,
    exhausted_connection_ids: frozenset[str] = frozenset(),
) -> list[ModelConnection]:
    """Return the entries still worth trying after a failure.

    An endpoint failure removes every entry sharing that Base URL, because a
    server that is down does not care which account asks.
    """
    ordered = [conn for conn in candidates if conn is not None]
    failed = next(
        (conn for conn in ordered if conn.id == failed_connection_id),
        None,
    )
    dead_endpoint = (
        endpoint_identity(failed)
        if failed is not None and failure_kind == FAILURE_ENDPOINT
        else None
    )

    remaining: list[ModelConnection] = []
    for connection in ordered:
        if connection.id == failed_connection_id:
            continue
        if connection.id in exhausted_connection_ids:
            continue
        if dead_endpoint and endpoint_identity(connection) == dead_endpoint:
            continue
        remaining.append(connection)
    return remaining
