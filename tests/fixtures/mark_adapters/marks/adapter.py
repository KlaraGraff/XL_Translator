"""A format adapter for tests only (``ext:marks``): one unit per non-empty line,
``*word*`` is bold.

``extract`` hands the bold stretches over as paired marks (``TextUnit.marked``,
``<g1>…</g1>``); ``write`` turns the marks of ``marked_translation`` back into
``*…*`` and writes the plain translation when there are none.  Standard
library and ``translator_adapter`` only.
"""

from __future__ import annotations

import re
from pathlib import Path

from translator_adapter import OutputFile, TextUnit, TranslatedUnit

_BOLD = re.compile(r"\*([^*]+)\*")
_MARK = re.compile(r"</?g\d+>")


def _marked(line: str) -> str:
    count = 0

    def mark(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return f"<g{count}>{match.group(1)}</g{count}>"

    return _BOLD.sub(mark, line)


def extract(path: str) -> list[TextUnit]:
    units = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        marked = _marked(line)
        units.append(
            TextUnit(
                id=f"L{number}",
                text=_MARK.sub("", marked),
                context=f"line {number}",
                marked=marked if marked != line else "",
            )
        )
    return units


def write(path: str, units: list[TranslatedUnit], mode: str, out_path: str, options: dict) -> OutputFile:
    by_id = {unit.id: unit for unit in units}
    out = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        unit = by_id.get(f"L{number}")
        if unit is None:
            out.append(line)
        elif unit.marked_translation:
            out.append(re.sub(r"</?g\d+>", "*", unit.marked_translation))
        else:
            out.append(unit.translation)
    Path(out_path).write_text("\n".join(out) + "\n", encoding="utf-8")
    return OutputFile(path=out_path)
