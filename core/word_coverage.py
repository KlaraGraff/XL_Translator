"""Word coverage detection and position-based untranslated-only writing."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from docx import Document

from core.language_registry import get_target_lang_display
from core.translation_coverage import (
    COVERAGE_AMBIGUOUS,
    COVERAGE_COVERED,
    COVERAGE_IGNORED,
    COVERAGE_SOURCE_ONLY,
    CoverageUnit,
    clean_coverage_text,
    coverage_summary,
    join_lines,
    looks_like_source_text,
    looks_like_target_text,
    split_existing_bilingual_text,
)
from core.word_document import (
    _append_translation_to_cell,
    _cell_source_text,
    _ensure_owner_writable,
    _insert_translation_paragraph_after,
    _is_toc_or_field_paragraph,
    _iter_unique_table_cells,
    _normalize_word_output_name,
    _paragraph_source_text,
    _sanitize_filename_fragment,
)

_SCHEME_KEYWORD = "方案"
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_LATIN_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ]")


@dataclass
class WordCoveragePlan:
    path: Path
    units: list[CoverageUnit]

    @property
    def source_units(self) -> list[CoverageUnit]:
        return [unit for unit in self.units if unit.status == COVERAGE_SOURCE_ONLY]

    @property
    def source_texts(self) -> list[str]:
        seen: set[str] = set()
        texts: list[str] = []
        for unit in self.source_units:
            source = unit.source_text.strip()
            if source and source not in seen:
                seen.add(source)
                texts.append(source)
        return texts

    @property
    def summary(self) -> dict[str, int]:
        return coverage_summary(self.units)


def build_word_coverage_plan(
    path: str | Path,
    *,
    target_lang: str,
    source_lang: str = "zh",
    protect_scheme_cover: bool = False,
) -> WordCoveragePlan:
    """Classify app-style bilingual Word content by coverage status."""
    source_path = Path(path)
    doc = Document(str(source_path))
    scheme_cover = _detect_scheme_cover(doc) if protect_scheme_cover else None
    units: list[CoverageUnit] = []
    units.extend(
        _classify_body_paragraphs(
            doc,
            target_lang=target_lang,
            source_lang=source_lang,
            scheme_cover=scheme_cover,
        )
    )
    units.extend(
        _classify_table_cells(
            doc,
            target_lang=target_lang,
            source_lang=source_lang,
            scheme_cover=scheme_cover,
        )
    )
    return WordCoveragePlan(path=source_path, units=units)


def write_untranslated_docx(
    *,
    source_path: str | Path,
    output_dir: str | Path,
    plan: WordCoveragePlan,
    translations: dict[str, str],
    target_lang: str,
    source_lang: str = "zh",
    output_name: str | None = None,
    log_callback=None,
) -> Path:
    """Copy a Word document and insert translations only at source-only positions."""
    source_path = Path(source_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

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

    paragraph_insertions = 0
    table_insertions = 0
    for unit in reversed(plan.source_units):
        translation = str(translations.get(unit.source_text.strip()) or "").strip()
        if not translation or translation.casefold() == unit.source_text.strip().casefold():
            continue
        kind = str(unit.kind or "")
        if kind == "paragraph":
            index = int(unit.data.get("paragraph_index", -1))
            if index < 0 or index >= len(body_paragraphs):
                continue
            paragraph = body_paragraphs[index]
            if _paragraph_source_text(paragraph) != unit.source_text.strip():
                continue
            _insert_translation_paragraph_after(
                paragraph,
                translation,
                target_lang=target_lang,
            )
            paragraph_insertions += 1
        elif kind == "table_cell":
            index = int(unit.data.get("cell_index", -1))
            if index < 0 or index >= len(table_cells):
                continue
            cell = table_cells[index]
            if _cell_source_text(cell) != unit.source_text.strip():
                continue
            _append_translation_to_cell(
                cell,
                translation,
                target_lang=target_lang,
            )
            table_insertions += 1

    doc.save(str(out_path))
    if log_callback:
        log_callback(
            f"[OK] 已输出：{out_path.name}（补译段落 {paragraph_insertions}，表格单元格 {table_insertions}）"
        )
    return out_path


def _classify_body_paragraphs(
    doc: Document,
    *,
    target_lang: str,
    source_lang: str,
    scheme_cover: "_SchemeCoverInfo | None" = None,
) -> list[CoverageUnit]:
    units: list[CoverageUnit] = []
    paragraphs = list(doc.paragraphs)
    consumed_targets: set[int] = set()
    index = 0
    while index < len(paragraphs):
        paragraph = paragraphs[index]
        text = _paragraph_source_text(paragraph)
        location = f"body.paragraph[{index}]"
        data = {"paragraph_index": index}
        if not text:
            index += 1
            continue
        if _is_toc_or_field_paragraph(paragraph):
            units.append(
                CoverageUnit(
                    source_text=text,
                    status=COVERAGE_IGNORED,
                    location=location,
                    kind="paragraph",
                    reason="目录或域段落默认跳过。",
                    data=data,
                )
            )
            index += 1
            continue
        if index in consumed_targets:
            index += 1
            continue
        if _is_scheme_cover_foreign_title(paragraph, index, scheme_cover):
            next_index = index + 1
            next_text = (
                _paragraph_source_text(paragraphs[next_index])
                if next_index < len(paragraphs)
                else ""
            )
            if next_text and looks_like_target_text(
                next_text,
                source_lang=source_lang,
                target_lang=target_lang,
            ):
                units.append(
                    CoverageUnit(
                        source_text=text,
                        target_text=next_text,
                        status=COVERAGE_COVERED,
                        location=location,
                        kind="paragraph",
                        reason="方案封面外文标题已有紧邻目标语言译文。",
                        data=data,
                    )
                )
                consumed_targets.add(next_index)
                index += 2
                continue
            units.append(
                CoverageUnit(
                    source_text=text,
                    status=COVERAGE_SOURCE_ONLY,
                    location=location,
                    kind="paragraph",
                    reason="方案封面中文标题下方外文标题例外：始终重新补译。",
                    data=data,
                )
            )
            index += 1
            continue
        if _is_protected_scheme_cover_paragraph(paragraph, index, scheme_cover):
            units.append(
                CoverageUnit(
                    source_text=text,
                    status=COVERAGE_IGNORED,
                    location=location,
                    kind="paragraph",
                    reason="方案封面保护：普通封面内容默认跳过。",
                    data=data,
                )
            )
            index += 1
            continue
        if looks_like_source_text(text, source_lang=source_lang, target_lang=target_lang):
            next_index = index + 1
            next_text = (
                _paragraph_source_text(paragraphs[next_index])
                if next_index < len(paragraphs)
                else ""
            )
            if next_text and looks_like_target_text(
                next_text,
                source_lang=source_lang,
                target_lang=target_lang,
            ):
                units.append(
                    CoverageUnit(
                        source_text=text,
                        target_text=next_text,
                        status=COVERAGE_COVERED,
                        location=location,
                        kind="paragraph",
                        reason="下一段为目标语言译文。",
                        data=data,
                    )
                )
                consumed_targets.add(next_index)
                index += 2
                continue
            units.append(
                CoverageUnit(
                    source_text=text,
                    status=COVERAGE_SOURCE_ONLY,
                    location=location,
                    kind="paragraph",
                    reason="源语言段落后未发现紧邻目标语言译文。",
                    data=data,
                )
            )
            index += 1
            continue
        if looks_like_target_text(text, source_lang=source_lang, target_lang=target_lang):
            units.append(
                CoverageUnit(
                    source_text="",
                    target_text=text,
                    status=COVERAGE_IGNORED,
                    location=location,
                    kind="paragraph",
                    reason="段落看起来是目标语言译文，默认跳过。",
                    data=data,
                )
            )
            index += 1
            continue
        units.append(
            CoverageUnit(
                source_text=text,
                status=COVERAGE_IGNORED,
                location=location,
                kind="paragraph",
                reason="段落不符合补译候选规则。",
                data=data,
            )
        )
        index += 1
    return units


@dataclass(frozen=True)
class _SchemeCoverInfo:
    protected_until: int
    foreign_title_index: int | None = None


def _detect_scheme_cover(doc: Document) -> _SchemeCoverInfo | None:
    paragraphs = list(doc.paragraphs)
    non_empty = [
        (index, _paragraph_source_text(paragraph))
        for index, paragraph in enumerate(paragraphs)
        if _paragraph_source_text(paragraph)
    ]
    if not non_empty:
        return None

    title_position: int | None = None
    foreign_title_index: int | None = None
    for position, (index, text) in enumerate(non_empty[:6]):
        if _SCHEME_KEYWORD not in text or not _CJK_RE.search(text):
            continue
        title_position = position
        for next_index, next_text in non_empty[position + 1 : min(position + 4, len(non_empty))]:
            if _looks_like_foreign_title(next_text):
                foreign_title_index = next_index
                break
        break
    if title_position is None:
        return None

    first_body_index = _first_body_heading_index(non_empty)
    protected_until = first_body_index if first_body_index is not None else non_empty[min(len(non_empty), 12) - 1][0] + 1
    return _SchemeCoverInfo(
        protected_until=protected_until,
        foreign_title_index=foreign_title_index,
    )


def _first_body_heading_index(non_empty: list[tuple[int, str]]) -> int | None:
    for index, text in non_empty:
        if re.match(r"^第[一二三四五六七八九十百]+章\b", text):
            return index
        if re.match(r"^\d+(?:\.\d+){1,4}(?:\s+|$)", text):
            return index
    return None


def _looks_like_foreign_title(text: str) -> bool:
    value = str(text or "").strip()
    return bool(value and not _CJK_RE.search(value) and _LATIN_RE.search(value))


def _is_scheme_cover_foreign_title(
    paragraph,
    index: int,
    scheme_cover: _SchemeCoverInfo | None,
) -> bool:
    return bool(
        scheme_cover is not None
        and scheme_cover.foreign_title_index is not None
        and index == scheme_cover.foreign_title_index
        and _looks_like_foreign_title(_paragraph_source_text(paragraph))
    )


def _is_protected_scheme_cover_paragraph(
    paragraph,
    index: int,
    scheme_cover: _SchemeCoverInfo | None,
) -> bool:
    if scheme_cover is None:
        return False
    if index >= scheme_cover.protected_until:
        return False
    if scheme_cover.foreign_title_index is not None and index == scheme_cover.foreign_title_index:
        return False
    return True


def _classify_table_cells(
    doc: Document,
    *,
    target_lang: str,
    source_lang: str,
    scheme_cover: _SchemeCoverInfo | None = None,
) -> list[CoverageUnit]:
    units: list[CoverageUnit] = []
    cell_index = 0
    for table_index, table in enumerate(doc.tables):
        protect_table = _is_protected_scheme_cover_table(table, table_index, scheme_cover)
        for cell in _iter_unique_table_cells(table):
            text = _cell_source_text(cell)
            location = f"table[{table_index}].cell[{cell_index}]"
            data = {"cell_index": cell_index, "table_index": table_index}
            if protect_table and clean_coverage_text(text):
                unit = CoverageUnit(
                    source_text=text,
                    status=COVERAGE_IGNORED,
                    location=location,
                    kind="table_cell",
                    section_path="表格",
                    reason="方案封面保护：封面元数据表格默认跳过。",
                    data=data,
                )
            else:
                unit = _classify_cell_text(
                    text,
                    location=location,
                    data=data,
                    source_lang=source_lang,
                    target_lang=target_lang,
                )
            if unit is not None:
                units.append(unit)
            cell_index += 1
    return units


def _is_protected_scheme_cover_table(
    table,
    table_index: int,
    scheme_cover: _SchemeCoverInfo | None,
) -> bool:
    if scheme_cover is None or table_index != 0:
        return False
    texts = [
        _cell_source_text(cell)
        for cell in _iter_unique_table_cells(table)
        if _cell_source_text(cell)
    ]
    combined = "\n".join(texts).casefold()
    markers = (
        "文件编号",
        "docno",
        "日期",
        "date",
        "编制",
        "preparedby",
        "审核",
        "approvedby",
        "批准",
    )
    return any(marker in combined for marker in markers)


def _classify_cell_text(
    text: str,
    *,
    location: str,
    data: dict,
    source_lang: str,
    target_lang: str,
) -> CoverageUnit | None:
    cleaned = clean_coverage_text(text)
    if not cleaned:
        return None
    paragraph_lines = [line for line in cleaned.splitlines() if line.strip()]
    split = split_existing_bilingual_text(
        join_lines(paragraph_lines),
        source_lang=source_lang,
        target_lang=target_lang,
    )
    if split is not None:
        source, target = split
        return CoverageUnit(
            source_text=source,
            target_text=target,
            status=COVERAGE_COVERED,
            location=location,
            kind="table_cell",
            section_path="表格",
            reason="表格单元格内已包含源文和目标语言译文。",
            data=data,
        )
    if looks_like_source_text(cleaned, source_lang=source_lang, target_lang=target_lang):
        return CoverageUnit(
            source_text=cleaned,
            status=COVERAGE_SOURCE_ONLY,
            location=location,
            kind="table_cell",
            section_path="表格",
            reason="表格单元格包含源语言文本，未识别到目标语言译文。",
            data=data,
        )
    if looks_like_target_text(cleaned, source_lang=source_lang, target_lang=target_lang):
        return CoverageUnit(
            source_text="",
            target_text=cleaned,
            status=COVERAGE_IGNORED,
            location=location,
            kind="table_cell",
            section_path="表格",
            reason="表格单元格看起来是目标语言译文，默认跳过。",
            data=data,
        )
    if len(paragraph_lines) > 1:
        return CoverageUnit(
            source_text=cleaned,
            status=COVERAGE_AMBIGUOUS,
            location=location,
            kind="table_cell",
            section_path="表格",
            reason="表格单元格无法可靠拆分源文和译文。",
            data=data,
        )
    return CoverageUnit(
        source_text=cleaned,
        status=COVERAGE_IGNORED,
        location=location,
        kind="table_cell",
        section_path="表格",
        reason="表格单元格不符合补译候选规则。",
        data=data,
    )
