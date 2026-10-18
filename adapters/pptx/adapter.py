"""Translator's built-in 「PPT」 type: PowerPoint presentations (.pptx, .pptm, .ppsx, .potx,
and the old binary .ppt / .pps).

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
  shapes around it and the slide edges (a number the user types with a unit, ``2pt`` by default).
- Old .ppt / .pps files are first converted to a temporary .pptx
  (``core/ppt_converter.py``: the local PowerPoint, else LibreOffice).  Reading,
  extracting and writing one file share that copy, so it is converted once;
  it is deleted once the translation is written.  The output is .pptx.
"""

from __future__ import annotations

import atexit
from collections.abc import Mapping
from dataclasses import fields
from pathlib import Path
from typing import Any

from core.format_adapter import AdapterError, parse_number_value
from core.ppt_converter import PptConversionCache, PptConversionError, converted_name, is_legacy_ppt
from core.pptx_fit import DEFAULT_GAP_PT, LAYOUT_FIT, LAYOUT_MODES
from core.pptx_text import PptxError, Translation, extract_units, scan_file, write_file
from translator_adapter import MODE_BILINGUAL, OutputFile, ScanItem, TextUnit, TranslatedUnit

NOTES_OPTION = "translate_notes"
#: The target language the app passes along with the switches (``lang`` of the written text).
TARGET_LANG_OPTION = "target_lang"
#: 写回偏好: 自适应排版 ``fit`` (default) / 保持字号 ``keep_size`` / 固定版面 ``fixed``.
LAYOUT_OPTION = "layout_mode"
#: 与周围内容的间距：用户输入的数字连单位（如 ``2pt``、``1.5mm``）；各单位折成多少磅。
SPACING_OPTION = "spacing"
PT_PER_UNIT = {"pt": 1.0, "mm": 72 / 25.4, "cm": 72 / 2.54}

_TEXT_UNIT_FIELDS = {item.name for item in fields(TextUnit)}
_OUTPUT_FIELDS = {item.name for item in fields(OutputFile)}

#: 旧版文件转出的临时 .pptx：读取时转一次，取字、写出接着用，写出后删掉；
#: 读过却没翻译的，应用退出时删掉。
_LEGACY = PptConversionCache()
atexit.register(_LEGACY.cleanup)


def _layout_mode(options: Mapping[str, Any] | None) -> str:
    mode = str((options or {}).get(LAYOUT_OPTION) or "")
    return mode if mode in LAYOUT_MODES else LAYOUT_FIT


def _gap_pt(options: Mapping[str, Any] | None) -> float:
    parsed = parse_number_value((options or {}).get(SPACING_OPTION))
    if parsed is None or (parsed[1] and parsed[1] not in PT_PER_UNIT):
        return DEFAULT_GAP_PT
    number, unit = parsed
    return number * PT_PER_UNIT[unit or "pt"]


def _notes(options: Mapping[str, Any] | None) -> bool:
    return bool((options or {}).get(NOTES_OPTION, True))


def _readable(path: str) -> tuple[str, str]:
    """``(要读的 .pptx, 给用户的转换说明)``；新版文件原样返回、说明为空。"""
    if not is_legacy_ppt(path):
        return path, ""
    try:
        converted = _LEGACY.convert(path)
    except PptConversionError as exc:
        # 不记住失败：用户装好 PowerPoint 或另存后重新读取，要能再试。
        _LEGACY.release(path)
        raise AdapterError(str(exc)) from exc
    return str(converted.path), converted.note


def _output_path(source: str, out_path: str) -> str:
    """旧版文件输出 .pptx。

    旁边有同名的 .pptx（或另一种旧版格式）时，两份的译文会撞成同一个名字、后写的
    盖掉先写的；这时旧版这份的名字带上原来的扩展名，如 ``汇报_法文_ppt.pptx``。
    """
    if not is_legacy_ppt(source):
        return out_path
    target = Path(out_path).with_name(converted_name(Path(out_path).name))
    own = Path(source).suffix.lower()
    if any(Path(source).with_suffix(suffix).exists() for suffix in (".pptx", ".ppt", ".pps") if suffix != own):
        target = target.with_name(f"{target.stem}_{own.lstrip('.')}{target.suffix}")
    return str(target)


def scan(path: str, options: Mapping[str, Any] | None = None) -> ScanItem:
    readable, note = _readable(path)
    try:
        result = scan_file(readable, translate_notes=_notes(options))
    except PptxError as exc:
        _LEGACY.release(path)
        raise AdapterError(str(exc)) from exc
    warnings = [note, *result.warnings] if note else result.warnings
    return ScanItem(unit_count=result.unit_count, warnings=warnings)


def extract(path: str, options: Mapping[str, Any] | None = None) -> list[TextUnit]:
    readable, _note = _readable(path)
    try:
        units = extract_units(readable, translate_notes=_notes(options))
    except PptxError as exc:
        _LEGACY.release(path)
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
    readable, _note = _readable(path)
    try:
        result = write_file(
            readable,
            _output_path(path, out_path),
            translations,
            bilingual=mode == MODE_BILINGUAL,
            target_lang=str((options or {}).get(TARGET_LANG_OPTION) or "") or None,
            translate_notes=_notes(options),
            layout_mode=_layout_mode(options),
            gap_pt=_gap_pt(options),
        )
    except PptxError as exc:
        raise AdapterError(str(exc)) from exc
    finally:
        _LEGACY.release(path)
    review = [{"id": item.unit_id, "context": item.context, "reason": item.reason} for item in result.review]
    if "review_items" in _OUTPUT_FIELDS:
        return OutputFile(path=result.path, review_count=len(review), review_items=review)  # type: ignore[call-arg]
    return OutputFile(path=result.path, review_count=len(review))
