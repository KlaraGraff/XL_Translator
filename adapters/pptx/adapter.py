"""Translator's built-in 「PPT」 type: PowerPoint presentations (.pptx, .pptm, .ppsx, .potx).

A thin shell over ``core/pptx_text.py``, which does the reading and writing.
Bundled with the app and loaded in its process, so unlike a third-party
adapter it may import app modules.

- ``extract``: one unit per paragraph, slides in presentation order, each
  slide followed by its speaker notes, then the fixed text on masters and
  layouts.  ``marked`` carries the paragraph with ``<gN>`` marks around the
  parts formatted differently from the rest (bold words, links).
- ``write``: translation only keeps every paragraph in place; bilingual adds
  the translation as its own paragraph right after the source, in the same box.
- ``translate_notes`` (on by default) decides whether speaker notes are
  translated; switched off, the notes keep the source text.
- ``fixed_layout`` (off by default) keeps every text box and table where and
  as large as it is when fitting the translation; only the font shrinks.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.format_adapter import TARGET_LANG_OPTION_KEY, AdapterError
from core.pptx_text import PptxError, Translation, extract_units, scan_file, write_file
from translator_adapter import MODE_BILINGUAL, OutputFile, ReviewItem, ScanItem, TextUnit, TranslatedUnit

NOTES_OPTION = "translate_notes"
#: 「固定版面」 (off by default): boxes and tables keep their size and place,
#: text that does not fit only gets smaller.
FIXED_LAYOUT_OPTION = "fixed_layout"


def _notes(options: Mapping[str, Any] | None) -> bool:
    return bool((options or {}).get(NOTES_OPTION, True))


def scan(path: str, options: Mapping[str, Any] | None = None) -> ScanItem:
    try:
        result = scan_file(path, translate_notes=_notes(options))
    except PptxError as exc:
        raise AdapterError(str(exc)) from exc
    return ScanItem(unit_count=result.unit_count, warnings=result.warnings)


def extract(path: str, options: Mapping[str, Any] | None = None) -> list[TextUnit]:
    try:
        units = extract_units(path, translate_notes=_notes(options))
    except PptxError as exc:
        raise AdapterError(str(exc)) from exc
    return [
        TextUnit(
            id=unit.id,
            text=unit.text,
            context=unit.context,
            kind=unit.kind,
            protected=list(unit.protected),
            marked=unit.marked,
        )
        for unit in units
    ]


def write(
    path: str,
    units: list[TranslatedUnit],
    mode: str,
    out_path: str,
    options: Mapping[str, Any],
) -> OutputFile:
    translations = {unit.id: Translation(unit.translation, unit.marked_translation) for unit in units}
    try:
        result = write_file(
            path,
            out_path,
            translations,
            bilingual=mode == MODE_BILINGUAL,
            target_lang=str((options or {}).get(TARGET_LANG_OPTION_KEY) or "") or None,
            translate_notes=_notes(options),
            fixed_layout=bool((options or {}).get(FIXED_LAYOUT_OPTION, False)),
        )
    except PptxError as exc:
        raise AdapterError(str(exc)) from exc
    review = [ReviewItem(reason=item.reason, id=item.unit_id, context=item.context) for item in result.review]
    return OutputFile(path=result.path, review_items=review)
