"""Small helpers shared by several route modules."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import JSONResponse


def _raise_active_tasks_block(category: str) -> None:
    raise HTTPException(
        409,
        "仍有任务正在进行，请待其结束后再清理。",
        headers={"X-Translator-Reason": f"active_tasks_block_{category}"},
    )


@contextmanager
def _no_active_tasks_during(app: FastAPI, *, category: str) -> Iterator[None]:
    """Check for active tasks and keep new ones out until the clean-up ends.

    A bare check followed by the clean-up leaves a window in which a task can
    start and then have its history, logs or keys removed underneath it.
    """
    with app.state.task_manager.maintenance_window() as active_count:
        if active_count:
            _raise_active_tasks_block(category)
        yield


def _json_error(
    status_code: int,
    detail: str,
    *,
    reason: str | None = None,
    accept: dict[str, Any] | None = None,
) -> Response:
    payload: dict[str, Any] = {"detail": detail}
    if reason:
        payload["reason"] = reason
    if accept:
        payload["accept"] = accept
    return JSONResponse(status_code=status_code, content=payload)


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return _json_safe(asdict(value))
    if hasattr(value, "model_dump"):
        return _json_safe(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)
