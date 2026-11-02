"""Where each selected source file sits under the task's output folder.

Files under the source root keep their folder structure.  Files picked from
other folders (a multi-folder drop, #288) land at the top of the output folder
by name.  When two of them would land on the same name, each clashing file
picked from outside the root goes into a subfolder named after the folder it
came from (「一期/清单_英文.xlsx」 and 「二期/清单_英文.xlsx」), so neither
translation overwrites the other.

Every runner asks this one place, so output files, resume matching and the
file list in the UI agree on the same layout.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

# A file picked straight from a drive root has no folder name to borrow.
_NAMELESS_FOLDER = "其他文件夹"


def _key(path: Path) -> str:
    return os.path.normcase(path.as_posix()).casefold()


def _resolved(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


class SourceLayout:
    def __init__(self, paths: Iterable[str | Path], source_root: str | Path | None) -> None:
        root = _resolved(source_root) if source_root else None
        self._root = root if root is not None and not root.is_file() else None
        self._relative: dict[Path, Path] = {}
        outside: list[Path] = []
        for raw in paths:
            path = _resolved(raw)
            if path in self._relative:
                continue
            inside = self._inside(path)
            self._relative[path] = inside if inside is not None else Path(path.name)
            if inside is None:
                outside.append(path)
        self._separate_clashing(outside)

    def _inside(self, path: Path) -> Path | None:
        if self._root is None:
            return None
        try:
            return path.relative_to(self._root)
        except ValueError:
            return None

    def _separate_clashing(self, outside: list[Path]) -> None:
        counts: dict[str, int] = {}
        for relative in self._relative.values():
            counts[_key(relative)] = counts.get(_key(relative), 0) + 1
        clashing = [path for path in outside if counts[_key(self._relative[path])] > 1]
        if not clashing:
            return
        taken = {
            _key(relative)
            for path, relative in self._relative.items()
            if path not in clashing
        }
        for path in clashing:
            folder = path.parent.name or _NAMELESS_FOLDER
            candidate = Path(folder) / path.name
            number = 2
            while _key(candidate) in taken:
                candidate = Path(f"{folder} ({number})") / path.name
                number += 1
            taken.add(_key(candidate))
            self._relative[path] = candidate

    def relative(self, path: str | Path) -> Path:
        """The file's path under the output folder, file name included."""
        resolved = _resolved(path)
        known = self._relative.get(resolved)
        if known is not None:
            return known
        inside = self._inside(resolved)
        return inside if inside is not None else Path(resolved.name)

    def subdir(self, path: str | Path) -> Path:
        """The folder under the output folder that the file's results go in."""
        return self.relative(path).parent
