"""
翻译判定过滤器。
完整迁移原 GAS 宏的 shouldTranslate() 逻辑。
"""
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

# 「数字 + 中文日期/数量单位」的判定模式只在 core/residual_classifier 维护一份；
# 残留中文的分级（阻断/可修/放行）、目标语豁免表、以及数字 token 的跨语言
# 归一也都由同一分类器给出，Word / Excel 共用。
from core.residual_classifier import (
    CN_DATE_UNIT_RE,
    NUMBER_TOKEN_PATTERN,
    NUMBER_TOKEN_RE,
    RESIDUAL_EXEMPT_TARGET_LANGS,
    NumberGroup,
    match_number_groups,
    number_group_readings,
    scale_number_key,
    summarize_residuals,
)

_CHINESE_CHAR_RE = re.compile(r"[\u4e00-\u9fa5]")
# 假名（平假名 / 片假名 / 半角片假名）与谚文：出现即证明这段文字不是中文，
# 与源语言选了什么无关。
# \u7247\u5047\u540d\u533a\u91cc\u7684\u4e2d\u70b9 \u30fb(U+30FB)\u3001\u957f\u97f3\u7b26 \u30fc(U+30FC) \u4e0e\u53cc\u8fde\u5b57\u7b26 \u30a0(U+30A0) \u7279\u610f
# \u6392\u9664\u5728\u5916\uff1a\u4e2d\u6587\u6b63\u6587\u4e5f\u7528\u5b83\u4eec\u5206\u9694\u5916\u56fd\u4eba\u540d\uff08\u76ae\u57c3\u5c14\u30fb\u5361\u5c14\u4e39\uff09\uff0c\u628a\u5b83\u4eec\u5f53\u6210\u300c\u8fd9\u6bb5
# \u4e0d\u662f\u4e2d\u6587\u300d\u7684\u8bc1\u636e\uff0c\u4f1a\u8ba9\u5916\u8bd1\u4e2d\u65f6\u5df2\u7ecf\u662f\u4e2d\u6587\u7684\u5355\u5143\u683c\u88ab\u91cd\u590d\u9001\u7ffb\u3002
_KANA_OR_HANGUL_RE = re.compile(
    r"[\u3041-\u309f\u30a1-\u30fa\u30fd-\u30ff"
    r"\uff66-\uff9f\uac00-\ud7a3\u1100-\u11ff]"
)
# 日文汉字、韩文汉字与中文汉字共用 \u4e00-\u9fa5 码区。源语言是日/韩时，
# 「含汉字」不等于「已经是中文」——「工事契約書」这类纯汉字标题是待译原文。
_HAN_SHARING_SOURCE_LANGS = frozenset({"ja", "ko"})
_SEMANTIC_NUMBER_RE = re.compile(r"[-+]?" + NUMBER_TOKEN_PATTERN + r"(?:\s*[万亿])?")
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
    # 该 token 的读法组：整体读法（1,500 在英/法两种写法下分别是 1500 与 1.5）
    # 加上空白分组的逐段读法（100 200 也可能是两个独立的数）。
    group: NumberGroup
    fragment: str
    weak: bool = False


# ── 辅助函数 ──────────────────────────────────────────────────────────────────

def _check_numbers_intact(original: str, translated: str) -> bool:
    """
    数字完整性模糊校验：验证译文是否保留了原文的所有数值。

    允许：符号（* / × / x）或空格的变化（如 100×200 → 100*200），以及各语言
    的千分位/数字体系写法差异（1500 与 1 500 / 1,500 / １５００ / ١٥٠٠ 同值）。
    不允许：任何数值缺失或频次减少（如 200*200 被截断为 200）。

    实现：提取数值序列后按「数值」一对一配对（core/residual_classifier）。
    """
    return not _missing_number_tokens(original, translated)


