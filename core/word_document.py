"""Word document scanning, extraction, and bilingual DOCX writing."""

from __future__ import annotations

import re
import shutil
import stat
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from docx import Document
from docx.shared import RGBColor
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph
from loguru import logger

from config import REVIEW_MARK_COLOR_DEFAULTS
from core.bilingual_writer import build_output_dir
from core.language_registry import get_target_lang_display
from core.mixed_language import (
    MIXED_MARK_FOREIGN_NOISE,
    MIXED_MARK_SEMANTIC,
    MIXED_MARK_UNRESOLVED,
)
from core.translation_filter import should_translate
from core.translation_protocol import extract_replace_translation, is_replace_translation
from core.word_converter import is_legacy_word_doc

SUPPORTED_WORD_SUFFIXES = {".docx", ".doc"}
GENERATED_OUTPUT_DIR_MARKER = "_翻译输出_"
EXISTING_HIGHLIGHT_POLICY_SKIP = "skip"
EXISTING_HIGHLIGHT_POLICY_OVERWRITE = "overwrite"
EXISTING_HIGHLIGHT_POLICY_RED_UNDERLINE = "red_underline"

_INVALID_FILENAME_FRAGMENT_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_HEADING_STYLE_RE = re.compile(r"heading\s*(\d+)|标题\s*(\d+)", re.IGNORECASE)
_CHINESE_CHAPTER_RE = re.compile(r"^(?:第[一二三四五六七八九十百千万]+[章节篇]|[一二三四五六七八九十]+[、.．])")
_CHINESE_SECTION_RE = re.compile(r"^（[一二三四五六七八九十]+）")
_NUMBERED_SECTION_RE = re.compile(r"^\d+(?:\.\d+)+\s+")
_CHINESE_NUMBER_CHARS = "零〇一二两三四五六七八九十百千万"
_CHINESE_NUMBERING_PREFIX_RE = re.compile(
    rf"^第[{_CHINESE_NUMBER_CHARS}0-9０-９]+[章节篇卷部]"
)
_CHINESE_LIST_PREFIX_RE = re.compile(
    rf"^(?:[{_CHINESE_NUMBER_CHARS}]+[、.．]|[（(][{_CHINESE_NUMBER_CHARS}]+[）)])"
)
_ARABIC_LIST_PREFIX_RE = re.compile(
    r"^(?:\d{1,3}[、．.)）]|[（(]\d{1,3}[）)])"
)
_ALPHA_LIST_PREFIX_RE = re.compile(r"^[A-Za-z][.)）]")
_BULLET_PREFIX_RE = re.compile(r"^[•·▪▫●○◆◇■□]\s+")
_ARABIC_DECIMAL_PREFIX_RE = re.compile(r"^(\d{1,3}(?:\.\d{1,3})+)(.*)$")


@dataclass
class WordFileItem:
    path: Path
    name: str
    size_kb: float
    paragraph_count: int = 0
    table_count: int = 0
    translatable_count: int = 0
    original_path: Path | None = None


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


@dataclass(frozen=True)
class WordNumberingNormalizationStats:
    labels_seen: int = 0
    labels_prepended: int = 0
    numbering_removed: int = 0


@dataclass(frozen=True)
class WordNumberingNormalizationResult:
    path: Path
    stats: WordNumberingNormalizationStats


def is_supported_word_file(path: str | Path) -> bool:
    """Return whether a path points to a supported Word file."""
    path = Path(path)
    return (
        path.is_file()
        and path.suffix.lower() in SUPPORTED_WORD_SUFFIXES
        and not path.name.startswith("~")
    )


