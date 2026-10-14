"""A format adapter for tests only (``ext:echo``): one unit per non-empty line.

Lines starting with ``#!`` are locked; ``{{name}}`` placeholders are protected.
``write`` puts each unit's translation back on its line (bilingual: the source
line, then the translation on the next line when it differs), upper-casing
the translations when the ``upper`` switch is on.  Standard library and
``translator_adapter`` only, like a real adapter.
"""

from __future__ import annotations

import re
from pathlib import Path

from translator_adapter import MODE_BILINGUAL, OutputFile, ScanItem, TextUnit, TranslatedUnit

_PLACEHOLDER = re.compile(r"\{\{\w+\}\}")


def _lines(path: str) -> list[str]:
    return Path(path).read_text(encoding="utf-8").splitlines(keepends=True)


def extract(path: str) -> list[TextUnit]:
    units = []
    for number, line in enumerate(_lines(path), start=1):
        text = line.rstrip("\r\n")
        if not text.strip():
            continue
        units.append(
            TextUnit(
                id=f"L{number}",
                text=text,
                context=f"line {number}",
                locked=text.startswith("#!"),
                protected=_PLACEHOLDER.findall(text),
            )
        )
    return units


def scan(path: str) -> ScanItem:
    units = [unit for unit in extract(path) if not unit.locked]
    return ScanItem(unit_count=len(units), warnings=[] if units else ["no text"])


def write(path: str, units: list[TranslatedUnit], mode: str, out_path: str, options: dict) -> OutputFile:
    by_id = {unit.id: unit for unit in units}
    upper = bool(options.get("upper"))
    out: list[str] = []
    for number, line in enumerate(_lines(path), start=1):
        unit = by_id.get(f"L{number}")
        if unit is None:
            out.append(line)
            continue
        ending = line[len(line.rstrip("\r\n")):]
        translation = unit.translation.upper() if upper and unit.changed else unit.translation
        if mode == MODE_BILINGUAL and unit.changed:
            out.append(unit.text + (ending or "\n"))
            out.append(translation + ending)
        else:
            out.append(translation + ending)
    Path(out_path).write_text("".join(out), encoding="utf-8", newline="")
    return OutputFile(path=out_path)