def _find_number_tokens(text: str) -> list[_NumberToken]:
    tokens: list[_NumberToken] = []
    source = str(text or "")
    for match in NUMBER_TOKEN_RE.finditer(source):
        token = match.group(0)
        weak = _is_weak_embedded_noise_number(source, match.start(), match.end())
        tokens.append(
            _NumberToken(
                token=token,
                group=number_group_readings(token),
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

    # 比对按「数值」而不是按「写法」：1500 与 1 500 / 1,500 / １５００ / ١٥٠٠
    # 是同一个数，配对成功即消耗（原文两个 3、译文只剩一个照样算丢）。空白分组
    # 的两种读法由 match_number_groups 兜住（1 500 是一个数，100 200 是两个）。
    missing_indexes = match_number_groups(
        [token.group for token in source_tokens],
        [token.group for token in _find_number_tokens(translated)],
    )
    return [source_tokens[index] for index in missing_indexes]


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


def _semantic_number_entries(
    text: str,
    *,
    skip_weak_source: bool = False,
) -> list[NumberGroup]:
    entries: list[NumberGroup] = []
    source = str(text or "")
    for match in _SEMANTIC_NUMBER_RE.finditer(source):
        raw = match.group(0)
        if skip_weak_source and _is_weak_embedded_noise_number(
            source,
            match.start(),
            _numeric_part_end(source, match.start(), match.end()),
        ):
            continue
        group = _semantic_number_group(raw)
        if group.whole or group.parts:
            entries.append(group)
    return entries


def _numeric_part_end(text: str, start: int, end: int) -> int:
    source = str(text or "")
    cursor = end
    while cursor > start and source[cursor - 1] in {"万", "亿"}:
        cursor -= 1
    while cursor > start and source[cursor - 1].isspace():
        cursor -= 1
    return cursor


def _scaled_number_keys(keys, factor: Decimal) -> frozenset:
    scaled: set[str] = set()
    for key in keys:
        try:
            scaled.add(scale_number_key(key, factor))
        except InvalidOperation:
            continue
    return frozenset(scaled)


def _semantic_number_group(raw: str) -> NumberGroup:
    """「3万」「-1 500,25」→ 该写法的读法组；解析不了返回空组。

    读法组而不是单一键集：空白分组的歧义（1 500 是一个数、100 200 是两个）
    与 _missing_number_tokens 那道闸门必须用同一套判据，否则又会出现「严格
    校验和分类器各有一套规则」——那正是本轮 高-2 的根因形状。
    """
    token = str(raw or "").strip()
    if not token:
        return NumberGroup()

    sign = Decimal(1)
    if token[0] in "+-":
        sign = Decimal(-1) if token[0] == "-" else Decimal(1)
        token = token[1:].strip()

    multiplier = Decimal(1)
    if token.endswith("万"):
        multiplier = Decimal(10000)
        token = token[:-1].strip()
    elif token.endswith("亿"):
        multiplier = Decimal(100000000)
        token = token[:-1].strip()

    group = number_group_readings(token)
    factor = multiplier * sign
    if factor == 1:
        return group
    # 倍率只作用于紧挨着 万/亿 的那一段：「100 200万」读作 100 与 200万，
    # 而不是 100万 与 200万。整体读法（100200万）则整体乘。
    parts = tuple(group.parts)
    if parts:
        parts = parts[:-1] + (_scaled_number_keys(parts[-1], factor),)
    return NumberGroup(
        token=group.token,
        whole=_scaled_number_keys(group.whole, factor),
        parts=parts,
    )


def _check_semantic_numbers_intact(
    original: str,
    translated: str,
    *,
    skip_weak_source: bool = False,
) -> bool:
    original_entries = _semantic_number_entries(
        original, skip_weak_source=skip_weak_source
    )
    if not original_entries:
        return True
    return not match_number_groups(
        original_entries,
        _semantic_number_entries(translated),
    )


def _contains_chinese(text: str) -> bool:
    return bool(_CHINESE_CHAR_RE.search(text))


def _normalize_lang(lang: str) -> str:
    """"ja-JP" / " JA " → "ja"；空值原样返回空串。"""
    cleaned = str(lang or "").strip().lower()
    if not cleaned:
        return ""
    return re.split(r"[-_]", cleaned, maxsplit=1)[0]


def _is_source_script_text(text: str, source_lang: str) -> bool:
    """目标语是中文时：这段文字属于「源语言自己的文字」，而不是已有的中文？

    一-龥 码区同时装着中文汉字、日文汉字和韩文汉字。旧判据「含汉字 → 已经是
    中文 → 跳过」在日译中/韩译中上是灾难：`工事契約書` 这类纯汉字的标题、
    表头、条款名不抽取、不翻译、也不进报告，整份文档只有假名句子被翻。

    判据因此看语言对而不是放宽正则：
      1. 假名 / 谚文是无歧义证据——含它们的文字一定不是中文，源语言选什么都算；
      2. 源语言明确选了日 / 韩时，含汉字的文字按待译原文处理。
    源语言是「自动」且整段全是汉字时仍无从分辨，保持旧行为（跳过），由语言
    预检把 auto 解析成具体语言后再走这里。
    """
    if _KANA_OR_HANGUL_RE.search(text):
        return True
    return (
        _normalize_lang(source_lang) in _HAN_SHARING_SOURCE_LANGS
        and _contains_chinese(text)
    )


def _contains_non_chinese_letters(text: str) -> bool:
    return any(char.isalpha() and not _contains_chinese(char) for char in text)


def _count_non_chinese_letters(text: str) -> int:
    return sum(1 for char in text if char.isalpha() and not _contains_chinese(char))


def _count_ascii_letters(text: str) -> int:
    return sum(1 for char in text if char.isascii() and char.isalpha())


# ── 主函数 ────────────────────────────────────────────────────────────────────

def should_translate(
    text: str,
    target_lang: str = "",
    source_lang: str = "zh",
) -> bool:
    """
    判断单元格文本是否需要翻译。

    源语言与目标语言共同决定判据：日译中 / 韩译中时「含汉字」不等于「已经是
    中文」，详见 _is_source_script_text。

    规则优先级（从高到低）：
      1. 空字符串  → 跳过
      2. 含中文字符 → 翻译
      3. 纯数字/符号/空白 → 跳过
      4. 无空格且字母数字混合（型号代码，如 "A3B12"） → 跳过
      5. 含空格（词组或短句） → 翻译
      6. 纯字母的单词 → 翻译（拉丁字母要长度 > 3，非拉丁文字见规则内注释）
      7. 其余 → 跳过
    """
    text = text.strip()

    # 规则 1：空字符串
    if not text:
        return False

    if target_lang == "zh" and not _is_source_script_text(text, source_lang):
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

    # 规则 2：含中文字符（日译中/韩译中时，这里的「汉字」是源语言的汉字，
    # 同样要翻——判据见 _is_source_script_text）
    if _contains_chinese(text):
        return True

    # 规则 3：纯数字、符号、空白
    if re.match(r'^[\d\s\W_]+$', text):
        return False

    # 规则 4：无空格且字母数字混合（型号代码）
    if ' ' not in text and _contains_non_chinese_letters(text) and re.search(r'\d', text):
        return False

    # 规则 5：含空格（词组/短句）
    if ' ' in text:
        return True

    # 规则 6：单个词。
    #
    # 「长度 > 3」这条是当年为中文文档里夹的英文缩写定的——DN、PE、Ltd 这类不该翻。
    # 它只对拉丁字母成立，套到别的文字上会把整份文档判成「没有要翻的内容」：泰语、
    # 高棉语、老挝语、缅甸语词与词之间不打空格，一整句话走到这里就是「一个词」，
    # 正则 ^[A-Za-z]+$ 一律不匹配；希腊语、西里尔字母、假名、谚文的单词同样落空；
    # 连德语的 Straße、法语的 Généralités 都因为一个变音字符被判成不用翻。
    #
    # 判据改成：含非 ASCII 字母时按字母数放行——字母够多（> 3）就是实词；一个
    # ASCII 字母都不含的（谚文、假名、泰文……）短到 2 个字也是实词。仍然含 ASCII
    # 字母的短串（μm、Nº）继续按缩写处理，避免把单位符号送去翻译。
    letter_count = _count_non_chinese_letters(text)
    ascii_letters = _count_ascii_letters(text)
    if (
        letter_count >= 2
        and letter_count > ascii_letters
        and (letter_count > 3 or ascii_letters == 0)
    ):
        return True

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

    # 目标语言既不是中文也不是日文时，译文本身不应残留「数字 + 中文日期/数量
    # 单位」写法（如 "2026年8月9日"、"18周岁"）；只查译文，不查原文。
    # 豁免表与 residual_classifier 共用一份：日文本来就写「2026年8月9日」，
    # 在这里判 fail 会让 engine_dispatcher 把正确的日文译文重置回中文原文。
    if _normalize_lang(target_lang) not in RESIDUAL_EXEMPT_TARGET_LANGS:
        cn_date_unit_issue = _residual_cn_date_unit_issue(tran)
        if cn_date_unit_issue is not None:
            issues.append(cn_date_unit_issue)

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
        "residual_cn_date_unit",
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

    # 残留中文分级交给共享分类器（与 Excel、写盘前残留巡检同一套标准）：
    # 阻断级（日期单位/整句未译）不放行；序号前缀、短语残留、数量单位以
    # 复核提示放行——它们随后由确定性修复与修复阶梯处理，比在这里一票
    # 否决多一条自动修复通道。
    residual = summarize_residuals(translated, target_lang=target_lang)
    if residual.spans:
        fragments = _unique_fragments(span.text for span in residual.spans)
        if residual.blocking:
            issues.append(
                TranslationValidationIssue(
                    code="residual_chinese_blocking",
                    message="译文仍残留未翻译的中文，疑似未完整翻译。",
                    fragments=fragments,
                )
            )
            return _validation_result_from_issues(issues)
        issues.append(
            TranslationValidationIssue(
                code="residual_chinese_light",
                message="译文残留少量中文，已作为 Word 恢复提示处理。",
                fragments=fragments,
            )
        )

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


def _residual_cn_date_unit_issue(
    translated: str,
) -> TranslationValidationIssue | None:
    """
    检测译文中是否残留「数字 + 中文日期/数量单位」组合，例如日译文中夹带的
    "2026年8月9日"、"18周岁"、"20000元"。编号（BTR-ANODE-CCTEB-032、1#）与国际
    单位（38℃、m²）本身不含中文字符，不会被 CN_DATE_UNIT_RE 命中。
    """
    matches = _unique_fragments(CN_DATE_UNIT_RE.findall(str(translated or "")))
    if not matches:
        return None
    return TranslationValidationIssue(
        code="residual_cn_date_unit",
        message="译文中残留中文日期/数量单位：" + "、".join(matches),
        fragments=matches,
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
