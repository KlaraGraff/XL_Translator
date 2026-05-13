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


@dataclass(frozen=True)
class ResolvedWordTranslation:
    text: str
    replace_only: bool = False


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

    for index, paragraph in enumerate(doc.paragraphs):
        source = _paragraph_source_text(paragraph)
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
    paragraph_insertions = 0
    table_insertions = 0

    for paragraph in list(doc.paragraphs):
        source = _paragraph_source_text(paragraph)
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
            _replace_paragraph_text(paragraph, resolved.text, target_lang=target_lang)
        else:
            _insert_translation_paragraph_after(
                paragraph,
                resolved.text,
                target_lang=target_lang,
            )
        paragraph_insertions += 1

    for table in doc.tables:
        for cell in _iter_unique_table_cells(table):
            source = _cell_source_text(cell)
            if not _is_translatable_source(
                source,
                target_lang=target_lang,
                source_lang=source_lang,
            ):
                continue
            resolved = _resolve_translation(source, translations)
            if resolved is None:
                continue
            if resolved.replace_only:
                cell.text = resolved.text
            else:
                _append_translation_to_cell(cell, resolved.text, target_lang=target_lang)
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
