"""Word document scanning, extraction, and bilingual DOCX writing."""

from __future__ import annotations

import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph
from loguru import logger

from core.bilingual_writer import build_output_dir
from core.language_registry import get_target_lang_display
from core.translation_filter import should_translate
from core.translation_protocol import extract_replace_translation, is_replace_translation

SUPPORTED_WORD_SUFFIXES = {".docx"}
GENERATED_OUTPUT_DIR_MARKER = "_翻译输出_"

_INVALID_FILENAME_FRAGMENT_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_HEADING_STYLE_RE = re.compile(r"heading\s*(\d+)|标题\s*(\d+)", re.IGNORECASE)
_CHINESE_CHAPTER_RE = re.compile(r"^(?:第[一二三四五六七八九十百千万]+[章节篇]|[一二三四五六七八九十]+[、.．])")
_CHINESE_SECTION_RE = re.compile(r"^（[一二三四五六七八九十]+）")
_NUMBERED_SECTION_RE = re.compile(r"^\d+(?:\.\d+)+\s+")


@dataclass
class WordFileItem:
    path: Path
    name: str
    size_kb: float
    paragraph_count: int = 0
    table_count: int = 0
    translatable_count: int = 0


@dataclass(frozen=True)
class WordSegment:
    source: str
    kind: str
    location: str
    section_path: str = ""


@dataclass(frozen=True)
class ResolvedWordTranslation:
    text: str
    replace_only: bool = False


@dataclass(frozen=True)
class NumberingLevelDefinition:
    start: int = 1
    number_format: str = "decimal"
    level_text: str = "%1."


def is_supported_word_file(path: str | Path) -> bool:
    """Return whether a path points to a supported Word file."""
    path = Path(path)
    return (
        path.is_file()
        and path.suffix.lower() in SUPPORTED_WORD_SUFFIXES
        and not path.name.startswith("~")
    )


def scan_word_path(path: str | Path) -> list[WordFileItem]:
    """Scan a folder or single `.docx` file."""
    path = Path(path)
    if not path.exists():
        logger.warning(f"路径不存在：{path}")
        return []

    if path.is_dir():
        return scan_word_folder(path)

    if path.is_file():
        if not is_supported_word_file(path):
            logger.warning(f"不支持的 Word 文件类型：{path}")
            return []
        try:
            return [_build_word_file_item(path)]
        except Exception as exc:
            logger.warning(f"扫描 Word 文件失败 {path.name}：{exc}")
            return []

    logger.warning(f"路径既不是文件也不是目录：{path}")
    return []


def scan_word_folder(root: str | Path) -> list[WordFileItem]:
    """Recursively scan a folder for `.docx` files."""
    root = Path(root)
    if not root.exists():
        logger.warning(f"文件夹不存在：{root}")
        return []

    items: list[WordFileItem] = []
    for path in root.rglob("*.docx"):
        if path.name.startswith("~"):
            continue
        if _is_generated_output(path.relative_to(root)):
            continue
        try:
            items.append(_build_word_file_item(path))
        except Exception as exc:
            logger.warning(f"扫描 Word 文件失败 {path.name}：{exc}")

    items.sort(key=lambda item: item.path)
    logger.info(f"Word 扫描完成：{root}，共发现 {len(items)} 个文件")
    return items


