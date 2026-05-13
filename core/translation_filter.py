"""
翻译判定过滤器。
完整迁移原 GAS 宏的 shouldTranslate() 逻辑。
"""
import re
from collections import Counter

_CHINESE_CHAR_RE = re.compile(r"[\u4e00-\u9fa5]")
_NUMBER_TOKEN_RE = re.compile(r"\d+(?:[.,]\d+)?")


# ── 辅助函数 ──────────────────────────────────────────────────────────────────

def _check_numbers_intact(original: str, translated: str) -> bool:
    """
    数字完整性模糊校验：验证译文是否保留了原文的所有数值。

    允许：符号（* / × / x）或空格的变化（如 100×200 → 100*200）。
    不允许：任何数值缺失或频次减少（如 200*200 被截断为 200）。

    实现：提取整数/小数数值序列，使用 Counter 频次对比。
    """
    orig_nums = Counter(_normalize_number_token(token) for token in _NUMBER_TOKEN_RE.findall(original))
    if not orig_nums:
        return True  # 原文无数字，无需校验
    tran_nums = Counter(_normalize_number_token(token) for token in _NUMBER_TOKEN_RE.findall(translated))
    return all(tran_nums[num] >= count for num, count in orig_nums.items())


def _normalize_number_token(token: str) -> str:
    """Normalize dot/comma decimal variants for cross-language number checks."""
    return str(token or "").replace(",", ".")


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


def is_translation_redundant(
    original: str,
    translated: str,
    target_lang: str = "",
    source_lang: str = "zh",
) -> bool:
    """
    判断译文是否无效（冗余或质量不合格），需拦截。

    条件1 — 等值检测：译文剔除大小写/首尾空白后与原文相同。

    条件2 — 语向感知子串检测（防"只返回原文非中文部分"）：
      触发条件（全部满足）：
        a. 原文含中文字符
        b. 译文不含中文字符（说明目标语言中无中文，正常情况）
        c. 译文（去空白后归一化）是原文非中文部分（去空白后归一化）的子串
           —— 即：模型只提取了原文已有的英文/型号/符号部分，未做任何翻译
        d. 附加守卫（避免误杀）：中文占比 > 15% 或 长度损失 > 30%
           —— 确保原文确实有实质性中文内容被丢弃

    条件3 — 数字完整性检测（防"数值被截断/省略"）：
      触发条件：原文中的任意数值在译文中出现频次不足。
      使用 Counter 对比，允许连接符变化（* × x /），严禁数值缺失。

    :param original:    原始中文单元格内容
    :param translated:  模型返回的译文
    :param target_lang: 目标语言代码（如 'en'/'fr'/'ar' 等），用于语向感知（保留扩展入口）
    """
    orig = original.strip()
    tran = translated.strip()

    # 条件1：完全相同（大小写不敏感）
    if orig.lower() == tran.lower():
        return True

    if target_lang == "zh":
        # 目标语言为中文时，若返回内容完全不含中文且原文本身是非中文文本，
        # 基本可以判定模型没有真正完成翻译。
        if not _contains_chinese(tran) and _contains_non_chinese_letters(orig):
            return True

        if not _check_numbers_intact(orig, tran):
            return True

        return False

    # 仅对含中文的原文执行进一步检测
    if not _contains_chinese(orig):
        return False

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
                return True

    # 条件3：数字完整性
    if not _check_numbers_intact(orig, tran):
        return True

    return False
