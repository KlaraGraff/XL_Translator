"""Translator's built-in 「文本」 type: plain text (.txt) and Markdown (.md, .markdown).

Standard library and ``translator_adapter`` only, like any other adapter.

- ``.txt``: one unit per paragraph (the lines between blank lines).
- ``.md``: headings, paragraphs, list items, block quotes and table cells are
  units.  Fenced and indented code, HTML blocks, YAML front matter, link
  reference definitions and table separator rows are locked.  Inline code,
  link addresses, autolinks, bare URLs, e-mail addresses and inline HTML tags
  are protected, so they come back exactly as written.

``write`` puts each translation where its source was, keeping every
character around it (markers, indentation, trailing spaces, line endings,
the byte-order mark).  A file written back with every translation equal to
its source is therefore byte-for-byte the original.

In bilingual mode a paragraph or quote is followed by its translation as a
paragraph of its own.  A list item or footnote keeps its translation on the
next line of the same item (a hard line break, no blank line), so a tight list
stays tight.  A heading or table cell, which must stay on one line, reads
``source / translation``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from translator_adapter import MODE_BILINGUAL, OutputFile, ScanItem, TextUnit, TranslatedUnit
from translator_adapter.text_file import TextSource, read_text_file

MARKDOWN_SUFFIXES = frozenset({".md", ".markdown"})
#: Longer paragraphs are cut at line boundaries so one unit stays a comfortable request.
MAX_UNIT_CHARS = 1500
#: Between source and translation on one line (headings, table cells) in bilingual mode.
INLINE_SEPARATOR = " / "
#: Units whose translation follows on the next line of the same block in bilingual mode:
#: a blank line inside a list item would turn a tight list into a loose one.
_SAME_BLOCK_KINDS = frozenset({"list_item", "footnote"})

_NEWLINE = re.compile(r"\r\n|\r|\n")


# ── reading ─────────────────────────────────────────────────────────────────


@dataclass
class _Line:
    number: int
    #: Container markers already consumed (``"> "`` inside a block quote).
    prefix: str
    body: str
    ending: str


def _split_lines(text: str) -> list[_Line]:
    lines: list[_Line] = []
    start = 0
    for number, match in enumerate(_NEWLINE.finditer(text), start=1):
        lines.append(_Line(number, "", text[start:match.start()], match.group(0)))
        start = match.end()
    if start < len(text):
        lines.append(_Line(len(lines) + 1, "", text[start:], ""))
    return lines


def _where(number: int) -> str:
    return f"第 {number} 行"


def _has_letters(text: str) -> bool:
    return any(char.isalpha() for char in text)


# ── protected fragments ─────────────────────────────────────────────────────

_CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)", re.S)
_AUTOLINK = re.compile(r"<(?:[A-Za-z][A-Za-z0-9+.-]{1,31}:[^<>\s]*|[^<>\s@]+@[^<>\s@]+)>")
# ``](address "title")`` of a link or image: the address part, after the ``]``.
_LINK_DESTINATION = re.compile(r"(?<=\])\((?:[^()\n]|\([^()\n]*\))*\)")
# ``[text][label]``: the label part.
_LINK_LABEL = re.compile(r"(?<=\])\[[^\]\n]*\]")
_FOOTNOTE = re.compile(r"\[\^[^\]\s]+\]")
_HTML_TAG = re.compile(r"</?[A-Za-z][A-Za-z0-9-]*(?:\s+[^<>]*?)?/?>")
_URL = re.compile(r"(?:https?|ftp)://[^\s<>\"'`　-〿一-鿿＀-￯]+")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_URL_TRAILING = ".,;:!?)]}'\""

_MARKDOWN_PATTERNS = (_CODE_SPAN, _AUTOLINK, _LINK_DESTINATION, _LINK_LABEL, _FOOTNOTE, _HTML_TAG, _URL, _EMAIL)
_TEXT_PATTERNS = (_URL, _EMAIL)


def _protected(text: str, patterns: tuple[re.Pattern[str], ...]) -> list[str]:
    """Fragments of ``text`` that must survive translation, earlier patterns first."""
    taken: list[tuple[int, int]] = []
    for pattern in patterns:
        for match in pattern.finditer(text):
            start, end = match.span()
            if pattern is _URL:
                end = start + len(match.group(0).rstrip(_URL_TRAILING))
            if end <= start or any(start < other_end and other_start < end for other_start, other_end in taken):
                continue
            taken.append((start, end))
    return [text[start:end] for start, end in sorted(taken)]


# ── the document: literal text with translatable slots ──────────────────────


@dataclass
class _Record:
    """One line of a unit: what comes before its text, the text, after it."""

    number: int
    lead: str
    content: str
    trail: str
    ending: str


def _record(line: _Line, marker: str = "") -> _Record:
    body = line.body[len(marker):]
    content = body.strip()
    if not content:
        return _Record(line.number, line.prefix + marker + body, "", "", line.ending)
    start = len(body) - len(body.lstrip())
    end = len(body.rstrip())
    return _Record(line.number, line.prefix + marker + body[:start], body[start:end], body[end:], line.ending)


@dataclass
class _Slot:
    unit: TextUnit
    #: Before each line's text; line 0's is written just before the slot.
    leads: list[str]
    trails: list[str]
    #: Line endings between the slot's lines.
    breaks: list[str]
    contents: list[str]
    #: Before each line a translation adds beyond the source's line count.
    continuation: str
    #: Headings and table cells keep the translation on one line.
    inline: bool = False

    def render_source(self) -> str:
        parts: list[str] = []
        for index, content in enumerate(self.contents):
            if index:
                parts += [self.breaks[index - 1], self.leads[index]]
            parts += [content, self.trails[index]]
        return "".join(parts)

    def _lines(self, text: str) -> list[str]:
        pieces = [piece.strip() for piece in _NEWLINE.split(text)]
        if self.inline:
            return [" ".join(piece for piece in pieces if piece)]
        while len(pieces) > 1 and not pieces[-1]:
            pieces.pop()
        while len(pieces) > 1 and not pieces[0]:
            pieces.pop(0)
        return pieces

    def render(self, text: str, newline: str) -> str:
        lines = self._lines(text)
        count = len(self.contents)
        parts: list[str] = []
        for index, line in enumerate(lines):
            if index:
                parts.append(self.breaks[index - 1] if index - 1 < len(self.breaks) else newline)
                lead = self.leads[index] if index < count else self.continuation
                parts.append(lead if line else lead.rstrip())
            parts.append(line)
            if index == len(lines) - 1:
                parts.append(self.trails[-1])
            elif index < count - 1:
                parts.append(self.trails[index])
        return "".join(parts)

    def render_bilingual(self, translation: str, newline: str) -> str:
        if self.inline:
            return self.render(self.unit.text + INLINE_SEPARATOR + self._lines(translation)[0], newline)
        rest = self.render(translation, newline)
        if self.trails[-1]:
            rest = rest[: -len(self.trails[-1])]
        if self.unit.kind in _SAME_BLOCK_KINDS:
            source = self.render_source().rstrip(" \t")
            # The hard break is a trailing backslash, or two spaces when the source itself
            # ends with a backslash (a second one would only escape it).
            hard_break = "  " if source.endswith("\\") else "\\"
            return source + hard_break + newline + self.continuation + rest
        blank = self.continuation.rstrip()
        return self.render_source() + newline + blank + newline + self.continuation + rest


class _Document:
    def __init__(self, source: TextSource, protect: tuple[re.Pattern[str], ...]) -> None:
        self.source = source
        self.lines = _split_lines(source.text)
        self.newline = next((line.ending for line in self.lines if line.ending), "\n")
        self.pieces: list[str | _Slot] = []
        self.units: list[TextUnit] = []
        self._protect = protect

    def raw(self, *lines: _Line) -> None:
        for line in lines:
            self.pieces.append(line.prefix + line.body + line.ending)

    def locked(self, lines: list[_Line], kind: str) -> None:
        self.raw(*lines)
        self.units.append(
            TextUnit(
                id=f"L{lines[0].number}",
                text="\n".join(line.prefix + line.body for line in lines),
                context=_where(lines[0].number),
                kind=kind,
                locked=True,
            )
        )

    def text(self, records: list[_Record], kind: str, continuation: str, *, inline: bool = False) -> None:
        for chunk in [records] if inline else _chunks(records):
            if not any(record.content for record in chunk):
                for record in chunk:
                    self.pieces.append(record.lead + record.content + record.trail + record.ending)
                continue
            first = chunk[0]
            text = "\n".join(record.content for record in chunk)
            unit = TextUnit(
                id=f"L{first.number}",
                text=text,
                context=_where(first.number),
                kind=kind,
                protected=_protected(text, self._protect),
            )
            self.pieces.append(first.lead)
            self.pieces.append(
                _Slot(
                    unit=unit,
                    leads=[record.lead for record in chunk],
                    trails=[record.trail for record in chunk],
                    breaks=[record.ending for record in chunk[:-1]],
                    contents=[record.content for record in chunk],
                    continuation=continuation,
                    inline=inline,
                )
            )
            self.pieces.append(chunk[-1].ending)
            self.units.append(unit)

    def table_row(self, line: _Line) -> None:
        body = line.body
        cuts = _pipe_positions(body)
        bounds = [-1, *cuts, len(body)]
        self.pieces.append(line.prefix)
        column = 0
        for index in range(len(bounds) - 1):
            if index:
                self.pieces.append("|")
            segment = body[bounds[index] + 1:bounds[index + 1]]
            outer = (index == 0 or index == len(bounds) - 2) and not segment.strip() and cuts
            if outer:
                self.pieces.append(segment)
                continue
            column += 1
            content = segment.strip()
            if not content:
                self.pieces.append(segment)
                continue
            start = len(segment) - len(segment.lstrip())
            end = len(segment.rstrip())
            unit = TextUnit(
                id=f"L{line.number}C{column}",
                text=content,
                context=f"{_where(line.number)} · 表格第 {column} 列",
                kind="table_cell",
                protected=_protected(content, self._protect),
            )
            self.pieces.append(segment[:start])
            self.pieces.append(_Slot(unit, [""], [""], [], [content], "", inline=True))
            self.pieces.append(segment[end:])
            self.units.append(unit)
        self.pieces.append(line.ending)


def _chunks(records: list[_Record]) -> list[list[_Record]]:
    chunks: list[list[_Record]] = [[]]
    size = 0
    for record in records:
        if chunks[-1] and size + len(record.content) > MAX_UNIT_CHARS:
            chunks.append([])
            size = 0
        chunks[-1].append(record)
        size += len(record.content) + 1
    return chunks


def _pipe_positions(body: str) -> list[int]:
    """Cell borders of a table row: pipes outside inline code, not escaped."""
    code = [match.span() for match in _CODE_SPAN.finditer(body)]
    return [
        index
        for index, char in enumerate(body)
        if char == "|"
        and (index == 0 or body[index - 1] != "\\")
        and not any(start <= index < end for start, end in code)
    ]


# ── plain text ──────────────────────────────────────────────────────────────


def _parse_text(document: _Document) -> None:
    lines = document.lines
    index = 0
    while index < len(lines):
        if not lines[index].body.strip():
            document.raw(lines[index])
            index += 1
            continue
        end = index
        while end < len(lines) and lines[end].body.strip():
            end += 1
        records = [_record(line) for line in lines[index:end]]
        document.text(records, "paragraph", records[0].lead)
        index = end


# ── Markdown ────────────────────────────────────────────────────────────────

_FENCE = re.compile(r"^[ \t]*(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
_ATX = re.compile(r"^(?P<marker> {0,3}#{1,6})(?P<gap>[ \t]+|$)(?P<text>.*?)(?P<close>(?:[ \t]+#+)?[ \t]*)$")
_QUOTE = re.compile(r"^ {0,3}> ?")
_LIST_ITEM = re.compile(r"^(?P<marker>[ \t]*(?:[-*+]|\d{1,9}[.)])(?:[ \t]+|$))(?P<text>.*)$")
# A task list box stays with the marker: ``- [ ] text``.
_TASK = re.compile(r"^\[[ xX]\][ \t]+")
_THEMATIC = re.compile(r"^ {0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$")
_SETEXT = re.compile(r"^ {0,3}(?:=+|-+)[ \t]*$")
_TABLE_SEPARATOR = re.compile(r"^[ \t]*\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$")
_REFERENCE = re.compile(r"^ {0,3}\[(?!\^)[^\]]+\]:[ \t]*\S")
# ``[^1]: text``: the label stays with the marker, the text is translated.
_FOOTNOTE_DEFINITION = re.compile(r"^ {0,3}\[\^[^\]\s]+\]:[ \t]*")
_HTML_COMMENT = re.compile(r"^ {0,3}<!--")
_HTML_RAW = re.compile(r"^ {0,3}<(script|pre|style|textarea)(?:[\s>]|$)", re.I)
_HTML_OTHER = re.compile(r"^ {0,3}<(?:\?|![A-Za-z]|!\[CDATA\[)")
_HTML_BLOCK = re.compile(r"^ {0,3}</?(?P<tag>[A-Za-z][A-Za-z0-9-]*)(?:\s|/?>|$)")
_HTML_LONE_TAG = re.compile(
    r"^ {0,3}(?:<[A-Za-z][A-Za-z0-9-]*(?:\s+[^<>]*)?/?>|</[A-Za-z][A-Za-z0-9-]*\s*>)[ \t]*$"
)
# CommonMark's block-level tag names (HTML block type 6).
_HTML_BLOCK_TAGS = frozenset(
    "address article aside base basefont blockquote body caption center col colgroup dd details "
    "dialog dir div dl dt fieldset figcaption figure footer form frame frameset h1 h2 h3 h4 h5 h6 "
    "head header hr html iframe legend li link main menu menuitem nav noframes ol optgroup option "
    "p param search section summary table tbody td tfoot th thead title tr track ul".split()
)


def _indent(body: str) -> int:
    expanded = body.expandtabs(4)
    return len(expanded) - len(expanded.lstrip()) if body.strip() else 0


def _fence(body: str) -> re.Match[str] | None:
    match = _FENCE.match(body)
    if match and match.group("fence")[0] == "`" and "`" in match.group("info"):
        return None  # ```inline code``` at the start of a paragraph
    return match


def _fence_end(lines: list[_Line], start: int, fence: str) -> int:
    closing = re.compile(rf"^[ \t]*{re.escape(fence[0])}{{{len(fence)},}}[ \t]*$")
    for index in range(start + 1, len(lines)):
        if closing.match(lines[index].body):
            return index + 1
    return len(lines)


def _html_start(body: str, *, interrupting: bool = False) -> str:
    """Which kind of HTML block ``body`` opens, or ``""``."""
    if _HTML_COMMENT.match(body):
        return "comment"
    if _HTML_RAW.match(body):
        return "raw"
    if _HTML_OTHER.match(body):
        return "other"
    match = _HTML_BLOCK.match(body)
    if match and match.group("tag").lower() in _HTML_BLOCK_TAGS:
        return "block"
    if not interrupting and _HTML_LONE_TAG.match(body):
        return "block"
    return ""


def _html_end(lines: list[_Line], start: int, kind: str) -> int:
    if kind == "comment":
        first = lines[start].body.split("<!--", 1)[1]
        if "-->" in first:
            return start + 1
        marker = "-->"
    elif kind == "raw":
        tag = _HTML_RAW.match(lines[start].body).group(1).lower()
        marker = f"</{tag}>"
        if marker in lines[start].body.lower():
            return start + 1
    else:
        marker = ""
    for index in range(start + 1, len(lines)):
        body = lines[index].body
        if marker and marker in body.lower():
            return index + 1
        if not marker and not body.strip():
            return index
    return len(lines)


def _starts_block(body: str) -> bool:
    """Whether ``body`` ends the paragraph above it."""
    item = _LIST_ITEM.match(body)
    return bool(
        _fence(body)
        or _ATX.match(body)
        or _QUOTE.match(body)
        or (item and item.group("text").strip())
        or _THEMATIC.match(body)
        or _html_start(body, interrupting=True)
    )


def _parse_markdown(document: _Document, lines: list[_Line], *, top: bool) -> None:
    index = 0
    count = len(lines)
    # Inside a list item, content indented up to the item's text is the item's,
    # not an indented code block.
    list_offset = 0
    if top and count and lines[0].body.strip() == "---":
        for end in range(1, count):
            if lines[end].body.strip() in {"---", "..."}:
                document.locked(lines[: end + 1], "front_matter")
                index = end + 1
                break
    while index < count:
        line = lines[index]
        body = line.body
        if not body.strip():
            document.raw(line)
            index += 1
            continue
        indent = _indent(body)
        if list_offset and indent < list_offset and not _LIST_ITEM.match(body):
            list_offset = 0
        if indent >= list_offset + 4:
            end = index
            while end < count and (not lines[end].body.strip() or _indent(lines[end].body) >= list_offset + 4):
                end += 1
            while not lines[end - 1].body.strip():
                end -= 1
            document.locked(lines[index:end], "code")
            index = end
            continue
        fence = _fence(body)
        if fence:
            end = _fence_end(lines, index, fence.group("fence"))
            document.locked(lines[index:end], "code")
            index = end
            continue
        html = _html_start(body)
        if html:
            end = _html_end(lines, index, html)
            document.locked(lines[index:end], "html")
            index = end
            continue
        if _REFERENCE.match(body):
            document.locked([line], "reference")
            index += 1
            continue
        if _THEMATIC.match(body):
            document.raw(line)
            index += 1
            continue
        heading = _ATX.match(body)
        if heading:
            marker = heading.group("marker") + heading.group("gap")
            record = _Record(
                line.number,
                line.prefix + marker,
                heading.group("text"),
                heading.group("close"),
                line.ending,
            )
            document.text([record], "heading", "", inline=True)
            index += 1
            continue
        if _QUOTE.match(body):
            end = index
            inner: list[_Line] = []
            while end < count and _QUOTE.match(lines[end].body):
                quoted = lines[end]
                marker = _QUOTE.match(quoted.body).group(0)
                inner.append(_Line(quoted.number, quoted.prefix + marker, quoted.body[len(marker):], quoted.ending))
                end += 1
            _parse_markdown(document, inner, top=False)
            index = end
            continue
        if index + 1 < count and "|" in body and "|" in lines[index + 1].body and _TABLE_SEPARATOR.match(lines[index + 1].body):
            document.table_row(line)
            document.locked([lines[index + 1]], "table_separator")
            end = index + 2
            while end < count and lines[end].body.strip() and "|" in lines[end].body and not _starts_block(lines[end].body):
                document.table_row(lines[end])
                end += 1
            index = end
            continue
        footnote = _FOOTNOTE_DEFINITION.match(body)
        if footnote and body[footnote.end():].strip():
            records = [_record(line, footnote.group(0))]
            end = index + 1
            while end < count and lines[end].body.strip() and not _starts_block(lines[end].body):
                records.append(_record(lines[end]))
                end += 1
            # A footnote's later paragraphs are indented by four spaces.
            document.text(records, "footnote", line.prefix + "    ")
            index = end
            continue
        item = _LIST_ITEM.match(body)
        if item and item.group("text").strip():
            marker = item.group("marker")
            task = _TASK.match(item.group("text"))
            records = [_record(line, marker + (task.group(0) if task else ""))]
            end = index + 1
            while end < count and lines[end].body.strip() and not _starts_block(lines[end].body):
                records.append(_record(lines[end]))
                end += 1
            list_offset = len(marker.expandtabs(4))
            document.text(records, "list_item", line.prefix + " " * list_offset)
            index = end
            continue
        end = index + 1
        while end < count:
            following = lines[end].body
            if not following.strip() or _SETEXT.match(following) or _starts_block(following):
                break
            end += 1
        records = [_record(paragraph_line) for paragraph_line in lines[index:end]]
        if end < count and _SETEXT.match(lines[end].body):
            document.text(records, "heading", "", inline=True)
            document.raw(lines[end])
            end += 1
        else:
            document.text(records, "paragraph", records[0].lead)
        index = end


# ── the adapter ─────────────────────────────────────────────────────────────


def _parse(path: str | Path) -> _Document:
    source = read_text_file(path)
    if Path(path).suffix.lower() in MARKDOWN_SUFFIXES:
        document = _Document(source, _MARKDOWN_PATTERNS)
        _parse_markdown(document, document.lines, top=True)
    else:
        document = _Document(source, _TEXT_PATTERNS)
        _parse_text(document)
    return document


def extract(path: str) -> list[TextUnit]:
    return _parse(path).units


def scan(path: str) -> ScanItem:
    count = sum(1 for unit in extract(path) if not unit.locked and _has_letters(unit.text))
    return ScanItem(unit_count=count, warnings=[] if count else ["没有找到需要翻译的文字。"])


def _tidy(translation: str, unit: TextUnit) -> str:
    for fragment in unit.protected:
        # A model sometimes puts a space between a link's text and its address.
        if fragment[:1] in "([":
            translation = re.sub(r"\]\s+" + re.escape(fragment), lambda _match: "]" + fragment, translation)
    if unit.kind == "table_cell":
        translation = re.sub(r"(?<!\\)\|", r"\\|", translation)
    return translation


def write(path: str, units: list[TranslatedUnit], mode: str, out_path: str, options: dict) -> OutputFile:
    document = _parse(path)
    by_id = {unit.id: unit for unit in units}
    parts: list[str] = []
    for piece in document.pieces:
        if isinstance(piece, str):
            parts.append(piece)
            continue
        translated = by_id.get(piece.unit.id)
        if (
            translated is None
            or not translated.changed
            or translated.text != piece.unit.text
            or not translated.translation.strip()
        ):
            parts.append(piece.render_source())
            continue
        translation = _tidy(translated.translation, piece.unit)
        if mode == MODE_BILINGUAL:
            parts.append(piece.render_bilingual(translation, document.newline))
        else:
            parts.append(piece.render(translation, document.newline))
    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(document.source.bom + "".join(parts).encode(document.source.encoding))
    return OutputFile(path=str(target))