def extract_word_segments(
    path: str | Path,
    *,
    target_lang: str,
    source_lang: str = "zh",
) -> list[WordSegment]:
    """Extract unique body-paragraph and table-cell texts that need translation."""
    doc = Document(str(path))
    seen: set[str] = set()
    segments: list[WordSegment] = []
    section_stack: dict[int, str] = {}

    for index, paragraph in enumerate(doc.paragraphs):
        source = _paragraph_source_text(paragraph)
        heading_level = _detect_heading_level(paragraph)
        if heading_level is not None and source:
            _update_section_stack(section_stack, heading_level, source)

        if not _is_translatable_source(
            source,
            target_lang=target_lang,
            source_lang=source_lang,
        ):
            continue
        if _is_toc_or_field_paragraph(paragraph):
            continue
        if source in seen:
            continue
        seen.add(source)
        segments.append(
            WordSegment(
                source=source,
                kind="paragraph",
                location=f"body.paragraph[{index}]",
                section_path=_format_section_path(section_stack),
            )
        )

    for table_index, table in enumerate(doc.tables):
        for cell_index, cell in enumerate(_iter_unique_table_cells(table)):
            source = _cell_source_text(cell)
            if not _is_translatable_source(
                source,
                target_lang=target_lang,
                source_lang=source_lang,
            ):
                continue
            if source in seen:
                continue
            seen.add(source)
            segments.append(
                WordSegment(
                    source=source,
                    kind="table_cell",
                    location=f"table[{table_index}].cell[{cell_index}]",
                    section_path=f"表格 {table_index + 1}",
                )
            )

    return segments


