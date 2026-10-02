"""Explicit, path-private file lifecycle messages shared by all runners."""

from __future__ import annotations

import hashlib
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FILE_TERMINAL_STATES = frozenset({"generated", "failed", "unstarted", "stopped", "interrupted"})


def file_progress_id(path: str | Path) -> str:
    """Match scanner and runner identities without sending source paths."""
    normalized = os.path.normcase(str(Path(path).expanduser().resolve()))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def file_progress_entry(item: Any, source_root: Path | None = None) -> dict[str, Any]:
    path = Path(item.path)
    try:
        relative = path.resolve().relative_to(source_root.resolve()) if source_root else Path(path.name)
    except ValueError:
        relative = Path(path.name)
    return {
        "file_id": file_progress_id(path),
        "name": path.name,
        "relative_path": str(relative),
        "state": "waiting",
        "phase": "prepare",
        "completed": 0,
        "total": 0,
        "result": {},
        "revision": 0,
    }


@dataclass
class FileProgressMsg:
    file_id: str
    name: str
    relative_path: str
    state: str
    phase: str
    completed: int = 0
    total: int = 0
    result: dict[str, Any] = field(default_factory=dict)


class FileProgressReporter:
    """Serialize lifecycle updates, including overlapping PDF page work."""

    def __init__(self, queue: Any, files: list[Any], source_root: Path | None = None) -> None:
        self._queue = queue
        self._entries = {file_progress_id(item.path): file_progress_entry(item, source_root) for item in files}
        self._lock = threading.RLock()

    def emit(
        self, source_path: str | Path, state: str, phase: str,
        *, completed: int = 0, total: int = 0, result: dict[str, Any] | None = None, force: bool = False,
    ) -> None:
        with self._lock:
            file_id = file_progress_id(source_path)
            entry = self._entries.get(file_id)
            if entry is None:
                return
            # A later aggregate phase must not revive a failed/generated file.
            if not force and entry["state"] in FILE_TERMINAL_STATES and state not in FILE_TERMINAL_STATES:
                return
            entry.update(state=state, phase=phase, completed=max(0, completed), total=max(0, total))
            if result is not None:
                entry["result"] = {**result, "file_id": file_id}
            self._queue.put(FileProgressMsg(**{key: value for key, value in entry.items() if key != "revision"}))

    def emit_many(self, state: str, phase: str, paths: list[str | Path] | None = None, *, force: bool = False) -> None:
        with self._lock:
            ids = {file_progress_id(path) for path in paths} if paths is not None else set(self._entries)
            for file_id, entry in self._entries.items():
                if file_id not in ids or (not force and entry["state"] in FILE_TERMINAL_STATES):
                    continue
                entry.update(state=state, phase=phase, completed=0, total=0)
                self._queue.put(FileProgressMsg(**{key: value for key, value in entry.items() if key != "revision"}))
