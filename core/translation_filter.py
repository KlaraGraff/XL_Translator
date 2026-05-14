"""
翻译判定过滤器。
完整迁移原 GAS 宏的 shouldTranslate() 逻辑。
"""
import re
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

_CHINESE_CHAR_RE = re.compile(r"[\u4e00-\u9fa5]")
_NUMBER_TOKEN_RE = re.compile(r"\d+(?:[.,]\d+)?")
_SEMANTIC_NUMBER_RE = re.compile(r"[-+]?\d+(?:[.,]\d+)*(?:\s*[万亿])?")
_CJK_SPAN_RE = re.compile(r"[\u4e00-\u9fa5]+")

VALIDATION_STATUS_PASS = "pass"
VALIDATION_STATUS_FAIL = "fail"
VALIDATION_STATUS_SOFT_PASS_REVIEW = "soft_pass_review"

VALIDATION_PROFILE_STRICT = "strict"
VALIDATION_PROFILE_WORD_RECOVERY = "word_recovery"


@dataclass(frozen=True)
class TranslationValidationIssue:
    code: str
    message: str
    fragments: tuple[str, ...] = ()


@dataclass(frozen=True)
class TranslationValidationResult:
    status: str = VALIDATION_STATUS_PASS
    issues: tuple[TranslationValidationIssue, ...] = ()

    @property
    def is_pass(self) -> bool:
        return self.status == VALIDATION_STATUS_PASS

    @property
    def is_fail(self) -> bool:
        return self.status == VALIDATION_STATUS_FAIL

    @property
    def needs_review(self) -> bool:
        return self.status == VALIDATION_STATUS_SOFT_PASS_REVIEW

    @property
    def review_fragments(self) -> tuple[str, ...]:
        fragments: list[str] = []
        seen: set[str] = set()
        for issue in self.issues:
            for fragment in issue.fragments:
                cleaned = str(fragment or "").strip()
                if not cleaned or cleaned in seen:
                    continue
                seen.add(cleaned)
                fragments.append(cleaned)
        return tuple(fragments)


@dataclass(frozen=True)
class _NumberToken:
    token: str
    normalized: str
    fragment: str
    weak: bool = False


# ── 辅助函数 ──────────────────────────────────────────────────────────────────

def _check_numbers_intact(original: str, translated: str) -> bool:
    """
    数字完整性模糊校验：验证译文是否保留了原文的所有数值。

    允许：符号（* / × / x）或空格的变化（如 100×200 → 100*200）。
    不允许：任何数值缺失或频次减少（如 200*200 被截断为 200）。

    实现：提取整数/小数数值序列，使用 Counter 频次对比。
    """
    return not _missing_number_tokens(original, translated)


def _normalize_number_token(token: str) -> str:
    """Normalize dot/comma decimal variants for cross-language number checks."""
    return str(token or "").replace(",", ".")


def _find_number_tokens(text: str) -> list[_NumberToken]:
    tokens: list[_NumberToken] = []
    source = str(text or "")
    for match in _NUMBER_TOKEN_RE.finditer(source):
        token = match.group(0)
        weak = _is_weak_embedded_noise_number(source, match.start(), match.end())
        tokens.append(
            _NumberToken(
                token=token,
                normalized=_normalize_number_token(token),
                fragment=(
                    _weak_number_context_fragment(source, match.start(), match.end())
                    if weak
                    else _number_context_fragment(source, match.start(), match.end())
                ),
                weak=weak,
            )
        )
    return tokens


def _missing_number_tokens(
    original: str,
    translated: str,
    *,
    skip_weak_source: bool = False,
) -> list[_NumberToken]:
    source_tokens = [
        token
        for token in _find_number_tokens(original)
        if not (skip_weak_source and token.weak)
    ]
    if not source_tokens:
        return []

    translated_counts = Counter(token.normalized for token in _find_number_tokens(translated))
    missing: list[_NumberToken] = []
    seen_counts: Counter[str] = Counter()
    for token in source_tokens:
        seen_counts[token.normalized] += 1
        if translated_counts[token.normalized] >= seen_counts[token.normalized]:
            continue
        missing.append(token)
    return missing


def _number_context_fragment(text: str, start: int, end: int) -> str:
    source = str(text or "")
    left = start
    while left > 0 and (
        _contains_chinese(source[left - 1]) or source[left - 1].isdigit()
    ):
        left -= 1
    right = end
    while right < len(source) and (
        _contains_chinese(source[right]) or source[right].isdigit()
    ):
        right += 1
    fragment = source[left:right].strip()
    return fragment or source[start:end]