def write_bilingual_docx(
    *,
    source_path: str | Path,
    output_dir: str | Path,
    translations: dict[str, str],
    target_lang: str,
    source_lang: str = "zh",
    log_callback=None,
) -> Path:
    """Write a bilingual Word document to the output directory."""
    source_path = Path(source_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    lang_display = _sanitize_filename_fragment(
        get_target_lang_display(target_lang, include_optional=True)
    )
    out_path = output_dir / f"双语({lang_display})_{source_path.name}"
    shutil.copy2(source_path, out_path)
    _ensure_owner_writable(out_path)

    doc = Document(str(out_path))
    body_paragraphs = list(doc.paragraphs)
    table_cells = [
        cell
        for table in doc.tables
        for cell in _iter_unique_table_cells(table)
    ]
    all_paragraphs = [
        *body_paragraphs,
        *(paragraph for cell in table_cells for paragraph in cell.paragraphs),
    ]
    original_paragraph_sources = {
        id(paragraph): _paragraph_source_text(paragraph)
        for paragraph in all_paragraphs
    }
    original_cell_sources = {
        id(cell): _cell_source_text(cell)
        for cell in table_cells
    }
    numbering_labels = _collect_numbering_labels(doc, all_paragraphs)
    _flatten_automatic_numbering(all_paragraphs, numbering_labels)

    paragraph_insertions = 0
    table_insertions = 0

    for paragraph in body_paragraphs:
        paragraph_key = id(paragraph)
        source = original_paragraph_sources.get(
            paragraph_key,
            _paragraph_source_text(paragraph),
        )
        if not _is_translatable_source(
            source,
            target_lang=target_lang,
            source_lang=source_lang,
        ):
            continue
        if _is_toc_or_field_paragraph(paragraph):
            continue

        resolved = _resolve_translation(source, translations)
        if resolved is None:
            continue
        if resolved.replace_only:
            _replace_paragraph_text(
                paragraph,
                _prepend_numbering_label(
                    resolved.text,
                    numbering_labels.get(paragraph_key, ""),
                ),
                target_lang=target_lang,
            )
        else:
            _insert_translation_paragraph_after(
                paragraph,
                _prepend_numbering_label(
                    resolved.text,
                    numbering_labels.get(paragraph_key, ""),
                ),
                target_lang=target_lang,
            )
        paragraph_insertions += 1

    for cell in table_cells:
        source = original_cell_sources.get(id(cell), _cell_source_text(cell))
        if not _is_translatable_source(
            source,
            target_lang=target_lang,
            source_lang=source_lang,
        ):
            continue
        resolved = _resolve_translation(source, translations)
        if resolved is None:
            continue
        numbering_label = _single_cell_numbering_label(cell, numbering_labels)
        if resolved.replace_only:
            cell.text = _prepend_numbering_label(resolved.text, numbering_label)
        else:
            _append_translation_to_cell(
                cell,
                _prepend_numbering_label(resolved.text, numbering_label),
                target_lang=target_lang,
            )
        table_insertions += 1

    doc.save(str(out_path))
    if log_callback:
        log_callback(
            f"[OK] 已输出：{out_path.name}（段落 {paragraph_insertions}，表格单元格 {table_insertions}）"
        )
    return out_path


def build_word_output_dir(
    source_dir: str | Path,
    custom_output_dir: str | Path | None = None,
) -> Path:
    """Expose the shared output-directory convention for Word callers."""
    return build_output_dir(source_dir, custom_output_dir)


def _build_word_file_item(path: Path) -> WordFileItem:
    doc = Document(str(path))
    segments = extract_word_segments(path, target_lang="en", source_lang="zh")
    return WordFileItem(
        path=path,
        name=path.stem,
        size_kb=round(path.stat().st_size / 1024, 1),
        paragraph_count=len(doc.paragraphs),
        table_count=len(doc.tables),
        translatable_count=len(segments),
    )


def _is_generated_output(path: Path) -> bool:
    return any(GENERATED_OUTPUT_DIR_MARKER in part for part in path.parts)


def _iter_unique_table_cells(table: Table, seen: set[int] | None = None):
    if seen is None:
        seen = set()
    for row in table.rows:
        for cell in row.cells:
            key = id(cell._tc)
            if key in seen:
                continue
            seen.add(key)
            yield cell
            for nested_table in cell.tables:
                yield from _iter_unique_table_cells(nested_table, seen)


def _paragraph_source_text(paragraph: Paragraph) -> str:
    return (paragraph.text or "").strip()


def _cell_source_text(cell: _Cell) -> str:
    return (cell.text or "").strip()


def _is_translatable_source(
    source: str,
    *,
    target_lang: str,
    source_lang: str,
) -> bool:
    return bool(
        source
        and should_translate(
            source,
            target_lang=target_lang,
            source_lang=source_lang,
        )
    )


def _resolve_translation(
    source: str,
    translations: dict[str, str],
) -> ResolvedWordTranslation | None:
    raw = translations.get(source.strip())
    if raw is None:
        return None
    if is_replace_translation(raw):
        replacement = extract_replace_translation(raw).strip()
        return ResolvedWordTranslation(replacement, replace_only=True) if replacement else None

    translated = str(raw).strip()
    if not translated:
        return None
    if source.strip().casefold() == translated.casefold():
        return None
    return ResolvedWordTranslation(translated)


def _is_toc_or_field_paragraph(paragraph: Paragraph) -> bool:
    style_name = _paragraph_style_name(paragraph).casefold()
    if "toc" in style_name or "目录" in style_name:
        return True

    xml = paragraph._p.xml
    if "w:fldSimple" in xml or "w:fldChar" in xml:
        return True
    if "TOC" in xml and "w:instrText" in xml:
        return True
    return False


def _paragraph_style_name(paragraph: Paragraph) -> str:
    try:
        return paragraph.style.name or ""
    except Exception:
        return ""


def _is_heading_style(paragraph: Paragraph) -> bool:
    style_name = _paragraph_style_name(paragraph).casefold()
    return style_name.startswith("heading") or "标题" in style_name


def _detect_heading_level(paragraph: Paragraph) -> int | None:
    text = _paragraph_source_text(paragraph)
    if not text:
        return None

    style_name = _paragraph_style_name(paragraph)
    match = _HEADING_STYLE_RE.search(style_name)
    if match:
        raw_level = match.group(1) or match.group(2)
        try:
            return max(1, min(int(raw_level), 6))
        except (TypeError, ValueError):
            return 1

    if _CHINESE_CHAPTER_RE.match(text):
        return 1
    if _CHINESE_SECTION_RE.match(text):
        return 2
    if _NUMBERED_SECTION_RE.match(text):
        return min(text.split(maxsplit=1)[0].count(".") + 1, 6)

    return None


def _update_section_stack(section_stack: dict[int, str], level: int, text: str) -> None:
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        return
    for existing_level in list(section_stack):
        if existing_level >= level:
            section_stack.pop(existing_level, None)
    section_stack[level] = cleaned


def _format_section_path(section_stack: dict[int, str]) -> str:
    if not section_stack:
        return "正文"
    return " / ".join(section_stack[level] for level in sorted(section_stack))


def _collect_numbering_labels(
    doc: Document,
    paragraphs: list[Paragraph],
) -> dict[int, str]:
    level_definitions = _load_numbering_level_definitions(doc)
    counters: dict[tuple[str, int], int] = {}
    labels: dict[int, str] = {}

    for paragraph in paragraphs:
        numbering_info = _get_paragraph_numbering_info(paragraph)
        if numbering_info is None:
            continue

        num_id, ilvl = numbering_info
        level_definition = level_definitions.get((num_id, ilvl))
        if level_definition is None:
            continue

        for key in list(counters):
            if key[0] == num_id and key[1] > ilvl:
                counters.pop(key, None)

        counter_key = (num_id, ilvl)
        if counter_key not in counters:
            counters[counter_key] = level_definition.start - 1
        counters[counter_key] += 1

        label = _format_numbering_label(
            num_id=num_id,
            ilvl=ilvl,
            counters=counters,
            level_definitions=level_definitions,
        )
        if label:
            labels[id(paragraph)] = label

    return labels


def _load_numbering_level_definitions(doc: Document) -> dict[tuple[str, int], NumberingLevelDefinition]:
    try:
        numbering_root = doc.part.numbering_part.element
    except Exception:
        return {}

    abstract_by_id = {
        abstract_num.get(qn("w:abstractNumId")): abstract_num
        for abstract_num in numbering_root.findall(qn("w:abstractNum"))
    }
    definitions: dict[tuple[str, int], NumberingLevelDefinition] = {}

    for num in numbering_root.findall(qn("w:num")):
        num_id = num.get(qn("w:numId"))
        abstract_id = _child_val(num, "w:abstractNumId")
        abstract_num = abstract_by_id.get(abstract_id)
        if not num_id or abstract_num is None:
            continue

        levels = {
            int(level.get(qn("w:ilvl")) or 0): _read_numbering_level_definition(level)
            for level in abstract_num.findall(qn("w:lvl"))
        }

        for override in num.findall(qn("w:lvlOverride")):
            ilvl = _to_int(override.get(qn("w:ilvl")), fallback=0)
            if override.find(qn("w:lvl")) is not None:
                levels[ilvl] = _read_numbering_level_definition(override.find(qn("w:lvl")))
            elif override.find(qn("w:startOverride")) is not None:
                existing = levels.get(ilvl, NumberingLevelDefinition())
                levels[ilvl] = NumberingLevelDefinition(
                    start=_to_int(
                        _child_val(override, "w:startOverride"),
                        fallback=existing.start,
                    ),
                    number_format=existing.number_format,
                    level_text=existing.level_text,
                )

        for ilvl, definition in levels.items():
            definitions[(num_id, ilvl)] = definition

    return definitions


def _read_numbering_level_definition(level) -> NumberingLevelDefinition:
    return NumberingLevelDefinition(
        start=_to_int(_child_val(level, "w:start"), fallback=1),
        number_format=_child_val(level, "w:numFmt", default="decimal"),
        level_text=_child_val(level, "w:lvlText", default="%1."),
    )


def _child_val(element, child_tag: str, default: str = "") -> str:
    if element is None:
        return default
    child = element.find(qn(child_tag))
    if child is None:
        return default
    return str(child.get(qn("w:val")) or default)


def _to_int(value, *, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _get_paragraph_numbering_info(paragraph: Paragraph) -> tuple[str, int] | None:
    direct = _read_numbering_info_from_ppr(getattr(paragraph._p, "pPr", None))
    if direct is not None:
        return direct

    try:
        style_element = paragraph.style._element
    except Exception:
        return None
    return _read_numbering_info_from_ppr(getattr(style_element, "pPr", None))


def _read_numbering_info_from_ppr(p_pr) -> tuple[str, int] | None:
    if p_pr is None:
        return None
    num_pr = p_pr.find(qn("w:numPr"))
    if num_pr is None:
        return None

    num_id = _child_val(num_pr, "w:numId")
    if not num_id or num_id == "0":
        return None
    return num_id, _to_int(_child_val(num_pr, "w:ilvl"), fallback=0)


def _format_numbering_label(
    *,
    num_id: str,
    ilvl: int,
    counters: dict[tuple[str, int], int],
    level_definitions: dict[tuple[str, int], NumberingLevelDefinition],
) -> str:
    definition = level_definitions.get((num_id, ilvl))
    if definition is None:
        return ""

    label = definition.level_text or "%1."
    if definition.number_format == "bullet":
        return _normalize_bullet_label(label)

    def replace_placeholder(match: re.Match[str]) -> str:
        level = max(0, _to_int(match.group(1), fallback=1) - 1)
        level_counter = counters.get((num_id, level), 0)
        level_definition = level_definitions.get((num_id, level), definition)
        return _format_number(level_counter, level_definition.number_format)

    return re.sub(r"%(\d+)", replace_placeholder, label).strip()


def _format_number(number: int, number_format: str) -> str:
    if number <= 0:
        number = 1
    if number_format == "lowerLetter":
        return _format_alpha_number(number).lower()
    if number_format == "upperLetter":
        return _format_alpha_number(number).upper()
    if number_format == "lowerRoman":
        return _format_roman_number(number).lower()
    if number_format == "upperRoman":
        return _format_roman_number(number).upper()
    return str(number)


def _format_alpha_number(number: int) -> str:
    chars: list[str] = []
    while number > 0:
        number -= 1
        chars.append(chr(ord("A") + (number % 26)))
        number //= 26
    return "".join(reversed(chars)) or "A"


def _format_roman_number(number: int) -> str:
    values = (
        (1000, "M"),
        (900, "CM"),
        (500, "D"),
        (400, "CD"),
        (100, "C"),
        (90, "XC"),
        (50, "L"),
        (40, "XL"),
        (10, "X"),
        (9, "IX"),
        (5, "V"),
        (4, "IV"),
        (1, "I"),
    )
    result: list[str] = []
    for value, token in values:
        while number >= value:
            result.append(token)
            number -= value
    return "".join(result) or "I"


def _normalize_bullet_label(label: str) -> str:
    cleaned = (label or "").strip()
    if cleaned in {"\uf0b7", ""}:
        return "•"
    return cleaned or "•"


def _flatten_automatic_numbering(
    paragraphs: list[Paragraph],
    numbering_labels: dict[int, str],
) -> None:
    if not numbering_labels:
        return
    for paragraph in paragraphs:
        label = numbering_labels.get(id(paragraph), "")
        if not label:
            continue
        _prepend_paragraph_text(paragraph, label)
        _remove_paragraph_numbering(paragraph)


def _prepend_paragraph_text(paragraph: Paragraph, label: str) -> None:
    source = _paragraph_source_text(paragraph)
    if not source or _text_starts_with_label(source, label):
        return

    prefix = f"{label} "
    if paragraph.runs:
        paragraph.runs[0].text = prefix + paragraph.runs[0].text.lstrip()
        return
    paragraph.add_run(prefix + source)


def _remove_paragraph_numbering(paragraph: Paragraph) -> None:
    p_pr = paragraph._p.get_or_add_pPr()
    num_pr = p_pr.find(qn("w:numPr"))
    if num_pr is not None:
        p_pr.remove(num_pr)
    suppress_num_pr = OxmlElement("w:numPr")
    suppress_num_id = OxmlElement("w:numId")
    suppress_num_id.set(qn("w:val"), "0")
    suppress_num_pr.append(suppress_num_id)
    p_pr.append(suppress_num_pr)


def _prepend_numbering_label(text: str, label: str) -> str:
    cleaned = str(text or "").strip()
    if not label or not cleaned or _text_starts_with_label(cleaned, label):
        return cleaned
    return f"{label} {cleaned}"


def _text_starts_with_label(text: str, label: str) -> bool:
    if not label:
        return False
    return bool(re.match(rf"^\s*{re.escape(label)}(?:\s|$)", str(text or "")))


def _single_cell_numbering_label(cell: _Cell, numbering_labels: dict[int, str]) -> str:
    labelled = [
        numbering_labels.get(id(paragraph), "")
        for paragraph in cell.paragraphs
        if _paragraph_source_text(paragraph)
    ]
    labelled = [label for label in labelled if label]
    return labelled[0] if len(labelled) == 1 else ""


def _insert_translation_paragraph_after(
    paragraph: Paragraph,
    text: str,
    *,
    target_lang: str,
) -> Paragraph:
    new_p = OxmlElement("w:p")
    paragraph._p.addnext(new_p)
    new_para = Paragraph(new_p, paragraph._parent)
    _copy_translation_paragraph_shape(paragraph, new_para)
    _remove_paragraph_numbering(new_para)
    run = new_para.add_run(text)
    _copy_run_shape(paragraph, run, target_lang=target_lang)
    return new_para


def _append_translation_to_cell(
    cell: _Cell,
    text: str,
    *,
    target_lang: str,
) -> Paragraph:
    source_para = _last_non_empty_paragraph(cell.paragraphs)
    new_para = cell.add_paragraph()
    if source_para is not None:
        _copy_translation_paragraph_shape(source_para, new_para)
        _remove_paragraph_numbering(new_para)
    run = new_para.add_run(text)
    if source_para is not None:
        _copy_run_shape(source_para, run, target_lang=target_lang)
    else:
        _set_latin_run_font(run, target_lang=target_lang)
    return new_para


def _replace_paragraph_text(
    paragraph: Paragraph,
    text: str,
    *,
    target_lang: str,
) -> None:
    if paragraph.runs:
        first_run = paragraph.runs[0]
        first_run.text = text
        _set_latin_run_font(first_run, target_lang=target_lang)
        for run in paragraph.runs[1:]:
            run.text = ""
    else:
        run = paragraph.add_run(text)
        _set_latin_run_font(run, target_lang=target_lang)


def _last_non_empty_paragraph(paragraphs: list[Paragraph]) -> Paragraph | None:
    for paragraph in reversed(paragraphs):
        if paragraph.text.strip():
            return paragraph
    return paragraphs[-1] if paragraphs else None


def _copy_translation_paragraph_shape(source: Paragraph, target: Paragraph) -> None:
    if not _is_heading_style(source):
        try:
            target.style = source.style
        except Exception:
            pass

    source_format = source.paragraph_format
    target_format = target.paragraph_format
    for attr in (
        "alignment",
        "first_line_indent",
        "keep_together",
        "keep_with_next",
        "left_indent",
        "line_spacing",
        "line_spacing_rule",
        "page_break_before",
        "right_indent",
        "space_after",
        "space_before",
        "widow_control",
    ):
        try:
            setattr(target_format, attr, getattr(source_format, attr))
        except Exception:
            continue


def _copy_run_shape(source_paragraph: Paragraph, target_run, *, target_lang: str) -> None:
    source_run = next((run for run in source_paragraph.runs if run.text.strip()), None)
    if source_run is not None:
        try:
            target_run.bold = source_run.bold
            target_run.italic = source_run.italic
            target_run.underline = source_run.underline
            target_run.font.size = source_run.font.size
            target_run.font.color.rgb = source_run.font.color.rgb
        except Exception:
            pass
    _set_latin_run_font(target_run, target_lang=target_lang)


def _set_latin_run_font(run, *, target_lang: str) -> None:
    if target_lang == "zh":
        return
    try:
        run.font.name = "Times New Roman"
        r_pr = run._element.get_or_add_rPr()
        r_fonts = r_pr.rFonts
        if r_fonts is None:
            r_fonts = OxmlElement("w:rFonts")
            r_pr.append(r_fonts)
        for attr in ("w:ascii", "w:hAnsi", "w:cs"):
            r_fonts.set(qn(attr), "Times New Roman")
    except Exception:
        pass


def _ensure_owner_writable(path: Path) -> None:
    current_mode = path.stat().st_mode
    if current_mode & stat.S_IWUSR:
        return
    path.chmod(current_mode | stat.S_IWUSR)


def _sanitize_filename_fragment(value: str) -> str:
    cleaned = _INVALID_FILENAME_FRAGMENT_RE.sub("_", str(value or "")).strip().rstrip(". ")
    return cleaned or "目标语言"
