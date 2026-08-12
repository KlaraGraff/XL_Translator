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
_NON_CJK_LETTER_RUN_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
# \u8bd1\u6587\u91cc\u5939\u5e26\u7684\u4e2d\u6587\u7247\u6bb5\uff1a\u628a\u7d27\u90bb\u7684\u6570\u5b57\u4e00\u5e76\u6536\u8fdb\u6765\uff0c\u62a5\u544a\u91cc\u624d\u770b\u5f97\u51fa\u662f\u300c2026\u5e748\u67089\u65e5\u300d
# \u8fd9\u79cd\u65e5\u671f\uff0c\u800c\u4e0d\u662f\u5b64\u96f6\u96f6\u4e00\u4e2a\u300c\u5e74\u300d\u5b57\u3002
_CJK_FRAGMENT_RE = re.compile(r"[\u4e00-\u9fff\d\uff10-\uff19]*[\u4e00-\u9fff][\u4e00-\u9fff\d\uff10-\uff19]*")

# \u300c\u8bd1\u6587\u91cc\u53ea\u5939\u5e26\u5c11\u91cf\u4e2d\u6587\u300d\u7684\u5224\u5b9a\u9608\u503c\u3002\u7f16\u53f7\u524d\u7f00\uff08\u4e00.1\uff09\u3001\u65e5\u671f\uff082026\u5e748\u67089\u65e5\uff09\u3001
# \u4e2a\u522b\u4e13\u540d\u5c5e\u4e8e\u8fd9\u4e00\u7c7b\uff1a\u6574\u53e5\u5df2\u7ecf\u662f\u76ee\u6807\u8bed\u8a00\uff0c\u4e0d\u8be5\u5224\u6210\u300c\u672a\u8bd1\u6e90\u6587\u300d\u3002
_INCIDENTAL_CJK_MAX_CHARS = 12
_INCIDENTAL_CJK_MAX_RATIO = 0.2
_INCIDENTAL_CJK_MIN_LETTERS = 12

_FRENCH_MARKER_WORDS = {
    "avec", "aux", "ce", "ces", "cette", "dans", "des", "du", "est",
    "et", "la", "le", "les", "pour", "sans", "sur", "une",
}
_ENGLISH_MARKER_WORDS = {
    "and", "are", "for", "from", "in", "is", "of", "on", "the", "this",
    "to", "with", "without",
}
_FRENCH_ELISION_RE = re.compile(r"\b(?:[cdjlmnstqu]|jusqu|lorsqu)['’]", re.IGNORECASE)
_FRENCH_DIACRITIC_RE = re.compile(r"[àâçéèêëîïôùûüÿœæ]", re.IGNORECASE)


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


def contains_meaningful_non_cjk_word(text: str) -> bool:
    """Return whether text contains a likely natural-language target word."""
    for match in _NON_CJK_LETTER_RUN_RE.finditer(str(text or "")):
        token = match.group(0)
        if contains_cjk(token):
            continue
        if sum(1 for char in token if char.isalpha()) >= 3:
            return True
    return False


def count_cjk_chars(text: str) -> int:
    return len(_CJK_RE.findall(str(text or "")))


def count_non_cjk_letters(text: str) -> int:
    return sum(1 for char in str(text or "") if char.isalpha() and not _CJK_RE.match(char))


def residual_cjk_fragments(text: str, *, limit: int = 3) -> list[str]:
    """Return the CJK fragments left inside an otherwise translated segment."""
    fragments: list[str] = []
    seen: set[str] = set()
    for match in _CJK_FRAGMENT_RE.finditer(str(text or "")):
        fragment = match.group(0).strip()
        if not fragment or fragment in seen:
            continue
        seen.add(fragment)
        fragments.append(fragment)
        if len(fragments) >= limit:
            break
    return fragments


def has_incidental_cjk(text: str, *, target_lang: str) -> bool:
    """Return whether text is target-language prose carrying only a few CJK chars."""
    cleaned = clean_coverage_text(text)
    if not cleaned:
        return False
    if str(target_lang or "").strip().lower() in {"zh", "ja"}:
        return False

    cjk_count = count_cjk_chars(cleaned)
    if cjk_count == 0 or cjk_count > _INCIDENTAL_CJK_MAX_CHARS:
        return False
    letter_count = count_non_cjk_letters(cleaned)
    if letter_count < _INCIDENTAL_CJK_MIN_LETTERS:
        return False
    if cjk_count > letter_count * _INCIDENTAL_CJK_MAX_RATIO:
        return False
    return contains_meaningful_non_cjk_word(cleaned)


def _language_evidence(text: str, language: str) -> bool | None:
    """Return positive/negative evidence for the supported Latin pair, else unknown."""
    normalized = str(language or "").strip().lower()
    if normalized not in {"en", "fr"}:
        return None
    words = {match.group(0).casefold() for match in _NON_CJK_LETTER_RUN_RE.finditer(text)}
    french_score = len(words & _FRENCH_MARKER_WORDS)
    english_score = len(words & _ENGLISH_MARKER_WORDS)
    if _FRENCH_DIACRITIC_RE.search(text) or _FRENCH_ELISION_RE.search(text):
        french_score += 2
    own_score = french_score if normalized == "fr" else english_score
    other_score = english_score if normalized == "fr" else french_score
    if own_score > other_score:
        return True
    if other_score > own_score:
        return False
    return None


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
        if not contains_cjk(cleaned):
            return False
        if _looks_translated_despite_cjk(
            cleaned,
            source_lang=source,
            target_lang=target_lang,
        ):
            return False
        return should_translate(
            cleaned,
            target_lang=target_lang,
            source_lang=source_lang,
        )

    if contains_cjk(cleaned):
        return False
    source_evidence = _language_evidence(cleaned, source)
    if source_evidence is False:
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
        # 整句已是目标语言、只夹带编号或日期这类零星中文时，仍算译文；
        # 残留的中文另由 residual_cjk_fragments() 单独提示，不再判成「未译源文」。
        return _looks_translated_despite_cjk(
            cleaned,
            source_lang=source_lang,
            target_lang=target,
        )
    target_evidence = _language_evidence(cleaned, target)
    if target_evidence is False:
        return False
    return contains_meaningful_non_cjk_word(cleaned)


def _looks_translated_despite_cjk(
    cleaned: str,
    *,
    source_lang: str,
    target_lang: str,
) -> bool:
    if str(source_lang or "zh").strip().lower() != "zh":
        return False
    if not has_incidental_cjk(cleaned, target_lang=target_lang):
        return False
    return _language_evidence(cleaned, str(target_lang or "").strip().lower()) is not False


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