def _weak_number_context_fragment(text: str, start: int, end: int) -> str:
    source = str(text or "")
    left = start
    left_count = 0
    while left > 0 and _contains_chinese(source[left - 1]) and left_count < 2:
        left -= 1
        left_count += 1
    right = end
    right_count = 0
    while right < len(source) and _contains_chinese(source[right]) and right_count < 1:
        right += 1
        right_count += 1
    return source[left:right].strip() or source[start:end]


def _is_weak_embedded_noise_number(text: str, start: int, end: int) -> bool:
    source = str(text or "")
    token = source[start:end]
    if len(token) != 1 or not token.isdigit():
        return False
    previous_char = source[start - 1] if start > 0 else ""
    next_char = source[end] if end < len(source) else ""
    if not (_contains_chinese(previous_char) and _contains_chinese(next_char)):
        return False

    context = source[max(0, start - 2): min(len(source), end + 2)]
    # Keep contract clauses, floors, axes, buildings, dates, units, and amounts strict.
    if re.search(r"[第条章节款号层栋轴线年月日米厘万亿%％]", context):
        return False
    return True


def _semantic_number_counts(text: str, *, skip_weak_source: bool = False) -> Counter[str]:
    counts: Counter[str] = Counter()
    source = str(text or "")
    for match in _SEMANTIC_NUMBER_RE.finditer(source):
        raw = match.group(0)
        if skip_weak_source and _is_weak_embedded_noise_number(
            source,
            match.start(),
            _numeric_part_end(source, match.start(), match.end()),
        ):
            continue
        value = _parse_semantic_number(raw)
        if value is None:
            continue
        counts[_decimal_key(value)] += 1
    return counts


def _numeric_part_end(text: str, start: int, end: int) -> int:
    source = str(text or "")
    cursor = end
    while cursor > start and source[cursor - 1] in {"万", "亿"}:
        cursor -= 1
    while cursor > start and source[cursor - 1].isspace():
        cursor -= 1
    return cursor


def _parse_semantic_number(raw: str) -> Decimal | None:
    token = str(raw or "").strip()
    if not token:
        return None

    multiplier = Decimal(1)
    if token.endswith("万"):
        multiplier = Decimal(10000)
        token = token[:-1].strip()
    elif token.endswith("亿"):
        multiplier = Decimal(100000000)
        token = token[:-1].strip()

    normalized = _normalize_semantic_number_text(token)
    if not normalized:
        return None
    try:
        return Decimal(normalized) * multiplier
    except InvalidOperation:
        return None


def _normalize_semantic_number_text(token: str) -> str:
    cleaned = str(token or "").strip().replace(" ", "")
    if not cleaned:
        return ""

    sign = ""
    if cleaned[0] in "+-":
        sign = cleaned[0]
        cleaned = cleaned[1:]

    separator_count = cleaned.count(",") + cleaned.count(".")
    if separator_count > 1:
        return sign + cleaned.replace(",", "").replace(".", "")
    if "," in cleaned:
        return sign + cleaned.replace(",", ".")
    return sign + cleaned


