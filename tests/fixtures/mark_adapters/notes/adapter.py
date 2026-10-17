"""A format adapter for tests only (``ext:notes``): one unit per non-empty line,
lines starting with ``NOTE:`` are notes.

``extract`` takes the page's switches: with ``translate_notes`` off, notes are
locked and never sent.  ``write`` puts ``target_lang`` on the first line and
asks for a review of every line containing ``VERTICAL``.
"""

from __future__ import annotations

from pathlib import Path

from translator_adapter import OutputFile, ReviewItem, TextUnit, TranslatedUnit


def extract(path: str, options: dict | None = None) -> list[TextUnit]:
    notes = True if options is None else bool(options.get("translate_notes", True))
    units = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if line.strip():
            locked = line.startswith("NOTE:") and not notes
            units.append(TextUnit(id=f"L{number}", text=line, context=f"line {number}", locked=locked))
    return units


def write(path: str, units: list[TranslatedUnit], mode: str, out_path: str, options: dict) -> OutputFile:
    lines = [f"lang={options.get('target_lang', '')}"] + [unit.translation for unit in units]
    Path(out_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    review = [
        ReviewItem(reason="竖排文本框中放入了西文", id=unit.id)
        for unit in units
        if "VERTICAL" in unit.text
    ]
    # A dict works as well; an entry without a reason is dropped.
    review.append({"reason": "缩到 60% 仍放不下", "context": "第 1 页 · 标题"})
    review.append({"reason": "", "id": "L1"})
    return OutputFile(path=out_path, review_items=review)