def scan_word_path(path: str | Path) -> list[WordFileItem]:
    """Scan a folder or single supported Word file."""
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
    """Recursively scan a folder for supported Word files."""
    root = Path(root)
    if not root.exists():
        logger.warning(f"文件夹不存在：{root}")
        return []

    items: list[WordFileItem] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.name.startswith("~"):
            continue
        if path.suffix.lower() not in SUPPORTED_WORD_SUFFIXES:
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
    output_name: str | None = None,
    review_highlight_sources: set[str] | None = None,
    review_highlight_color: str = "FFF2CC",
    review_marks: dict[str, str] | None = None,
    review_mark_colors: dict[str, str] | None = None,
    existing_highlight_policy: str = EXISTING_HIGHLIGHT_POLICY_SKIP,
    log_callback=None,
) -> Path:
    """Write a bilingual Word document to the output directory."""
    source_path = Path(source_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    review_mark_map = _normalize_review_marks(
        review_marks=review_marks,
        review_highlight_sources=review_highlight_sources,
        review_highlight_color=review_highlight_color,
    )
    review_color_map = _normalize_review_mark_colors(review_mark_colors)
    highlight_policy = _normalize_existing_highlight_policy(existing_highlight_policy)

    lang_display = _sanitize_filename_fragment(
        get_target_lang_display(target_lang, include_optional=True)
    )
    source_output_name = _normalize_word_output_name(output_name or source_path.name)
    out_path = output_dir / f"双语({lang_display})_{source_output_name}"
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
    numbering_labels = _collect_numbering_labels(doc, all_paragraphs)
    _flatten_automatic_numbering(all_paragraphs, numbering_labels)
    original_paragraph_sources = {
        id(paragraph): _paragraph_source_text(paragraph)
        for paragraph in all_paragraphs
    }
    original_cell_sources = {
        id(cell): _cell_source_text(cell)
        for cell in table_cells
    }

    paragraph_insertions = 0
    table_insertions = 0
    highlight_count = 0
    highlight_skip_count = 0

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

        review_mark = review_mark_map.get(source.strip())
        if review_mark:
            if _apply_paragraph_review_mark(
                paragraph,
                review_mark,
                highlight_policy,
                review_color_map,
            ):
                highlight_count += 1
            else:
                highlight_skip_count += 1

        resolved = _resolve_translation(source, translations)
        if resolved is None:
            continue
        if resolved.replace_only:
            _replace_paragraph_text(
                paragraph,
                resolved.text,
                target_lang=target_lang,
            )
            if review_mark:
                _apply_paragraph_review_mark(
                    paragraph,
                    review_mark,
                    highlight_policy,
                    review_color_map,
                )
        else:
            translation_paragraph = _insert_translation_paragraph_after(
                paragraph,
                resolved.text,
                target_lang=target_lang,
            )
            if review_mark:
                _apply_paragraph_review_mark(
                    translation_paragraph,
                    review_mark,
                    highlight_policy,
                    review_color_map,
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
        review_mark = review_mark_map.get(source.strip())
        resolved = _resolve_translation(source, translations)
        if resolved is None:
            if review_mark:
                if _apply_cell_review_mark(
                    cell,
                    review_mark,
                    highlight_policy,
                    review_color_map,
                ):
                    highlight_count += 1
                else:
                    highlight_skip_count += 1
            continue
        if resolved.replace_only:
            cell.text = resolved.text
        else:
            _append_translation_to_cell(
                cell,
                resolved.text,
                target_lang=target_lang,
            )
        if review_mark:
            if _apply_cell_review_mark(
                cell,
                review_mark,
                highlight_policy,
                review_color_map,
            ):
                highlight_count += 1
            else:
                highlight_skip_count += 1
        table_insertions += 1

    doc.save(str(out_path))
    if log_callback:
        highlight_summary = ""
        if review_mark_map:
            highlight_summary = f"，复核标记 {highlight_count}"
            if highlight_skip_count:
                highlight_summary += f"，跳过已有标记 {highlight_skip_count}"
        log_callback(
            f"[OK] 已输出：{out_path.name}（段落 {paragraph_insertions}，表格单元格 {table_insertions}{highlight_summary}）"
        )
    return out_path


def build_word_output_dir(
    source_dir: str | Path,
    custom_output_dir: str | Path | None = None,
) -> Path:
    """Expose the shared output-directory convention for Word callers."""
    return build_output_dir(source_dir, custom_output_dir)


def normalize_docx_automatic_numbering(
    source_path: str | Path,
    output_path: str | Path | None = None,
) -> WordNumberingNormalizationResult:
    """Materialize automatic numbering once, then suppress residual list metadata."""
    source_path = Path(source_path)
    target_path = Path(output_path) if output_path is not None else _temp_docx_path(source_path)
    if source_path.resolve() != target_path.resolve():
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target_path)

    doc = Document(str(target_path))
    table_cells = [
        cell
        for table in doc.tables
        for cell in _iter_unique_table_cells(table)
    ]
    all_paragraphs = [
        *list(doc.paragraphs),
        *(paragraph for cell in table_cells for paragraph in cell.paragraphs),
    ]
    numbering_labels = _collect_numbering_labels(doc, all_paragraphs)
    stats = _flatten_automatic_numbering(all_paragraphs, numbering_labels)
    doc.save(str(target_path))
    return WordNumberingNormalizationResult(path=target_path, stats=stats)


def _build_word_file_item(path: Path) -> WordFileItem:
    if is_legacy_word_doc(path):
        return WordFileItem(
            path=path,
            name=path.stem,
            size_kb=round(path.stat().st_size / 1024, 1),
            original_path=path,
        )

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


def _normalize_word_output_name(name: str) -> str:
    cleaned = _sanitize_filename_fragment(Path(str(name or "document")).name)
    if Path(cleaned).suffix.lower() == ".doc":
        return f"{Path(cleaned).stem}.docx"
    if Path(cleaned).suffix.lower() != ".docx":
        return f"{cleaned}.docx"
    return cleaned


def _temp_docx_path(original_path: Path) -> Path:
    temp_dir = Path(tempfile.gettempdir()) / "word_translator_temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    return temp_dir / f"{original_path.stem}_normalized_{uuid.uuid4().hex[:8]}.docx"


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


def _normalize_hex_fill(value: str, fallback: str = "FFF2CC") -> str:
    cleaned = str(value or "").strip().lstrip("#").upper()
    if len(cleaned) == 6 and all(char in "0123456789ABCDEF" for char in cleaned):
        return cleaned
    return fallback


def _normalize_review_mark_colors(colors: dict[str, str] | None) -> dict[str, str]:
    raw_colors = dict(colors or {})
    normalized: dict[str, str] = {}
    for mark, default_color in REVIEW_MARK_COLOR_DEFAULTS.items():
        normalized[mark] = _normalize_hex_fill(
            raw_colors.get(mark, ""),
            fallback=default_color,
        )
    return normalized


def _normalize_existing_highlight_policy(policy: str) -> str:
    value = str(policy or "").strip()
    if value in {
        EXISTING_HIGHLIGHT_POLICY_SKIP,
        EXISTING_HIGHLIGHT_POLICY_OVERWRITE,
        EXISTING_HIGHLIGHT_POLICY_RED_UNDERLINE,
    }:
        return value
    return EXISTING_HIGHLIGHT_POLICY_SKIP


def _normalize_review_marks(
    *,
    review_marks: dict[str, str] | None,
    review_highlight_sources: set[str] | None,
    review_highlight_color: str,
) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for source, mark in (review_marks or {}).items():
        cleaned_source = str(source or "").strip()
        if not cleaned_source:
            continue
        normalized[cleaned_source] = _normalize_review_mark(mark)

    if review_marks is None:
        legacy_fill = _normalize_hex_fill(review_highlight_color)
        for source in review_highlight_sources or set():
            cleaned_source = str(source or "").strip()
            if cleaned_source:
                normalized[cleaned_source] = legacy_fill
    return normalized


def _normalize_review_mark(mark: str) -> str:
    value = str(mark or "").strip()
    if value in {
        MIXED_MARK_UNRESOLVED,
        MIXED_MARK_FOREIGN_NOISE,
        MIXED_MARK_SEMANTIC,
    }:
        return value
    return _normalize_hex_fill(value)


def _review_mark_fill(mark: str, mark_colors: dict[str, str]) -> str:
    if mark in mark_colors:
        return mark_colors[mark]
    return _normalize_hex_fill(mark)


def _apply_paragraph_review_mark(
    paragraph: Paragraph,
    mark: str,
    existing_policy: str,
    mark_colors: dict[str, str],
) -> bool:
    if _paragraph_has_existing_highlight(paragraph):
        if existing_policy == EXISTING_HIGHLIGHT_POLICY_SKIP:
            return False
        if existing_policy == EXISTING_HIGHLIGHT_POLICY_RED_UNDERLINE:
            return _apply_paragraph_red_underline(paragraph)
    return _apply_paragraph_text_shading(paragraph, _review_mark_fill(mark, mark_colors))


def _apply_cell_review_mark(
    cell: _Cell,
    mark: str,
    existing_policy: str,
    mark_colors: dict[str, str],
) -> bool:
    if _cell_has_existing_highlight(cell):
        if existing_policy == EXISTING_HIGHLIGHT_POLICY_SKIP:
            return False
        if existing_policy == EXISTING_HIGHLIGHT_POLICY_RED_UNDERLINE:
            return _apply_cell_red_underline(cell)
    applied = False
    fill = _review_mark_fill(mark, mark_colors)
    for paragraph in cell.paragraphs:
        applied = _apply_paragraph_text_shading(paragraph, fill) or applied
    return applied


def _apply_paragraph_text_shading(paragraph: Paragraph, fill: str) -> bool:
    applied = False
    for run in paragraph.runs:
        if not run.text:
            continue
        _set_shading_fill(run._element.get_or_add_rPr(), fill)
        applied = True
    return applied


def _apply_paragraph_red_underline(paragraph: Paragraph) -> bool:
    applied = False
    for run in paragraph.runs:
        if not run.text:
            continue
        try:
            run.font.color.rgb = RGBColor(192, 0, 0)
            run.underline = True
            applied = True
        except Exception:
            continue
    return applied


def _apply_cell_red_underline(cell: _Cell) -> bool:
    applied = False
    for paragraph in cell.paragraphs:
        applied = _apply_paragraph_red_underline(paragraph) or applied
    return applied


def _paragraph_has_existing_highlight(paragraph: Paragraph) -> bool:
    p_pr = getattr(paragraph._p, "pPr", None)
    if _element_has_existing_shading(p_pr):
        return True
    try:
        if _element_has_existing_shading(getattr(paragraph.style._element, "pPr", None)):
            return True
    except Exception:
        pass

    for run in paragraph.runs:
        try:
            if run.font.highlight_color is not None:
                return True
        except Exception:
            pass

        r_pr = getattr(run._element, "rPr", None)
        if _element_has_existing_shading(r_pr):
            return True
        highlight = r_pr.find(qn("w:highlight")) if r_pr is not None else None
        if highlight is not None:
            value = str(highlight.get(qn("w:val")) or "").strip().upper()
            if value and value != "NONE":
                return True
    return False


def _cell_has_existing_highlight(cell: _Cell) -> bool:
    tc_pr = getattr(cell._tc, "tcPr", None)
    if _element_has_existing_shading(tc_pr):
        return True
    return any(_paragraph_has_existing_highlight(paragraph) for paragraph in cell.paragraphs)


def _element_has_existing_shading(element) -> bool:
    if element is None:
        return False
    shd = element.find(qn("w:shd"))
    if shd is None:
        return False
    fill = str(shd.get(qn("w:fill")) or "").strip().upper()
    if fill and fill != "AUTO":
        return True
    value = str(shd.get(qn("w:val")) or "").strip().upper()
    return bool(value and value not in {"CLEAR", "NIL"})


def _set_shading_fill(parent_element, fill: str) -> None:
    shd = parent_element.find(qn("w:shd"))
    if shd is None:
        shd = OxmlElement("w:shd")
        parent_element.append(shd)
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), _normalize_hex_fill(fill))


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
) -> WordNumberingNormalizationStats:
    if not numbering_labels:
        return WordNumberingNormalizationStats()
    labels_seen = 0
    labels_prepended = 0
    numbering_removed = 0
    for paragraph in paragraphs:
        label = numbering_labels.get(id(paragraph), "")
        if not label:
            continue
        labels_seen += 1
        if _prepend_paragraph_text(paragraph, label):
            labels_prepended += 1
        if _remove_paragraph_numbering(paragraph):
            numbering_removed += 1
    return WordNumberingNormalizationStats(
        labels_seen=labels_seen,
        labels_prepended=labels_prepended,
        numbering_removed=numbering_removed,
    )


