"""The data a format adapter exchanges with the app.

Every type converts to and from plain JSON values (``to_dict`` / ``from_dict``):
a bundled adapter runs inside the app, a third-party one will run in a child
process that only passes JSON, and both see the same types.

Standard library only.  Types only ever gain fields with defaults, so an
adapter written against an older ``ADAPTER_API_VERSION`` keeps working.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

#: The protocol version this package describes.  An adapter's manifest names
#: the version it was written for; the app accepts the versions it supports.
ADAPTER_API_VERSION = 1

#: ``write`` modes: only the translation, or the source followed by the translation.
MODE_TRANSLATION_ONLY = "translation_only"
MODE_BILINGUAL = "bilingual"
MODES = (MODE_TRANSLATION_ONLY, MODE_BILINGUAL)

#: ``TranslatedUnit.status``.  ``translation`` always holds the text to write:
#: for ``locked`` and ``untranslated`` units it is the source text itself.
STATUS_TRANSLATED = "translated"  # translated by the model
STATUS_MEMORY = "memory"  # taken whole from the translation memory
STATUS_LOCKED = "locked"  # marked ``locked`` by the adapter, never sent
STATUS_UNTRANSLATED = "untranslated"  # no usable translation: source kept, listed for review
STATUS_NEEDS_REVIEW = "needs_review"  # translated, but listed for review
STATUSES = (STATUS_TRANSLATED, STATUS_MEMORY, STATUS_LOCKED, STATUS_UNTRANSLATED, STATUS_NEEDS_REVIEW)


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _texts(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [_text(item) for item in value]


def _localized(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): _text(item) for key, item in value.items()}


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a JSON object, got {type(value).__name__}")
    return value


@dataclass
class ScanItem:
    """What ``scan(path)`` reports about one file before the task starts."""

    #: Translatable units in the file (shown in the file list).
    unit_count: int
    #: Short notes about the file for the user (e.g. "binary content skipped").
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"unit_count": int(self.unit_count), "warnings": list(self.warnings)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ScanItem:
        data = _mapping(data, "ScanItem")
        return cls(unit_count=int(data.get("unit_count") or 0), warnings=_texts(data.get("warnings")))


@dataclass
class TextUnit:
    """One piece of text ``extract(path)`` found: a paragraph, a heading, a cell…"""

    #: Unique within the file; ``write`` gets it back to put the translation in place.
    id: str
    text: str
    #: Where the text sits, as the user reads it in the review list
    #: ("line 12", "table B3"); left empty, the review list shows ``id``.
    context: str = ""
    #: What the text is ("paragraph", "heading", "list_item", "cell" …).
    kind: str = "paragraph"
    #: Never translated: written back as is (code blocks, front matter).
    locked: bool = False
    #: Substrings of ``text`` that must come back unchanged (inline code, link
    #: addresses).  The app hides them from the model and puts them back.
    protected: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "context": self.context,
            "kind": self.kind,
            "locked": bool(self.locked),
            "protected": list(self.protected),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TextUnit:
        data = _mapping(data, "TextUnit")
        return cls(
            id=_text(data.get("id")),
            text=_text(data.get("text")),
            context=_text(data.get("context")),
            kind=_text(data.get("kind")) or "paragraph",
            locked=bool(data.get("locked", False)),
            protected=_texts(data.get("protected")),
        )


@dataclass
class TranslatedUnit:
    """A ``TextUnit`` with its result, handed back to ``write``."""

    id: str
    #: The source text, as ``extract`` returned it.
    text: str
    #: The text to write in its place (the source itself when not translated).
    translation: str
    #: One of ``STATUSES``.
    status: str = STATUS_TRANSLATED

    @property
    def changed(self) -> bool:
        """The written text differs from the source (worth a second line in bilingual mode)."""
        return self.translation != self.text

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "text": self.text, "translation": self.translation, "status": self.status}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TranslatedUnit:
        data = _mapping(data, "TranslatedUnit")
        text = _text(data.get("text"))
        translation = data.get("translation")
        return cls(
            id=_text(data.get("id")),
            text=text,
            translation=text if translation is None else _text(translation),
            status=_text(data.get("status")) or STATUS_TRANSLATED,
        )


@dataclass
class OutputFile:
    """What ``write`` produced."""

    path: str
    #: Units the adapter itself marked for review while writing (usually 0).
    review_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"path": str(self.path), "review_count": int(self.review_count)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> OutputFile:
        data = _mapping(data, "OutputFile")
        return cls(path=_text(data.get("path")), review_count=int(data.get("review_count") or 0))


@dataclass
class OptionSpec:
    """A switch the page shows under 运行设置; same fields as the app's descriptor."""

    key: str
    label: dict[str, str]
    default: bool = False
    help: dict[str, str] = field(default_factory=dict)
    #: Only ``"toggle"`` for now.
    kind: str = "toggle"

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "kind": self.kind,
            "label": dict(self.label),
            "help": dict(self.help),
            "default": self.default,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> OptionSpec:
        data = _mapping(data, "OptionSpec")
        return cls(
            key=_text(data.get("key")),
            label=_localized(data.get("label")),
            default=bool(data.get("default", False)),
            help=_localized(data.get("help")),
            kind=_text(data.get("kind")) or "toggle",
        )
