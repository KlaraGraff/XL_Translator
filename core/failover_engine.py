"""A translation engine that moves down a connection chain when one dies.

The wrapper keeps the frozen candidate chain a task started with, so a
runtime switch can only ever land on a connection that already existed and
was already validated at start.  It never reads settings again, which is what
keeps a running task insulated from edits made in the panel meanwhile.
"""

from __future__ import annotations

import inspect
import threading
from collections.abc import Callable, Sequence
from typing import TypeVar

from loguru import logger

from core.connection_pool import (
    FAILURE_ENDPOINT,
    classify_connection_failure,
    endpoint_identity,
    failover_candidates,
    should_switch_connection,
)
from engines.base_engine import TranslationEngine
from core.language_preflight import TranslationLanguageResult
from settings import ModelConnection

_T = TypeVar("_T")


class AllConnectionsExhaustedError(RuntimeError):
    """Raised when every connection in the chain has been ruled out."""


def concrete_base_engine_members() -> frozenset[str]:
    """Public members ``TranslationEngine`` already defines with a body.

    ``__getattr__`` only fires for names ordinary lookup fails to find, so
    every one of these shadows the wrapper's delegation and must be
    re-implemented on :class:`FailoverTranslationEngine`.  The test-suite
    asserts on this set, which turns a future concrete helper on the base
    class into a red test instead of a document that silently ships untouched
    source text.
    """
    abstract = getattr(TranslationEngine, "__abstractmethods__", frozenset())
    members: set[str] = set()
    for name, value in vars(TranslationEngine).items():
        if name.startswith("_") or name in abstract:
            continue
        if inspect.isfunction(value) or isinstance(value, (property, staticmethod, classmethod)):
            members.add(name)
    return frozenset(members)


class FailoverTranslationEngine(TranslationEngine):
    """Delegate to one connection's engine, moving on when it stops working."""

    def __init__(
        self,
        *,
        build_engine_for: Callable[[ModelConnection], TranslationEngine],
        candidates: Sequence[ModelConnection],
        on_switch: Callable[[ModelConnection, ModelConnection, str, str], None] | None = None,
    ) -> None:
        usable = [conn for conn in candidates if conn is not None]
        if not usable:
            raise ValueError("连接链为空，无法构建引擎。")
        self._build_engine_for = build_engine_for
        self._candidates = list(usable)
        self._on_switch = on_switch
        self._exhausted: set[str] = set()
        # Batches run on a thread pool, so failures on one connection arrive
        # from several threads at once; every switch decision happens under
        # this lock and is attributed to the connection the call actually used.
        self._lock = threading.Lock()
        self._current = self._candidates[0]
        self._engine = build_engine_for(self._current)

    @property
    def current_connection(self) -> ModelConnection:
        with self._lock:
            return self._current

    @property
    def engine_name(self) -> str:
        with self._lock:
            engine = self._engine
        return getattr(engine, "engine_name", "")

    def __getattr__(self, item: str):
        # Anything the wrapper does not define belongs to the live engine.
        return getattr(self.__dict__["_engine"], item)

    def _call_with_failover(self, invoke: Callable[[TranslationEngine], _T]) -> _T:
        """Run ``invoke`` on the live engine, moving down the chain on failure."""
        while True:
            with self._lock:
                engine = self._engine
                connection = self._current
            try:
                return invoke(engine)
            except Exception as exc:  # noqa: BLE001 - the failure is classified below
                failure_kind = classify_connection_failure(exc)
                if not should_switch_connection(failure_kind):
                    # Rate limits and blips stay with the caller's own retry
                    # and backoff; burning a connection would not help.
                    raise
                if not self._switch_after(connection, exc, failure_kind):
                    raise

    def translate_batch(
        self,
        texts: list[str],
        target_lang: str,
        system_prompt: str,
        source_lang: str = "zh",
    ) -> dict[str, str]:
        return self._call_with_failover(
            lambda engine: engine.translate_batch(
                texts,
                target_lang,
                system_prompt,
                source_lang=source_lang,
            )
        )

    # ``chat`` and ``translate_batch_with_sources`` carry a body on
    # ``TranslationEngine``, so attribute lookup resolves them on the base class
    # and ``__getattr__`` never runs.  Without these overrides the wrapper
    # answered every automatic-language batch with the base class'
    # NotImplementedError, and the Excel fallback turned that into source text
    # presented as translation.  They must delegate to the *live* engine rather
    # than reuse the base implementation, because concrete engines override both.
    def chat(self, system: str, user: str) -> str:
        return self._call_with_failover(lambda engine: engine.chat(system, user))

    def translate_batch_with_sources(
        self,
        texts: list[str],
        target_lang: str,
        system_prompt: str,
        source_lang: str = "auto",
    ) -> list[TranslationLanguageResult]:
        return self._call_with_failover(
            lambda engine: engine.translate_batch_with_sources(
                texts,
                target_lang,
                system_prompt,
                source_lang=source_lang,
            )
        )

    def _switch_after(
        self,
        failed: ModelConnection,
        exc: BaseException,
        failure_kind: str,
    ) -> bool:
        """Move off ``failed``; return False when no viable connection is left."""
        with self._lock:
            return self._switch_from_locked(failed, exc, failure_kind)

    def _switch_from_locked(
        self,
        failed: ModelConnection,
        exc: BaseException,
        failure_kind: str,
    ) -> bool:
        self._exhausted.add(failed.id)
        if failure_kind == FAILURE_ENDPOINT:
            # The server is down, so every account on it is out, not just this one.
            dead_endpoint = endpoint_identity(failed)
            for candidate in self._candidates:
                if endpoint_identity(candidate) == dead_endpoint:
                    self._exhausted.add(candidate.id)

        if self._current.id != failed.id and self._current.id not in self._exhausted:
            # Another thread already moved off the connection this call was
            # using; retry on the one it picked instead of burning it too.
            return True

        remaining = failover_candidates(
            self._candidates,
            failed_connection_id=failed.id,
            failure_kind=failure_kind,
            exhausted_connection_ids=frozenset(self._exhausted),
        )
        if not remaining:
            logger.error(
                "连接链已全部失效：last={} kind={} error={}",
                failed.display_label,
                failure_kind,
                exc,
            )
            return False

        target = remaining[0]
        try:
            engine = self._build_engine_for(target)
        except Exception as build_error:  # noqa: BLE001 - try the next candidate
            logger.warning(
                "切换连接失败，继续尝试下一条：target={} error={}",
                target.display_label,
                build_error,
            )
            return self._switch_from_locked(target, build_error, FAILURE_ENDPOINT)

        previous, self._current = self._current, target
        self._engine = engine
        logger.warning(
            "已切换连接：{} -> {}（原因：{}）",
            previous.display_label,
            target.display_label,
            failure_kind,
        )
        if self._on_switch is not None:
            self._on_switch(previous, target, failure_kind, str(exc))
        return True
