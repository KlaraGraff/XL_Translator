"""Utilities for accepting paths copied from shells, file browsers, and chat."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote


# A copied path is often wrapped by a shell, Markdown, or a Chinese text editor.
# Only remove a matching outer pair; an apostrophe or quote inside a real file
# name must remain part of the path.
_OUTER_PAIRS = {
    "'": "'",
    '"': '"',
    "`": "`",
    "“": "”",
    "‘": "’",
    "「": "」",
    "『": "』",
    "《": "》",
    "〈": "〉",
    "<": ">",
}


def normalize_user_path(value: Any) -> Any:
    """Trim whitespace and one or more matching wrappers around a user path.

    The function is deliberately conservative: unmatched wrappers and wrapper
    characters in the middle of a path are preserved. Returning non-strings as
    is keeps it safe to use from Pydantic ``before`` validators.
    """

    if not isinstance(value, str):
        return value
    normalized = value.strip()
    while len(normalized) >= 2:
        closing = _OUTER_PAIRS.get(normalized[0])
        if closing is None or normalized[-1] != closing:
            break
        normalized = normalized[1:-1].strip()
    return _unescape_shell_path(_from_file_url(normalized))


def _from_file_url(value: str) -> str:
    """``file:///Users/a/b%20c.xlsx`` (copied from a browser or Finder) -> a plain path."""

    if value[:7].lower() != "file://":
        return value
    rest = unquote(value[7:])
    if rest.lower().startswith("localhost/"):
        rest = rest[len("localhost"):]
    if re.match(r"^/[A-Za-z]:[\\/]", rest):
        return rest[1:]
    if rest.startswith("/"):
        return rest
    # file://server/share/x -> \\server\share\x
    return "\\\\" + rest.replace("/", "\\") if rest else value


def _unescape_shell_path(value: str) -> str:
    """A path dragged into a terminal comes out as ``/Users/a/my\\ file.xlsx``.

    Only POSIX-looking paths are touched (``/`` or ``~`` first), so Windows
    separators stay. A backslash before a letter or digit is left alone.
    """

    if not value.startswith(("/", "~")) or "\\" not in value:
        return value
    return re.sub(r"\\([^A-Za-z0-9])", r"\1", value)


def normalize_user_paths(values: Any) -> Any:
    """Normalize a list of user supplied paths while preserving its shape."""

    if not isinstance(values, list):
        return values
    return [normalize_user_path(value) for value in values]
