"""Shared state models for native translation task pages."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Generic, TypeVar


T = TypeVar("T")


def file_item_identity(item: object) -> str:
    """Return a stable identity for scanned file items."""
    path = getattr(item, "path", None)
    if path is not None:
        try:
            return str(Path(path).expanduser().resolve(strict=False))
        except (OSError, RuntimeError, TypeError, ValueError):
            return str(path)
    return repr(item)


class FileSelectionModel(Generic[T]):
    """Keep selected files independent from transient Qt table widgets."""

    def __init__(self, identity: Callable[[T], str] = file_item_identity):
        self._identity = identity
        self._files: list[T] = []
        self._selected_ids: set[str] = set()

    def set_files(self, files: Iterable[T], *, select_all: bool = True) -> list[T]:
        self._files = list(files)
        current_ids = [self._identity(item) for item in self._files]
        if select_all:
            self._selected_ids = set(current_ids)
        else:
            self._selected_ids.intersection_update(current_ids)
        return list(self._files)

    def sync_files(self, files: Iterable[T]) -> None:
        new_files = list(files)
        current_ids = {self._identity(item) for item in new_files}
        existing_ids = {self._identity(item) for item in self._files}
        if current_ids != existing_ids:
            self._files = new_files
            self._selected_ids = set(current_ids)
            return
        self._files = new_files
        self._selected_ids.intersection_update(current_ids)

    def clear(self) -> None:
        self._files = []
        self._selected_ids = set()

    def selected_files(self, files: Iterable[T] | None = None) -> list[T]:
        if files is not None:
            self.sync_files(files)
        return [item for item in self._files if self.is_selected(item)]

    def selected_count(self, files: Iterable[T] | None = None) -> int:
        return len(self.selected_files(files))

    def is_selected(self, item: T) -> bool:
        return self._identity(item) in self._selected_ids

    def set_selected(self, item: T, selected: bool) -> None:
        item_id = self._identity(item)
        if selected:
            self._selected_ids.add(item_id)
        else:
            self._selected_ids.discard(item_id)

    def set_all(self, selected: bool, files: Iterable[T] | None = None) -> None:
        if files is not None:
            self.sync_files(files)
        if selected:
            self._selected_ids = {self._identity(item) for item in self._files}
        else:
            self._selected_ids = set()
