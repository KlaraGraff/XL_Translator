"""A translation engine that moves down a connection chain when one dies.

The wrapper keeps the frozen candidate chain a task started with, so a
runtime switch can only ever land on a connection that already existed and
was already validated at start.  It never reads settings again, which is what
keeps a running task insulated from edits made in the panel meanwhile.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from loguru import logger

from core.connection_pool import (
    FAILURE_ENDPOINT,
    classify_connection_failure,
    failover_candidates,
    should_switch_connection,
)
from engines.base_engine import TranslationEngine
from settings import ModelConnection


class AllConnectionsExhaustedError(RuntimeError):
    """Raised when every connection in the chain has been ruled out."""


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
        self._current = self._candidates[0]
        self._engine = build_engine_for(self._current)

    @property
    def current_connection(self) -> ModelConnection:
        return self._current

    @property
    def engine_name(self) -> str:
        return getattr(self._engine, "engine_name", "")

    def __getattr__(self, item: str):
        # Anything the wrapper does not define belongs to the live engine.
        return getattr(self.__dict__["_engine"], item)

    def translate_batch(
        self,
        texts: list[str],
        target_lang: str,
        system_prompt: str,
        source_lang: str = "zh",
    ) -> dict[str, str]:
        while True:
            try:
                return self._engine.translate_batch(
                    texts,
                    target_lang,
                    system_prompt,
                    source_lang=source_lang,
                )
            except Exception as exc:  # noqa: BLE001 - the failure is classified below
                failure_kind = classify_connection_failure(exc)
                if not should_switch_connection(failure_kind):
                    # Rate limits and blips stay with the caller's own retry
                    # and backoff; burning a connection would not help.
                    raise
                if not self._switch_after(exc, failure_kind):
                    raise

    def _switch_after(self, exc: BaseException, failure_kind: str) -> bool:
        """Move to the next viable connection; return False when none is left."""
        failed = self._current
        self._exhausted.add(failed.id)
        if failure_kind == FAILURE_ENDPOINT:
            # The server is down, so every account on it is out, not just this one.
            for candidate in self._candidates:
                if candidate.base_url == failed.base_url:
                    self._exhausted.add(candidate.id)

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
            self._engine = self._build_engine_for(target)
        except Exception as build_error:  # noqa: BLE001 - try the next candidate
            logger.warning(
                "切换连接失败，继续尝试下一条：target={} error={}",
                target.display_label,
                build_error,
            )
            self._current = target
            return self._switch_after(build_error, FAILURE_ENDPOINT)

        previous, self._current = self._current, target
        logger.warning(
            "已切换连接：{} -> {}（原因：{}）",
            previous.display_label,
            target.display_label,
            failure_kind,
        )
        if self._on_switch is not None:
            self._on_switch(previous, target, failure_kind, str(exc))
        return True