def _decimal_key(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return str(normalized.quantize(Decimal(1)))
    return format(normalized, "f")


def _check_semantic_numbers_intact(
    original: str,
    translated: str,
    *,
    skip_weak_source: bool = False,
) -> bool:
    original_counts = _semantic_number_counts(original, skip_weak_source=skip_weak_source)
    if not original_counts:
        return True
    translated_counts = _semantic_number_counts(translated)
    return all(
        translated_counts[value] >= count
        for value, count in original_counts.items()
    )


def _contains_chinese(text: str) -> bool:
    return bool(_CHINESE_CHAR_RE.search(text))


def _contains_non_chinese_letters(text: str) -> bool:
    return any(char.isalpha() and not _contains_chinese(char) for char in text)


def _count_non_chinese_letters(text: str) -> int:
    return sum(1 for char in text if char.isalpha() and not _contains_chinese(char))


# ── 主函数 ────────────────────────────────────────────────────────────────────

def should_translate(
    text: str,
    target_lang: str = "",
    source_lang: str = "zh",
) -> bool:
    """
    判断单元格文本是否需要翻译。

    规则优先级（从高到低）：
      1. 空字符串  → 跳过
      2. 含中文字符 → 翻译
      3. 纯数字/符号/空白 → 跳过
      4. 无空格且字母数字混合（型号代码，如 "A3B12"） → 跳过
      5. 含空格（词组或短句） → 翻译
      6. 纯字母且长度 > 3 → 翻译
      7. 其余 → 跳过
    """
    text = text.strip()

    # 规则 1：空字符串
    if not text:
        return False

    if target_lang == "zh":
        # 中文已经是目标语言，本轮最小范围下直接跳过。
        if _contains_chinese(text):
            return False

        # 保留旧分支对纯数字/符号/空白与型号代码的保护。
        if re.match(r'^[\d\s\W_]+$', text):
            return False
        if ' ' not in text and _contains_non_chinese_letters(text) and re.search(r'\d', text):
            return False

        # 任意非中文自然语言到中文：
        # 1) 含空格的词组/短句
        # 2) 不含空格但有足够字母内容的单词（含重音字符、阿拉伯字母等）
        if ' ' in text and _contains_non_chinese_letters(text):
            return True

        letter_count = _count_non_chinese_letters(text)
        if letter_count >= 2:
            return True

        return False

    # 规则 2：含中文字符
    if _contains_chinese(text):
        return True

    # 规则 3：纯数字、符号、空白
    if re.match(r'^[\d\s\W_]+$', text):
        return False

    # 规则 4：无空格且字母数字混合（型号代码）
    if ' ' not in text and re.search(r'[A-Za-z]', text) and re.search(r'\d', text):
        return False

    # 规则 5：含空格（词组/短句）
    if ' ' in text:
        return True

    # 规则 6：纯字母且长度 > 3
    if re.match(r'^[A-Za-z]+$', text) and len(text) > 3:
        return True

    return False


def validate_translation(
    original: str,
    translated: str,
    target_lang: str = "",
    source_lang: str = "zh",
    profile: str = VALIDATION_PROFILE_STRICT,
) -> TranslationValidationResult:
    """
    Validate whether a translation can be accepted.

    `strict` preserves the legacy Excel-compatible quality gate.
    `word_recovery` is intended only for Word single-paragraph retry recovery.
    """
    strict_result = _validate_translation_strict(
        original,
        translated,
        target_lang=target_lang,
        source_lang=source_lang,
    )
    if profile == VALIDATION_PROFILE_WORD_RECOVERY:
        return _validate_translation_word_recovery(
            original,
            translated,
            target_lang=target_lang,
            source_lang=source_lang,
            strict_result=strict_result,
        )
    return strict_result


def _validate_translation_strict(
    original: str,
    translated: str,
    target_lang: str = "",
    source_lang: str = "zh",
) -> TranslationValidationResult:
    issues: list[TranslationValidationIssue] = []
    orig = original.strip()
    tran = translated.strip()

    if not tran:
        issues.append(
            TranslationValidationIssue(
                code="empty_translation",
                message="译文为空。",
            )
        )
        return _validation_result_from_issues(issues)

    # 条件1：完全相同（大小写不敏感）
    if orig.lower() == tran.lower():
        issues.append(
            TranslationValidationIssue(
                code="same_as_source",
                message="译文与原文相同。",
            )
        )
        return _validation_result_from_issues(issues)

    if target_lang == "zh":
        # 目标语言为中文时，若返回内容完全不含中文且原文本身是非中文文本，
        # 基本可以判定模型没有真正完成翻译。
        if not _contains_chinese(tran) and _contains_non_chinese_letters(orig):
            issues.append(
                TranslationValidationIssue(
                    code="missing_target_chinese",
                    message="目标语言为中文，但译文不含中文。",
                )
            )

        missing_numbers = _missing_number_tokens(orig, tran)
        if missing_numbers:
            issues.append(_missing_number_issue(missing_numbers))

        return _validation_result_from_issues(issues)

    # 仅对含中文的原文执行进一步检测
    if not _contains_chinese(orig):
        return _validation_result_from_issues(issues)

    # 条件2：语向感知子串检测
    if not _contains_chinese(tran):
        # 剥离原文中的中文字符，得到"原有非中文部分"
        orig_noncn      = re.sub(r'[\u4e00-\u9fa5]', '', orig)
        orig_noncn_norm = re.sub(r'\s+', '', orig_noncn).lower()
        tran_norm       = re.sub(r'\s+', '', tran).lower()

        if orig_noncn_norm and tran_norm and tran_norm in orig_noncn_norm:
            cn_count  = len(re.findall(r'[\u4e00-\u9fa5]', orig))
            cn_ratio  = cn_count / max(len(orig), 1)
            orig_norm = re.sub(r'\s+', '', orig).lower()
            len_ratio = len(tran_norm) / max(len(orig_norm), 1)
            # 附加守卫：中文占比 > 15%（有实质内容未翻译）或 内容损失 > 30%
            if cn_ratio > 0.15 or len_ratio < 0.7:
                issues.append(
                    TranslationValidationIssue(
                        code="source_non_chinese_only",
                        message="译文疑似只保留了原文中的非中文片段。",
                    )
                )

    # 条件3：数字完整性
    missing_numbers = _missing_number_tokens(orig, tran)
    if missing_numbers:
        issues.append(_missing_number_issue(missing_numbers))

    return _validation_result_from_issues(issues)


def _validate_translation_word_recovery(
    original: str,
    translated: str,
    *,
    target_lang: str,
    source_lang: str,
    strict_result: TranslationValidationResult,
) -> TranslationValidationResult:
    if target_lang == "zh":
        return strict_result

    # Recovery must never rescue empty, unchanged, or obviously non-translated text.
    hard_codes = {
        "empty_translation",
        "same_as_source",
        "missing_target_chinese",
        "source_non_chinese_only",
    }
    if any(issue.code in hard_codes for issue in strict_result.issues):
        return strict_result

    issues: list[TranslationValidationIssue] = [
        issue
        for issue in strict_result.issues
        if issue.code != "missing_number"
    ]

    missing_numbers = _missing_number_tokens(original, translated)
    weak_missing = [token for token in missing_numbers if token.weak]
    numbers_ok = not missing_numbers or _check_semantic_numbers_intact(
        original,
        translated,
        skip_weak_source=True,
    )
    if not numbers_ok:
        issues.append(_missing_number_issue(missing_numbers))
        return _validation_result_from_issues(issues)

    if weak_missing:
        issues.append(
            TranslationValidationIssue(
                code="weak_ocr_number",
                message="原文中存在疑似 OCR 噪声数字，译文主体已通过恢复校验。",
                fragments=_unique_fragments(token.fragment for token in weak_missing),
            )
        )

    residual_issue = _light_residual_chinese_issue(translated, target_lang=target_lang)
    if residual_issue is not None:
        if residual_issue.code == "residual_chinese_blocking":
            issues.append(residual_issue)
            return _validation_result_from_issues(issues)
        issues.append(residual_issue)

    if issues:
        return TranslationValidationResult(
            status=VALIDATION_STATUS_SOFT_PASS_REVIEW,
            issues=tuple(issues),
        )
    return TranslationValidationResult()


def _validation_result_from_issues(
    issues: list[TranslationValidationIssue],
) -> TranslationValidationResult:
    if issues:
        return TranslationValidationResult(
            status=VALIDATION_STATUS_FAIL,
            issues=tuple(issues),
        )
    return TranslationValidationResult()


def _missing_number_issue(
    missing_numbers: list[_NumberToken],
) -> TranslationValidationIssue:
    return TranslationValidationIssue(
        code="missing_number",
        message="译文缺少原文中的数字或数字出现次数不足。",
        fragments=_unique_fragments(token.fragment for token in missing_numbers),
    )


def _unique_fragments(values) -> tuple[str, ...]:
    fragments: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = str(value or "").strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        fragments.append(cleaned)
    return tuple(fragments)


def _light_residual_chinese_issue(
    translated: str,
    *,
    target_lang: str,
) -> TranslationValidationIssue | None:
    if target_lang in {"zh", "ja"}:
        return None
    spans = _CJK_SPAN_RE.findall(str(translated or ""))
    if not spans:
        return None

    total_cjk = sum(len(span) for span in spans)
    non_space_len = max(len(re.sub(r"\s+", "", str(translated or ""))), 1)
    fragments = _unique_fragments(spans)
    if all(set(span) <= {"万", "亿"} for span in spans) and total_cjk <= 4:
        return TranslationValidationIssue(
            code="residual_chinese_light",
            message="译文仅残留少量中文数量单位，已作为 Word 恢复提示处理。",
            fragments=fragments,
        )
    if total_cjk >= 4 or (total_cjk / non_space_len) > 0.04:
        return TranslationValidationIssue(
            code="residual_chinese_blocking",
            message="译文仍残留较多中文，疑似未完整翻译。",
            fragments=fragments,
        )
    return TranslationValidationIssue(
        code="residual_chinese_light",
        message="译文残留少量中文，已作为 Word 恢复提示处理。",
        fragments=fragments,
    )


def is_translation_redundant(
    original: str,
    translated: str,
    target_lang: str = "",
    source_lang: str = "zh",
) -> bool:
    """
    判断译文是否无效（冗余或质量不合格），需拦截。

    This compatibility wrapper preserves the legacy strict behavior used by Excel.
    Use `validate_translation()` when structured issues or Word recovery behavior
    are needed.
    """
    if not str(translated or "").strip():
        return False
    return validate_translation(
        original,
        translated,
        target_lang=target_lang,
        source_lang=source_lang,
        profile=VALIDATION_PROFILE_STRICT,
    ).is_fail
