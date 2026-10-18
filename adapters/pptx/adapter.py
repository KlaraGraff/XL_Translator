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
- ``layout_mode`` (写回偏好) decides how text that does not fit is handled:
  ``fit`` (default) grows boxes into free space first and then shrinks,
  ``keep_size`` keeps the font size and only grows, ``fixed`` keeps every box
  and table where and as large as it is and only shrinks.
- ``spacing`` is how far a box or table grown under ``fit`` stays from the
  shapes around it and the slide edges (``mm2``, 2 mm, by default).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields
from typing import Any

from core.format_adapter import AdapterError
from core.pptx_fit import DEFAULT_GAP_MM, LAYOUT_FIT, LAYOUT_MODES
from core.pptx_text import PptxError, Translation, extract_units, scan_file, write_file
from translator_adapter import MODE_BILINGUAL, OutputFile, ScanItem, TextUnit, TranslatedUnit

NOTES_OPTION = "translate_notes"
#: The target language the app passes along with the switches (``lang`` of the written text).
TARGET_LANG_OPTION = "target_lang"
#: 写回偏好: 融合 ``fit`` (default) / 保持字号 ``keep_size`` / 固定版面 ``fixed``.
LAYOUT_OPTION = "layout_mode"
#: 与周围内容的间距：选项值 → 毫米。
SPACING_OPTION = "spacing"
SPACING_MM = {"none": 0.0, "mm1": 1.0, "mm2": 2.0, "mm4": 4.0}

_TEXT_UNIT_FIELDS = {item.name for item in fields(TextUnit)}
_OUTPUT_FIELDS = {item.name for item in fields(OutputFile)}


def _layout_mode(options: Mapping[str, Any] | None) -> str:
    mode = str((options or {}).get(LAYOUT_OPTION) or "")
    return mode if mode in LAYOUT_MODES else LAYOUT_FIT


def _gap_mm(options: Mapping[str, Any] | None) -> float:
    return SPACING_MM.get(str((options or {}).get(SPACING_OPTION) or ""), DEFAULT_GAP_MM)


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
    extracted = []
    for unit in units:
        values: dict[str, Any] = {
            "id": unit.id,
            "text": unit.text,
            "context": unit.context,
            "kind": unit.kind,
            "protected": list(unit.protected),
        }
        if unit.marked and "marked" in _TEXT_UNIT_FIELDS:
            values["marked"] = unit.marked
        extracted.append(TextUnit(**values))
    return extracted


def write(
    path: str,
    units: list[TranslatedUnit],
    mode: str,
    out_path: str,
    options: Mapping[str, Any],
) -> OutputFile:
    translations = {
        unit.id: Translation(unit.translation, str(getattr(unit, "marked_translation", "") or ""))
        for unit in units
    }
    try:
        result = write_file(
            path,
            out_path,
            translations,
            bilingual=mode == MODE_BILINGUAL,
            target_lang=str((options or {}).get(TARGET_LANG_OPTION) or "") or None,
            translate_notes=_notes(options),
            layout_mode=_layout_mode(options),
            gap_mm=_gap_mm(options),
        )
    except PptxError as exc:
        raise AdapterError(str(exc)) from exc
    review = [{"id": item.unit_id, "context": item.context, "reason": item.reason} for item in result.review]
    if "review_items" in _OUTPUT_FIELDS:
        return OutputFile(path=result.path, review_count=len(review), review_items=review)  # type: ignore[call-arg]
    return OutputFile(path=result.path, review_count=len(review))
