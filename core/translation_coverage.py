"""Deterministic coverage detection for untranslated-only tasks."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from core.translation_filter import should_translate

COVERAGE_COVERED = "covered"
COVERAGE_SOURCE_ONLY = "source_only"
COVERAGE_AMBIGUOUS = "ambiguous"
COVERAGE_IGNORED = "ignored"

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


@dataclass
class CoverageUnit:
    """One source/translation coverage decision at a concrete document position."""

    source_text: str
    status: str
    location: str
    reason: str
    target_text: str = ""
    kind: str = ""
    section_path: str = ""
    data: dict = field(default_factory=dict)


def clean_coverage_text(text: str | None) -> str:
    return str(text or "").strip()


def non_empty_lines(text: str | None) -> list[str]:
    return [line.strip() for line in str(text or "").splitlines() if line.strip()]


def join_lines(lines: list[str]) -> str:
    return "\n".join(line.strip() for line in lines if line.strip()).strip()


def contains_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(str(text or "")))


def contains_non_cjk_letters(text: str) -> bool:
    return any(char.isalpha() and not contains_cjk(char) for char in str(text or ""))


def looks_like_source_text(
    text: str,
    *,
    source_lang: str,
    target_lang: str,
) -> bool:
    """Return whether text is a credible source-language segment."""
    cleaned = clean_coverage_text(text)
    if not cleaned:
        return False

    source = str(source_lang or "zh").strip().lower()
    if source == "zh":
        return contains_cjk(cleaned) and should_translate(
            cleaned,
            target_lang=target_lang,
            source_lang=source_lang,
        )

    if contains_cjk(cleaned):
        return False
    return contains_non_cjk_letters(cleaned) and should_translate(
        cleaned,
        target_lang=target_lang,
        source_lang=source_lang,
    )


def looks_like_target_text(
    text: str,
    *,
    source_lang: str,
    target_lang: str,
) -> bool:
    """Return whether text is a credible target-language segment."""
    cleaned = clean_coverage_text(text)
    if not cleaned:
        return False

    target = str(target_lang or "").strip().lower()
    if target == "zh":
        return contains_cjk(cleaned)

    if contains_cjk(cleaned):
        return False
    return contains_non_cjk_letters(cleaned)


def split_existing_bilingual_text(
    text: str,
    *,
    source_lang: str,
    target_lang: str,
) -> tuple[str, str] | None:
    """
    Split app-style bilingual text into source and target parts.

    The split is intentionally conservative: it only accepts a boundary where
    the left side looks like source text and the right side looks like target
    text. This lets multi-line source text stay intact.
    """
    lines = non_empty_lines(text)
    if len(lines) < 2:
        return None

    for split_index in range(1, len(lines)):
        source_candidate = join_lines(lines[:split_index])
        target_candidate = join_lines(lines[split_index:])
        if not source_candidate or not target_candidate:
            continue
        if not looks_like_source_text(
            source_candidate,
            source_lang=source_lang,
            target_lang=target_lang,
        ):
            continue
        if not looks_like_target_text(
            target_candidate,
            source_lang=source_lang,
            target_lang=target_lang,
        ):
            continue
        return source_candidate, target_candidate
    return None


def coverage_summary(units: list[CoverageUnit]) -> dict[str, int]:
    summary = {
        COVERAGE_COVERED: 0,
        COVERAGE_SOURCE_ONLY: 0,
        COVERAGE_AMBIGUOUS: 0,
        COVERAGE_IGNORED: 0,
    }
    for unit in units:
        summary[unit.status] = summary.get(unit.status, 0) + 1
    return summary
