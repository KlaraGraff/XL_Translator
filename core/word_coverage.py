"""Word coverage detection and position-based untranslated-only writing."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
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
    has_incidental_cjk,
    join_lines,
    looks_like_source_text,
    looks_like_target_text,
    residual_cjk_fragments,
    split_existing_bilingual_text,
)
from core.word_document import (
    WordFrontMatterBoundary,
    _append_translation_to_cell,
    _cell_source_text,
    _ensure_owner_writable,
    _insert_translation_paragraph_after,
    _is_toc_or_field_paragraph,
    _iter_unique_table_cells,
    _normalize_word_output_name,
    _paragraph_source_text,
    _sanitize_filename_fragment,
    find_word_front_matter_boundary,
)

# "未请求前置内容保护"时的占位边界：found 恒为 False，因此下面的分类逻辑天然
# 不会保护任何段落或表格——不需要另外一个 if protect_front_matter 分支。
_NO_FRONT_MATTER_PROTECTION = WordFrontMatterBoundary(body_start_index=None)


@dataclass
class WordCoveragePlan:
    path: Path
    units: list[CoverageUnit]
    front_matter: WordFrontMatterBoundary = field(default_factory=lambda: _NO_FRONT_MATTER_PROTECTION)

    @property
    def source_units(self) -> list[CoverageUnit]:
        return [unit for unit in self.units if unit.status == COVERAGE_SOURCE_ONLY]

    @property
    def residual_units(self) -> list[CoverageUnit]:
        """Covered positions whose translation still carries a few CJK characters."""
        return [unit for unit in self.units if unit.data.get("residual_cjk")]

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
    protect_front_matter: bool = False,
) -> WordCoveragePlan:
    """Classify app-style bilingual Word content by coverage status."""
    source_path = Path(path)
    doc = Document(str(source_path))
    front_matter = (
        find_word_front_matter_boundary(doc)
        if protect_front_matter
        else _NO_FRONT_MATTER_PROTECTION
    )
    units: list[CoverageUnit] = []
    units.extend(
        _classify_body_paragraphs(
            doc,
            target_lang=target_lang,
            source_lang=source_lang,
            front_matter=front_matter,
        )
    )
    units.extend(
        _classify_table_cells(
            doc,
            target_lang=target_lang,
            source_lang=source_lang,
            front_matter=front_matter,
        )
    )
    return WordCoveragePlan(path=source_path, units=units, front_matter=front_matter)


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


def _mark_residual_cjk(
    data: dict,
    target_text: str,
    *,
    target_lang: str,
    location: str,
) -> None:
    """Record the CJK left inside an accepted translation, for a softer report item."""
    if not has_incidental_cjk(target_text, target_lang=target_lang):
        return
    fragments = residual_cjk_fragments(target_text)
    if not fragments:
        return
    data["residual_cjk"] = fragments
    data["residual_location"] = location
    data["residual_text"] = clean_coverage_text(target_text)


def _classify_body_paragraphs(
    doc: Document,
    *,
    target_lang: str,
    source_lang: str,
    front_matter: WordFrontMatterBoundary = _NO_FRONT_MATTER_PROTECTION,
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
        if index in front_matter.protected_paragraph_indices:
            units.append(
                CoverageUnit(
                    source_text=text,
                    status=COVERAGE_IGNORED,
                    location=location,
                    kind="paragraph",
                    reason="前置内容保护：正文标题之前的封面/目录/前言默认跳过。",
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
                _mark_residual_cjk(
                    data,
                    next_text,
                    target_lang=target_lang,
                    location=f"body.paragraph[{next_index}]",
                )
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
            _mark_residual_cjk(data, text, target_lang=target_lang, location=location)
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


def _classify_table_cells(
    doc: Document,
    *,
    target_lang: str,
    source_lang: str,
    front_matter: WordFrontMatterBoundary = _NO_FRONT_MATTER_PROTECTION,
) -> list[CoverageUnit]:
    units: list[CoverageUnit] = []
    cell_index = 0
    for table_index, table in enumerate(doc.tables):
        protect_table = table_index in front_matter.protected_table_indices
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
                    reason="前置内容保护：正文标题之前的表格（常见于封面）默认跳过。",
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
        _mark_residual_cjk(data, target, target_lang=target_lang, location=location)
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
        _mark_residual_cjk(data, cleaned, target_lang=target_lang, location=location)
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
