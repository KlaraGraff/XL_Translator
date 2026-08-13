"""Deterministic coverage detection for untranslated-only tasks."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from core.language_registry import get_default_source_lang, is_auto_source_lang
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
#
# \u4e4b\u524d\u53ea\u6309\u7edd\u5bf9\u5b57\u6570\uff0812 \u4e2a\u5b57\uff09\u5224\u5b9a\uff0c\u5b9e\u6d4b\u5750\u5b9e\u8fc7\u4e00\u6b21\u771f\u5b9e\u8bef\u62a5\uff1a1330 \u5b57\u7684\u6cd5\u6587\u8bd1\u6587\u6bb5\u843d\u91cc
# \u5939\u4e86 15 \u4e2a\u4e2d\u6587\u5b57\uff08\u5168\u662f\u65e5\u671f\uff0c\u5982\u300c2025\u5e7412\u67088\u65e5\u300d\uff09\uff0c15 > 12\uff0c\u4e8e\u662f\u6574\u6bb5\u88ab\u5224\u6210\u4e2d\u6587
# \u539f\u6587\uff0c\u8ddf\u5b83\u914d\u5bf9\u7684\u539f\u6587\u6bb5\u843d\u4e5f\u4e00\u8d77\u88ab\u62a5\u6210\u300c\u672a\u8bd1\u6e90\u6587\u300d\u2014\u2014\u4e24\u6761\u8bef\u62a5\uff0c\u5b9e\u9645\u4e0a\u662f\u4e00\u6bb5\u7ffb\u5b8c\u6574
# \u7684\u8bd1\u6587\u3002\u6539\u6210\u6309\u6bd4\u4f8b\u5224\u5b9a\uff1a\u4e2d\u6587\u5b57\u6570\u5360\u5168\u6587\u957f\u5ea6\u7684\u6bd4\u4f8b\u8db3\u591f\u4f4e\uff0c\u624d\u7b97\u300c\u987a\u5e26\u5939\u5e26\u300d\uff1b\u540c\u65f6
# \u4fdd\u7559\u4e00\u4e2a\u5bbd\u677e\u7684\u7edd\u5bf9\u4e0a\u9650\uff0c\u9632\u6b62\u6bd4\u4f8b\u7b97\u6cd5\u5728\u8d85\u957f\u6587\u6863\u91cc\u653e\u8fc7\u5927\u6bb5\u771f\u6b63\u6ca1\u7ffb\u7684\u5185\u5bb9\u3002
_INCIDENTAL_CJK_MAX_CHARS = 60
_INCIDENTAL_CJK_MAX_RATIO_OF_TEXT = 0.05
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

# 极短的目标语言片段：编号符号、计量单位、罗马数字。这类文本天然不满足「至少 3 个
# 字母的自然语言词」门槛（"N°" 只有一个字母），但整份双语文档里大量出现——常见于
# 「序号 / N°」这类只有一个词的表头格。实测坐实过：8 个「序号 / N°」双语格被误判成
# 「未译源文」，根因就是 looks_like_target_text("N°") 原先恒为 False。这里按需求列出
# 的例子（N°、m²、kg、%、Réf.、No.、纯数字、罗马数字）加一个前置正则，命中就直接判
# 定为有效的目标语言内容，不再送进「至少 3 个字母」的自然语言词检测。
_SHORT_TARGET_TOKEN_RE = re.compile(
    r"""
    N°\.?|                                            # N° / N°.
    N[o0]\.?|Nº\.?|                                    # No. / No / Nº
    R[ée]f\.?|                                              # Réf. / Ref.
    Art\.?|                                                 # Art.
    §\s*[0-9]+(?:[.,][0-9]+)*|                         # § 3.2
    [0-9]+(?:[.,][0-9]+)*\s*
        (?:%|°C|°|m²|m³|km²|km|kg|g|mm|cm)?|
    m²|m³|km²|km|kg|g|mm|cm|%|°C|
    M{0,3}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|I?V?I{0,3})
    """,
    re.IGNORECASE | re.VERBOSE,
)


def looks_like_short_target_token(text: str) -> bool:
    """Return whether text is a short target-language token (N°、m²、kg、罗马数字……)."""
    cleaned = clean_coverage_text(text)
    if not cleaned:
        return False
    return bool(_SHORT_TARGET_TOKEN_RE.fullmatch(cleaned))


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
    total_len = len(cleaned)
    if total_len == 0 or cjk_count / total_len > _INCIDENTAL_CJK_MAX_RATIO_OF_TEXT:
        # 按占比判定，而不是只看绝对字数：短标题（如「抢工方案」，4 字 100% 中文）
        # 必须仍判成源语言；长段落里夹几个字的日期（1330 字里 15 个中文字，1.1%）
        # 不该被这一条拦下。
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


def resolve_coverage_source_lang(source_lang: str | None) -> str:
    """Turn 「自动识别」 into a concrete source language for coverage checks.

    补译判定必须先知道源语言是哪一门，才分得清「原文」和「译文」。而自动识别是在
    提取之后才出结果的，提取阶段拿到的就是字面量 auto——它既不是 zh 也不是任何
    受支持的语言码，于是所有中文单元格都会被判成「不是源文」，补译清单变成 0 条，
    最后输出一份一个字都没翻的文件。这里统一落到默认源语言，不让 auto 漏进判定。
    """
    candidate = str(source_lang or "").strip()
    if not candidate or is_auto_source_lang(candidate):
        return get_default_source_lang()
    return candidate


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

    source_lang = resolve_coverage_source_lang(source_lang)
    source = source_lang.lower()
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

    if looks_like_short_target_token(cleaned):
        return True

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
    if resolve_coverage_source_lang(source_lang).lower() != "zh":
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

    # 边界从末尾往前扫，而不是从「只有第一行是原文」往后扫。原文本身可能有好几
    # 行——实测坐实过一例：单元格第一行「变配电室」、第二行「专项」，第三行才是
    # 法文译文；旧代码从 split_index=1 试起，第一次就命中「源文=变配电室，译文=
    # 专项+法文」，把「专项」这个真正的中文原文错判成译文里的残留中文。
    #
    # 但也不能反过来一路贪心到底：译文同样可能有好几行（「污染/破坏/Contamination/
    # Détérioration」），从最大源文侧试起会把 Contamination 吞进原文侧。所以先从末尾
    # 逐行往前扩，直到某一行不再像目标语言为止——这一行就是边界候选，译文侧取到的
    # 是最长的、整段都像目标语言的后缀。
    boundary = len(lines)
    while boundary > 1 and looks_like_target_text(
        lines[boundary - 1],
        source_lang=source_lang,
        target_lang=target_lang,
    ):
        boundary -= 1
    candidates = [boundary] if boundary < len(lines) else []
    # 后缀扫描没收敛时（例如逐行看都不像目标语言、合起来才像），退回旧的逐位试探，
    # 保证行为不比原来差。
    candidates.extend(
        index for index in range(len(lines) - 1, 0, -1) if index != boundary
    )
    for split_index in candidates:
        source_candidate = join_lines(lines[:split_index])
        target_candidate = join_lines(lines[split_index:])
        if not source_candidate or not target_candidate:
            continue
        if not looks_like_target_text(
            target_candidate,
            source_lang=source_lang,
            target_lang=target_lang,
        ):
            continue
        if not looks_like_source_text(
            source_candidate,
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