def _prepend_paragraph_text(paragraph: Paragraph, label: str) -> bool:
    source = _paragraph_source_text(paragraph)
    if not source or _has_visible_numbering_prefix(source) or _text_starts_with_label(source, label):
        return False

    prefix = f"{label} "
    if paragraph.runs:
        paragraph.runs[0].text = prefix + paragraph.runs[0].text.lstrip()
        return True
    paragraph.add_run(prefix + source)
    return True


def _remove_paragraph_numbering(paragraph: Paragraph) -> bool:
    p_pr = paragraph._p.get_or_add_pPr()
    num_pr = p_pr.find(qn("w:numPr"))
    if num_pr is not None:
        p_pr.remove(num_pr)
    suppress_num_pr = OxmlElement("w:numPr")
    suppress_num_id = OxmlElement("w:numId")
    suppress_num_id.set(qn("w:val"), "0")
    suppress_num_pr.append(suppress_num_id)
    p_pr.append(suppress_num_pr)
    return True


def _text_starts_with_label(text: str, label: str) -> bool:
    cleaned_text = str(text or "").strip()
    cleaned_label = str(label or "").strip()
    if not cleaned_text or not cleaned_label:
        return False
    return _text_starts_with_exact_label(cleaned_text, cleaned_label)


def _has_visible_numbering_prefix(text: str) -> bool:
    cleaned = str(text or "").strip()
    if not cleaned:
        return False
    if _BULLET_PREFIX_RE.match(cleaned):
        return True
    if _CHINESE_NUMBERING_PREFIX_RE.match(cleaned):
        return True
    if _CHINESE_LIST_PREFIX_RE.match(cleaned):
        return True
    if _ARABIC_LIST_PREFIX_RE.match(cleaned):
        return True
    if _ALPHA_LIST_PREFIX_RE.match(cleaned):
        return True

    decimal_match = _ARABIC_DECIMAL_PREFIX_RE.match(cleaned)
    if decimal_match is None:
        return False
    remainder = decimal_match.group(2)
    if not remainder:
        return True
    next_char = remainder[0]
    if next_char.isascii() and (next_char.isalpha() or next_char in {"%", "#"}):
        return False
    return True


def _text_starts_with_exact_label(text: str, label: str) -> bool:
    if not text.startswith(label):
        return False
    remainder = text[len(label) :]
    if not remainder:
        return True
    if label.startswith("第") and re.search(r"[章节篇]$", label):
        return True
    return _is_numbering_boundary(remainder[0])


def _is_numbering_boundary(char: str) -> bool:
    return char.isspace() or char in {
        ".",
        "．",
        "、",
        "，",
        ",",
        ":",
        "：",
        ")",
        "）",
        "-",
        "－",
        "—",
        "·",
    }


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
