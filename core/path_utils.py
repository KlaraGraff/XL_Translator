"""Utilities for accepting paths copied from shells, file browsers, and chat."""

from __future__ import annotations

from typing import Any


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
    return normalized


def normalize_user_paths(values: Any) -> Any:
    """Normalize a list of user supplied paths while preserving its shape."""

    if not isinstance(values, list):
        return values
    return [normalize_user_path(value) for value in values]
