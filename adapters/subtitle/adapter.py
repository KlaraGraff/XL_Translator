"""Translator's built-in 「字幕」 type: subtitle files (.srt, .vtt, .ass, .ssa).

Standard library and ``translator_adapter`` only, like any other adapter.

Only the words change.  Numbers, timings, cue settings, styles, comments and
the formatting codes inside a line (``<i>``, ``{\\an8}``, ``{\\i1}``) come back
exactly as written, so a file written back with every translation equal to its
source is byte-for-byte the original.

A sentence is often cut across two or three subtitles.  Translated one by one,
each half reads stiffly and the word order of the target language cannot
follow.  Consecutive subtitles up to the end of a sentence (at most
``MAX_GROUP_CUES``) are therefore sent as one unit, each subtitle wrapped in a
``<gN>…</gN>`` mark; the model puts each subtitle's share of the translation in
its mark.  When the marks do not come back whole, the translation is split by
the length of the source pieces at a word or punctuation boundary and the
subtitles are listed for review.

A line of two speakers (``- …`` / ``- …``) keeps one unit per speaker line.

The translation of a subtitle is wrapped onto at most two lines.  Timings are
never changed: a translation too long to read in its subtitle's time is written
anyway and listed for review.  In bilingual mode the source lines stay and the
translation follows on the next lines of the same subtitle.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from translator_adapter import MODE_BILINGUAL, OutputFile, ReviewItem, ScanItem, TextUnit, TranslatedUnit
from translator_adapter.text_file import TextSource, read_text_file

#: At most this many subtitles make one unit.
MAX_GROUP_CUES = 4
#: A unit grows no longer than this many characters.
MAX_GROUP_CHARS = 240
#: Subtitles further apart than this are never joined.
MAX_GROUP_GAP_MS = 1500
#: Width of one subtitle line, in half-width characters (a CJK character counts 2).
LINE_WIDTH = {"latin": 42, "cjk": 36}
#: Reading speed above which a subtitle is listed for review, in characters a second.
READING_SPEED = {"latin": 17.0, "cjk": 9.0}

_CJK_TARGETS = ("zh", "ja", "ko")

_NEWLINE = re.compile(r"\r\n|\r|\n")
_TIME = r"(?:\d+:)?\d{1,2}:\d{2}[,.]\d{1,3}"
_TIMING = re.compile(rf"^\s*({_TIME})\s*-->\s*({_TIME})")
_ASS_SECTION = re.compile(r"^\s*\[([^\]]+)\]\s*$")
_ASS_BREAK = re.compile(r"\\[Nn]")
_ASS_DRAWING = re.compile(r"\{[^{}]*\\p[1-9][^{}]*\}")
#: Formatting codes at the start of a line: ``<i>``, ``<v Bob>``, ``{\an8}``.
_LEAD = re.compile(r"^(?:\s*(?:<[^<>\n]*>|\{[^{}\n]*\}))*\s*")
#: Closing codes at the end of a line: ``</i>``, ``{\i0}``.
_TRAIL = re.compile(r"(?:\s*(?:</[^<>\n]*>|\{[^{}\n]*\}))*\s*$")
_DASH = re.compile(r"^[-‐–—]\s*")
_PROTECTED = re.compile(r"<[^<>\n]*>|\{[^{}\n]*\}|\\h|&(?:[A-Za-z]+|#\d+|#x[0-9A-Fa-f]+);")
_VOICE = re.compile(r"<v(?:\.[^\s>]*)?\s+([^>]*)>")
_MARK = re.compile(r"<\s*(/?)\s*g\s*(\d+)\s*>", re.IGNORECASE)
#: A subtitle ending with one of these ends its sentence (a trailing quote or bracket may follow).
_SENTENCE_END = re.compile(r"(?:[。！？!?；;]|(?<!\.)\.)[\"'”’」』）)\]]*$")
_BREAK_AFTER = frozenset("，,、；;：:。.！!？?…）)」』”’")
#: French and other languages put a space before these; never start a line with one.
_NO_LINE_START = frozenset("?!:;»%)]}”’")


# ── characters ──────────────────────────────────────────────────────────────


def _wide(char: str) -> bool:
    return unicodedata.east_asian_width(char) in ("W", "F")


def _width(text: str) -> int:
    return sum(2 if _wide(char) else 1 for char in text)


def _visible(text: str) -> str:
    return _PROTECTED.sub("", text)


def _has_letters(text: str) -> bool:
    return any(char.isalpha() for char in _visible(text))


def _joiner(left: str, right: str) -> str:
    """What goes between two pieces of one sentence: nothing between CJK, a space elsewhere."""
    left, right = _visible(left).rstrip(), _visible(right).lstrip()
    if left and right and _wide(left[-1]) and _wide(right[0]):
        return ""
    return " "


def _script(target_lang: str) -> str:
    code = str(target_lang or "").lower()
    return "cjk" if code.startswith(_CJK_TARGETS) else "latin"


def _parse_time(value: str) -> int:
    """``00:01:05,200`` / ``01:05.200`` / ``0:01:05.20`` → milliseconds."""
    clock, _, fraction = value.replace(",", ".").partition(".")
    seconds = 0
    for piece in clock.split(":"):
        seconds = seconds * 60 + int(piece)
    return seconds * 1000 + int((fraction + "000")[:3])


def _clock(ms: int) -> str:
    seconds = ms // 1000
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


# ── the file ────────────────────────────────────────────────────────────────


@dataclass
class _Line:
    body: str
    ending: str


@dataclass
class _Part:
    """One piece of a subtitle that is translated as a whole: the subtitle, or one speaker line."""

    #: Dash and formatting codes before the words, kept as written.
    lead: str
    #: Closing codes after the words, kept as written.
    trail: str
    #: The words, the subtitle's lines joined into one.
    text: str
    #: A ``- …`` line of a subtitle with two speakers: never wrapped, never joined.
    dialogue: bool = False
    unit_id: str = ""


@dataclass
class _Cue:
    number: int
    start: int
    end: int
    #: Subtitles of different styles or speakers are never joined.
    key: str
    #: The text lines as written (for .ass, the pieces between ``\\N``).
    lines: list[str]
    parts: list[_Part] = field(default_factory=list)
    #: .srt / .vtt: the text lines are ``document.lines[first:last]``.
    first: int = 0
    last: int = 0
    #: .ass: the line index and where its text field starts.
    line: int = 0
    offset: int = 0


@dataclass
class _Group:
    unit: TextUnit
    #: (cue, part) in order; the n-th is wrapped in ``<g{n}>``.
    members: list[tuple[_Cue, _Part]]


@dataclass
class _Document:
    source: TextSource
    kind: str
    lines: list[_Line]
    newline: str
    cues: list[_Cue] = field(default_factory=list)
    groups: list[_Group] = field(default_factory=list)


def _split_lines(text: str) -> list[_Line]:
    lines: list[_Line] = []
    start = 0
    for match in _NEWLINE.finditer(text):
        lines.append(_Line(text[start:match.start()], match.group(0)))
        start = match.end()
    if start < len(text):
        lines.append(_Line(text[start:], ""))
    return lines


def _newline(lines: list[_Line]) -> str:
    endings = [line.ending for line in lines if line.ending]
    return max(set(endings), key=endings.count) if endings else "\n"


def _kind(path: str | Path) -> str:
    suffix = Path(path).suffix.lower()
    return {".ass": "ass", ".ssa": "ass", ".vtt": "vtt"}.get(suffix, "srt")


def _parse_timed_blocks(document: _Document) -> None:
    """.srt and .vtt: a timing line, then the text lines up to a blank line."""
    lines = document.lines
    index = 0
    while index < len(lines):
        match = _TIMING.match(lines[index].body)
        if not match:
            index += 1
            continue
        first = index + 1
        last = first
        while last < len(lines) and lines[last].body.strip():
            # .srt files sometimes miss the blank line before the next number.
            if (
                document.kind == "srt"
                and lines[last].body.strip().isdigit()
                and last + 1 < len(lines)
                and _TIMING.match(lines[last + 1].body)
            ):
                break
            if _TIMING.match(lines[last].body):
                break
            last += 1
        texts = [line.body for line in lines[first:last]]
        voice = _VOICE.search(texts[0]) if texts else None
        cue = _Cue(
            number=len(document.cues) + 1,
            start=_parse_time(match.group(1)),
            end=_parse_time(match.group(2)),
            key=voice.group(1).strip() if voice else "",
            lines=texts,
            first=first,
            last=last,
        )
        document.cues.append(cue)
        index = max(last, index + 1)


def _parse_ass(document: _Document) -> None:
    """.ass / .ssa: the text field of each ``Dialogue:`` line in ``[Events]``."""
    section = ""
    fields: list[str] = []
    for index, line in enumerate(document.lines):
        heading = _ASS_SECTION.match(line.body)
        if heading:
            section = heading.group(1).strip().lower()
            continue
        if section != "events":
            continue
        name, colon, rest = line.body.partition(":")
        if not colon:
            continue
        name = name.strip().lower()
        if name == "format":
            fields = [item.strip().lower() for item in rest.split(",")]
            continue
        if name != "dialogue" or not fields or "text" not in fields or fields[-1] != "text":
            continue
        values = rest.split(",", len(fields) - 1)
        if len(values) != len(fields):
            continue
        offset = len(line.body) - len(values[-1])
        text = values[-1]
        if _ASS_DRAWING.search(text):
            continue
        record = dict(zip(fields, (value.strip() for value in values[:-1])))
        try:
            start, end = _parse_time(record.get("start", "")), _parse_time(record.get("end", ""))
        except ValueError:
            continue
        document.cues.append(
            _Cue(
                number=len(document.cues) + 1,
                start=start,
                end=end,
                key=f"{record.get('style', '')}|{record.get('name', '')}",
                lines=_ASS_BREAK.split(text),
                line=index,
                offset=offset,
            )
        )


def _split_part(text: str, *, dialogue: bool) -> _Part:
    lead = _LEAD.match(text).group(0)
    rest = text[len(lead):]
    if dialogue:
        dash = _DASH.match(rest)
        if dash:
            lead += dash.group(0)
            rest = rest[dash.end():]
    trail = _TRAIL.search(rest).group(0)
    return _Part(lead=lead, trail=trail, text=rest[: len(rest) - len(trail)], dialogue=dialogue)


def _parts(cue: _Cue) -> list[_Part]:
    lines = [line for line in cue.lines if line.strip()]
    if not lines or len(lines) != len(cue.lines):
        # Nothing to translate, or an empty line inside the text (``a\\N\\Nb`` in .ass): written back as is.
        return []
    if len(lines) > 1 and any(_DASH.match(_LEAD.sub("", line, count=1)) for line in lines[1:]):
        return [_split_part(line, dialogue=True) for line in lines]
    first = _LEAD.match(lines[0]).group(0)
    last_trail = _TRAIL.search(lines[-1]).group(0)
    if len(lines) == 1:
        inner = [lines[0][len(first): len(lines[0]) - len(last_trail)]]
    else:
        inner = [lines[0][len(first):], *lines[1:-1], lines[-1][: len(lines[-1]) - len(last_trail)]]
    inner = [piece.strip() for piece in inner]
    text = inner[0]
    for piece in inner[1:]:
        text += _joiner(text, piece) + piece
    return [_Part(lead=first, trail=last_trail, text=text)]


def _ends_sentence(text: str) -> bool:
    return bool(_SENTENCE_END.search(_visible(text).rstrip()))


def _where(first: _Cue, last: _Cue, part: _Part | None = None) -> str:
    if part is not None and part.dialogue:
        line = first.parts.index(part) + 1
        return f"第 {first.number} 条第 {line} 行 · {_clock(first.start)}"
    if first is last:
        return f"第 {first.number} 条 · {_clock(first.start)}"
    return f"第 {first.number}–{last.number} 条 · {_clock(first.start)}"


def _group(document: _Document) -> None:
    pending: list[tuple[_Cue, _Part]] = []

    def flush() -> None:
        if not pending:
            return
        first, last = pending[0][0], pending[-1][0]
        unit_id = f"c{first.number}" if first is last else f"c{first.number}-{last.number}"
        texts = [part.text for _cue, part in pending]
        text, marked = texts[0], ""
        if len(pending) > 1:
            marked = f"<g1>{texts[0]}</g1>"
            for number, piece in enumerate(texts[1:], start=2):
                gap = _joiner(text, piece)
                text += gap + piece
                marked += f"{gap}<g{number}>{piece}</g{number}>"
        protected = list(dict.fromkeys(_PROTECTED.findall(text)))
        unit = TextUnit(
            id=unit_id,
            text=text,
            context=_where(first, last),
            kind="subtitle",
            protected=protected,
            marked=marked,
        )
        for _cue, part in pending:
            part.unit_id = unit_id
        document.groups.append(_Group(unit, list(pending)))
        pending.clear()

    for cue in document.cues:
        cue.parts = _parts(cue)
        joinable = len(cue.parts) == 1 and not cue.parts[0].dialogue and _has_letters(cue.parts[0].text)
        if not joinable:
            flush()
            for number, part in enumerate(cue.parts, start=1):
                if not part.dialogue or not _has_letters(part.text):
                    continue
                unit_id = f"c{cue.number}.{number}"
                part.unit_id = unit_id
                unit = TextUnit(
                    id=unit_id,
                    text=part.text,
                    context=_where(cue, cue, part),
                    kind="subtitle",
                    protected=list(dict.fromkeys(_PROTECTED.findall(part.text))),
                )
                document.groups.append(_Group(unit, [(cue, part)]))
            continue
        part = cue.parts[0]
        if pending:
            previous_cue, previous_part = pending[-1]
            size = sum(len(member.text) for _c, member in pending) + len(part.text)
            if (
                _ends_sentence(previous_part.text)
                or len(pending) >= MAX_GROUP_CUES
                or size > MAX_GROUP_CHARS
                or cue.key != previous_cue.key
                or cue.start < previous_cue.start
                or cue.start - previous_cue.end > MAX_GROUP_GAP_MS
            ):
                flush()
        pending.append((cue, part))
    flush()


def _parse(path: str | Path) -> _Document:
    source = read_text_file(path)
    lines = _split_lines(source.text)
    document = _Document(source=source, kind=_kind(path), lines=lines, newline=_newline(lines))
    if document.kind == "ass":
        _parse_ass(document)
    else:
        _parse_timed_blocks(document)
    _group(document)
    return document


def extract(path: str) -> list[TextUnit]:
    return [group.unit for group in _parse(path).groups]


def scan(path: str) -> ScanItem:
    document = _parse(path)
    warnings = [] if document.cues else ["没有找到字幕内容，可能不是字幕文件。"]
    return ScanItem(unit_count=len(document.groups), warnings=warnings)


# ── splitting a sentence back over its subtitles ────────────────────────────


def _from_marks(marked: str, count: int) -> list[str] | None:
    """The text of each ``<gN>`` in ``marked``.

    Words before the first mark join the first mark; words after a mark join it.
    ``None`` when a mark is missing, repeated, nested or left empty.
    """
    if not marked:
        return None
    pieces: dict[int, str] = {}
    order: list[int] = []
    leading = ""
    position = 0
    current: int | None = None
    for match in _MARK.finditer(marked):
        closing, number = match.group(1) == "/", int(match.group(2))
        chunk = marked[position:match.start()]
        position = match.end()
        if current is not None:
            if not closing or number != current:
                return None
            pieces[number] += chunk
            current = None
            continue
        if order:
            pieces[order[-1]] += chunk
        else:
            leading = chunk
        if closing or number in pieces or not 1 <= number <= count:
            return None
        current = number
        pieces[number] = ""
        order.append(number)
    if current is not None or len(pieces) != count:
        return None
    pieces[order[-1]] += marked[position:]
    pieces[order[0]] = leading + pieces[order[0]]
    result = [" ".join(pieces[number].split()) if not any(_wide(c) for c in pieces[number]) else pieces[number].strip() for number in range(1, count + 1)]
    return result if all(result) else None


def _boundaries(text: str) -> list[int]:
    """Places ``text`` may be cut: after a space, next to a CJK character, never inside a code."""
    inside = set()
    for match in _PROTECTED.finditer(text):
        inside.update(range(match.start() + 1, match.end()))
    spots = []
    for index in range(1, len(text)):
        left, right = text[index - 1], text[index]
        if index in inside or right.isspace() or right in _NO_LINE_START:
            continue
        if left.isspace() or _wide(left) or _wide(right):
            spots.append(index)
    return spots


def _after_punctuation(text: str, spot: int) -> bool:
    before = text[:spot].rstrip()
    return bool(before) and before[-1] in _BREAK_AFTER


def _split_by_length(text: str, weights: list[int]) -> list[str]:
    """Cut ``text`` into ``len(weights)`` pieces in proportion, at the nearest boundary."""
    spots = _boundaries(text)
    total = sum(weights) or 1
    cuts: list[int] = []
    running = 0
    for weight in weights[:-1]:
        running += weight
        goal = len(text) * running / total
        options = [spot for spot in spots if not cuts or spot > cuts[-1]]
        if not options:
            break
        # Prefer a cut after punctuation when it is nearly as close.
        cuts.append(min(options, key=lambda spot: abs(spot - goal) - (3 if _after_punctuation(text, spot) else 0)))
    pieces, start = [], 0
    for cut in cuts:
        pieces.append(text[start:cut].strip())
        start = cut
    pieces.append(text[start:].strip())
    while len(pieces) < len(weights):
        pieces.append("")
    return pieces


# ── wrapping a subtitle ─────────────────────────────────────────────────────


def _wrap(text: str, limit: int) -> tuple[list[str], bool]:
    """At most two lines; the second value says whether two lines of ``limit`` were not enough."""
    text = " ".join(text.split()) if not any(_wide(char) for char in text) else text.strip()
    if _width(_visible(text)) <= limit:
        return [text], False
    best: tuple[float, int] | None = None
    for spot in _boundaries(text):
        left, right = text[:spot].rstrip(), text[spot:].lstrip()
        if not _visible(left).strip() or not _visible(right).strip():
            continue
        widths = (_width(_visible(left)), _width(_visible(right)))
        # Balanced lines, slightly preferring a cut after punctuation.
        score = max(widths) - (2 if _after_punctuation(text, spot) else 0)
        if best is None or score < best[0]:
            best = (score, spot)
    if best is None:
        return [text], True
    spot = best[1]
    lines = [text[:spot].rstrip(), text[spot:].lstrip()]
    return lines, max(_width(_visible(line)) for line in lines) > limit


# ── writing ─────────────────────────────────────────────────────────────────


def _distribute(document: _Document, units: list[TranslatedUnit]) -> tuple[dict[int, str], list[ReviewItem]]:
    """The translated words of every part (keyed by ``id(part)``) and review items for split sentences."""
    by_id = {unit.id: unit for unit in units}
    words: dict[int, str] = {}
    review: list[ReviewItem] = []
    for group in document.groups:
        translated = by_id.get(group.unit.id)
        if (
            translated is None
            or not translated.changed
            or translated.text != group.unit.text
            or not translated.translation.strip()
        ):
            continue
        parts = [part for _cue, part in group.members]
        if len(parts) == 1:
            words[id(parts[0])] = translated.translation.strip()
            continue
        pieces = _from_marks(translated.marked_translation, len(parts))
        if pieces is None:
            pieces = _split_by_length(translated.translation.strip(), [max(1, _width(_visible(part.text))) for part in parts])
            first, last = group.members[0][0], group.members[-1][0]
            review.append(
                ReviewItem(
                    reason="一句话分在几条字幕里，译文是按长短比例分开的，请检查断开的位置",
                    id=group.unit.id,
                    context=_where(first, last),
                )
            )
        for part, piece in zip(parts, pieces):
            words[id(part)] = piece
    return words, review


_ASS_CODES = re.compile(r"\{[^{}\n]*\}")


def _part_lines(part: _Part, words: str, limit: int, *, after_source: bool) -> tuple[list[str], bool]:
    lead, trail = part.lead, part.trail
    if after_source:
        # .ass codes ({\an8}, colours) hold on after a \N: not repeated under the source.
        lead, trail = _ASS_CODES.sub("", lead), _ASS_CODES.sub("", trail)
    if part.dialogue:
        return [f"{lead}{words}{trail}"], _width(_visible(words)) > limit
    lines, too_long = _wrap(words, limit)
    lines[0] = lead + lines[0]
    lines[-1] = lines[-1] + trail
    return lines, too_long


def write(path: str, units: list[TranslatedUnit], mode: str, out_path: str, options: dict) -> OutputFile:
    document = _parse(path)
    words, review = _distribute(document, units)
    script = _script(str((options or {}).get("target_lang") or ""))
    limit, speed = LINE_WIDTH[script], READING_SPEED[script]
    bilingual = mode == MODE_BILINGUAL
    replaced: dict[int, tuple[_Cue, list[str]]] = {}  # first text line → new text lines (.srt / .vtt)
    ass_text: dict[int, tuple[_Cue, str]] = {}  # line → new text field (.ass)
    for cue in document.cues:
        if not any(id(part) in words for part in cue.parts):
            continue
        new_lines: list[str] = []
        too_long = False
        shown = ""
        for part in cue.parts:
            if id(part) in words:
                lines, long = _part_lines(part, words[id(part)], limit, after_source=bilingual and document.kind == "ass")
                too_long = too_long or long
                shown += words[id(part)]
            else:
                lines = [f"{part.lead}{part.text}{part.trail}"]
            new_lines.extend(lines)
        seconds = max(0.001, (cue.end - cue.start) / 1000)
        rate = len("".join(_visible(shown).split())) / seconds
        if too_long or (cue.end > cue.start and rate > speed):
            review.append(
                ReviewItem(
                    reason=f"译文较长，按正常语速可能读不完（每秒约 {math.ceil(rate)} 字）"
                    if rate > speed
                    else "译文较长，两行放不下",
                    id=next((part.unit_id for part in cue.parts if part.unit_id), ""),
                    context=f"第 {cue.number} 条 · {_clock(cue.start)}",
                )
            )
        if document.kind == "ass":
            original = document.lines[cue.line].body[cue.offset:]
            ass_text[cue.line] = (cue, (original + "\\N" if bilingual else "") + "\\N".join(new_lines))
        else:
            replaced[cue.first] = (cue, (cue.lines + new_lines) if bilingual else new_lines)

    parts: list[str] = []
    skip_until = -1
    for index, line in enumerate(document.lines):
        if index < skip_until:
            continue
        if index in ass_text:
            cue, text = ass_text[index]
            parts.append(line.body[: cue.offset] + text + line.ending)
        elif index in replaced:
            cue, texts = replaced[index]
            # The last text line keeps its own ending (none at the end of the file).
            parts.append(document.newline.join(texts) + document.lines[cue.last - 1].ending)
            skip_until = cue.last
        else:
            parts.append(line.body + line.ending)
    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(document.source.encode("".join(parts)))
    return OutputFile(path=str(target), review_items=review)
