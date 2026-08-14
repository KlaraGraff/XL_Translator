# -*- coding: utf-8 -*-
"""
残留中文分类与确定性修复（Word / Excel 两条流水线共用）。

设计背景见 docs/redesign/2026-08-14_residual_repair_pipeline.md。
核心立场：残留中文是「类的问题」而不是「量的问题」——1~3 字的序号残留
和 4 字以上的成句未译需要完全不同的处置，量阈值无法区分它们。

本模块只依赖标准库，不 import 任何流水线模块（translation_filter、
task_runner、word_task_runner 反向依赖本模块），保证两条流水线共享
同一份判定逻辑。
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

# 与 translation_filter 既有检测保持同一 CJK 范围（一-龥）。
CJK_SPAN_RE = re.compile(r"[一-龥]+")

# 「数字 + 中文日期/数量单位」的权威定义。translation_filter 的
# _residual_cn_date_unit_issue 使用同一模式（单一来源，勿另抄一份）。
CN_DATE_UNIT_RE = re.compile(r"\d+\s*(?:周岁|万元|岁|元|年|月|日|时|分)")

# 残留类别（详见设计文档 §3.1）
CATEGORY_NUMBERING_PREFIX = "numbering_prefix"  # 段首结构序号：（四）、3、第1节 → 确定性修复
CATEGORY_CN_DATE_UNIT = "cn_date_unit"          # 数字+中文单位：2026年 → 阻断重译
CATEGORY_QUANTITY_UNIT = "quantity_unit"        # 万/亿 → 放行 + 记录
CATEGORY_TERM_FRAGMENT = "term_fragment"        # ≤3 字、嵌在目标语成句中 → 外科修补
CATEGORY_SENTENCE_BLOCK = "sentence_block"      # 成句未译 → 带反馈重译

# 目标语为中文/日文时不存在「残留中文」问题（与 _light_residual_chinese_issue 一致）
RESIDUAL_EXEMPT_TARGET_LANGS = frozenset({"zh", "ja"})

_CN_NUM_CHARS = "零一二三四五六七八九十"
_CN_DIGIT_VALUE = {c: i for i, c in enumerate("零一二三四五六七八九")}

# 段首结构序号：（四）/ (四) / 四、 / 四. / 第X节（X 可为汉字或数字，节可省略）。
# 「第X」两支的 (?![一-龥]) 是硬约束：前缀边界后紧跟汉字时（第三|方、第五节|点、
# 第二部|分）它不是序号而是词语的前半截——按序号切割会把正文砍掉半截。
NUMBERING_PREFIX_RE = re.compile(
    r"^\s*(?:"
    r"[（(]\s*[%(cn)s]{1,3}\s*[）)]"
    r"|[%(cn)s]{1,3}\s*[、\.．]"
    r"|第\s*[%(cn)s0-9０-９]{1,3}\s*(?:[节章条款项]|部分)(?![一-龥])"
    r"|第\s*[%(cn)s0-9０-９]{1,3}(?![一-龥])"
    r")" % {"cn": _CN_NUM_CHARS}
)

_PAREN_CN_PREFIX_RE = re.compile(r"^(\s*)[（(]\s*([%s]{1,3})\s*[）)]" % _CN_NUM_CHARS)
_SECTION_CN_PREFIX_RE = re.compile(
    r"^(\s*)第\s*([%s0-9０-９]{1,3})\s*([节章条款项])(?![一-龥])" % _CN_NUM_CHARS
)

# 源锚定替换后残留的枚举分隔符（「第一、」只匹配到「第一」时留下的顿号）。
# ASCII 句点后跟数字时是小数/多级编号的一部分，不能吃。
_DANGLING_ENUM_SEPARATOR_RE = re.compile(r"^\s*(?:[、，]|[\.．](?!\d))\s*")

# 目标语单词（用于判断残留是否「嵌在成句译文中」）：拉丁（含扩展）/西里尔/希腊
_TARGET_WORD_RE = re.compile(r"[A-Za-zÀ-ɏͰ-ϿЀ-ӿ]{2,}")

_ROMAN_NUMERALS = [
    "I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X",
    "XI", "XII", "XIII", "XIV", "XV", "XVI", "XVII", "XVIII", "XIX", "XX",
]

# 同级序号惯例族：探测既有译文使用哪一族，供确定性修复对齐
CONVENTION_PAREN_ROMAN = "paren_roman"
CONVENTION_PAREN_ARABIC = "paren_arabic"
CONVENTION_ARABIC_DOT = "arabic_dot"
_CONVENTION_PATTERNS = (
    (CONVENTION_PAREN_ROMAN, re.compile(r"^\s*\(\s*([IVX]{1,5})\s*\)")),
    (CONVENTION_PAREN_ARABIC, re.compile(r"^\s*\(\s*(\d{1,2})\s*\)")),
    (CONVENTION_ARABIC_DOT, re.compile(r"^\s*(\d{1,2})[\.．]\s")),
)

# 源段以阿拉伯数字编号开头（5.5.3 / 3.1施工 / 7、）：残留序号可直接从源段还原。
# 实测依据：交付文档里出现过源段「5.5.3 …」被译成「三、…」——模型把编号本身
# 改掉了，此时按同级惯例翻译「三」反而是错的，唯一正确的修复是抄回源编号。
_SOURCE_ARABIC_PREFIX_RE = re.compile(r"^\s*(\d+(?:[\.．]\d+)*)([、\.．])?")
# 数字后随这些字符时它是年份/数量而不是编号（2026年、3.5米），不作源锚点
_NON_NUMBERING_UNIT_CHARS = frozenset("年月日时分秒岁元米厘毫吨千克kmKM%万亿")
_FW_DIGIT_DOT_TRANS = str.maketrans("０１２３４５６７８９．", "0123456789.")


def _source_arabic_prefix_label(source_text: str) -> str | None:
    """源段的阿拉伯编号前缀 → 还原用标签；证据不足返回 None。

    单级数字必须带显式分隔符（7、/ 3.）才算编号；多级（5.5.3 / 3.1）
    后随空白或非单位汉字即可。这样年份（2026年）、数量（3.5米、7 台）
    都不会被误当成编号锚点。
    """
    text = str(source_text or "")
    match = _SOURCE_ARABIC_PREFIX_RE.match(text)
    if not match or not match.group(1):
        return None
    token = match.group(1).translate(_FW_DIGIT_DOT_TRANS)
    if match.group(2):
        return f"{token}."
    follower = text[match.end(): match.end() + 1]
    if follower and not follower.isspace():
        if not CJK_SPAN_RE.match(follower) or follower in _NON_NUMBERING_UNIT_CHARS:
            return None
    if "." not in token:
        return None
    return token

# 「第X节/章」的目标语写法（仅收录可确定性替换的语言；未收录语言只报告不代改）
_SECTION_WORD_BY_LANG = {
    "fr": {"节": "Section", "章": "Chapitre", "条": "Article"},
    "en": {"节": "Section", "章": "Chapter", "条": "Article"},
}

# 节标题写法族（文档级一致性巡检用）
HEADING_FORM_SECTION_N = "section_n"        # Section 2 / Chapitre 2
HEADING_FORM_ORDINAL_WORD = "ordinal_word"  # Deuxième section
_HEADING_SECTION_N_RE = re.compile(
    r"^\s*(Section|Chapitre|Chapter|Article)\s+(\d{1,3})\b", re.IGNORECASE
)
_HEADING_ORDINAL_RE = re.compile(
    r"^\s*([A-Za-zÀ-ɏ]+)\s+(section|chapitre|chapter)\b[\s—–\-]*",
    re.IGNORECASE,
)
_FR_ORDINAL_VALUE = {
    "première": 1, "premier": 1, "deuxième": 2, "seconde": 2, "second": 2,
    "troisième": 3, "quatrième": 4, "cinquième": 5, "sixième": 6,
    "septième": 7, "huitième": 8, "neuvième": 9, "dixième": 10,
    "onzième": 11, "douzième": 12, "treizième": 13, "quatorzième": 14,
    "quinzième": 15, "seizième": 16, "dix-septième": 17, "dix-huitième": 18,
    "dix-neuvième": 19, "vingtième": 20,
    # "second" 与法语拼写相同，法语块里已收录
    "first": 1, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
}


@dataclass(frozen=True)
class ResidualSpan:
    """译文中一个残留中文片段的分类结果。"""

    text: str      # 残留文本（numbering_prefix 为整个序号前缀，其余为 CJK 连续片段）
    start: int     # 在译文中的字符偏移
    category: str  # CATEGORY_* 之一


@dataclass(frozen=True)
class ResidualSummary:
    """一段译文的残留总评，供验收层直接使用。"""

    spans: tuple[ResidualSpan, ...] = ()
    # 必须阻断（cn_date_unit / sentence_block）：不允许带着这些残留放行
    blocking: bool = False
    # 存在可自动修复或需外科修补的残留（numbering_prefix / term_fragment）
    repairable: bool = False
    # 仅剩可放行残留（quantity_unit），或完全干净
    releasable: bool = True
    categories: tuple[str, ...] = ()


def parse_cn_numeral(text: str) -> int | None:
    """汉字数字 → 整数（支持 一~九十九；也接受阿拉伯/全角数字）。"""
    cleaned = str(text or "").strip()
    if not cleaned:
        return None
    normalized = cleaned.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    if normalized.isdigit():
        value = int(normalized)
        return value if value > 0 else None
    if not set(cleaned) <= set(_CN_NUM_CHARS):
        return None
    if "十" not in cleaned:
        if len(cleaned) == 1:
            value = _CN_DIGIT_VALUE.get(cleaned)
            return value if value else None
        return None
    tens_part, _, units_part = cleaned.partition("十")
    tens = _CN_DIGIT_VALUE.get(tens_part, None) if tens_part else 1
    units = _CN_DIGIT_VALUE.get(units_part, None) if units_part else 0
    if tens is None or units is None or tens == 0:
        return None
    return tens * 10 + units


def classify_residual_spans(
    target_text: str,
    *,
    target_lang: str = "",
) -> list[ResidualSpan]:
    """
    对译文中的每个残留中文片段按「它是什么」分类。

    干净译文返回空列表。同一序号前缀内的多个 CJK 片段合并为一条
    numbering_prefix（如 `第1节` 的「第」「节」两个片段）。
    """
    text = str(target_text or "")
    if not text or target_lang in RESIDUAL_EXEMPT_TARGET_LANGS:
        return []
    cjk_matches = list(CJK_SPAN_RE.finditer(text))
    if not cjk_matches:
        return []

    prefix_match = NUMBERING_PREFIX_RE.match(text)
    date_unit_ranges = [m.span() for m in CN_DATE_UNIT_RE.finditer(text)]
    target_word_count = len(_TARGET_WORD_RE.findall(text))

    spans: list[ResidualSpan] = []
    prefix_reported = False
    for match in cjk_matches:
        fragment, start = match.group(), match.start()
        if prefix_match and start < prefix_match.end():
            if prefix_reported:
                continue
            prefix_reported = True
            spans.append(
                ResidualSpan(
                    text=text[prefix_match.start():prefix_match.end()].strip(),
                    start=prefix_match.start(),
                    category=CATEGORY_NUMBERING_PREFIX,
                )
            )
            continue
        if any(s <= start and match.end() <= e for s, e in date_unit_ranges):
            # 仅当整个片段都落在「数字+单位」匹配内（2026年 的「年」）才算
            # 日期单位；片段只是与之沾边的长串（30日历天内完成全部工作）
            # 按长度正常分类，否则整句未译会被日期标签劫持而失去修复通道。
            category = CATEGORY_CN_DATE_UNIT
        elif set(fragment) <= {"万", "亿"}:
            category = CATEGORY_QUANTITY_UNIT
        elif len(fragment) <= 3 and target_word_count >= 3:
            category = CATEGORY_TERM_FRAGMENT
        else:
            category = CATEGORY_SENTENCE_BLOCK
        spans.append(ResidualSpan(text=fragment, start=start, category=category))
    return spans


def summarize_residuals(
    target_text: str,
    *,
    target_lang: str = "",
) -> ResidualSummary:
    """分类 + 汇总成验收层可直接消费的总评。"""
    spans = tuple(classify_residual_spans(target_text, target_lang=target_lang))
    categories = tuple(sorted({span.category for span in spans}))
    blocking = any(
        span.category in (CATEGORY_CN_DATE_UNIT, CATEGORY_SENTENCE_BLOCK)
        for span in spans
    )
    repairable = any(
        span.category in (CATEGORY_NUMBERING_PREFIX, CATEGORY_TERM_FRAGMENT)
        for span in spans
    )
    releasable = not blocking and not repairable
    return ResidualSummary(
        spans=spans,
        blocking=blocking,
        repairable=repairable,
        releasable=releasable,
        categories=categories,
    )


# ---------------------------------------------------------------------------
# 同级序号惯例探测 + 确定性修复
# ---------------------------------------------------------------------------

def detect_sibling_convention(
    pairs,
    *,
    default: str = CONVENTION_PAREN_ARABIC,
) -> str:
    """
    从「源段以（X）系序号开头」的同族段落译文里投票，得出本文档的序号惯例。

    只统计同族源段，避免被正文步骤编号（1. 2. 3.）污染——这是 PoC 阶段
    实测踩过的坑。pairs 为 (source_text, target_text) 可迭代对象。
    """
    votes: dict[str, int] = {}
    for source_text, target_text in pairs:
        if not _PAREN_CN_PREFIX_RE.match(str(source_text or "")):
            continue
        for family, pattern in _CONVENTION_PATTERNS:
            if pattern.match(str(target_text or "")):
                votes[family] = votes.get(family, 0) + 1
                break
    if not votes:
        return default
    return max(votes, key=lambda family: votes[family])


_ROMAN_TO_VALUE = {
    numeral: index for index, numeral in enumerate(_ROMAN_NUMERALS, start=1)
}


def align_enum_prefix_to_convention(
    source_text: str,
    target_text: str,
    *,
    convention: str,
) -> str | None:
    """
    重译稿的序号前缀按文档惯例对齐（0 API）。

    带反馈重译的验收只看「无残留、数字不丢」，不管前缀写法——源段
    「（三）…」的重译稿开头写成「3.」也会通过，而全篇惯例可能是「(III)」。
    这里做确定性收口：源段以（X）系中文序号开头、重译稿以另一族目标语
    序号开头、且数值与源序号一致时，把前缀改写成惯例族写法。
    数值对不上（模型改了编号）、惯例未知、或重译稿本来就是惯例写法，
    一律返回 None——绝不猜，绝不多改。
    """
    source_match = _PAREN_CN_PREFIX_RE.match(str(source_text or ""))
    if not source_match:
        return None
    value = parse_cn_numeral(source_match.group(2))
    if value is None:
        return None
    label = format_enum_label(value, convention)
    if label is None:
        return None
    text = str(target_text or "")
    for family, pattern in _CONVENTION_PATTERNS:
        match = pattern.match(text)
        if match is None:
            continue
        if family == convention:
            return None
        token = match.group(1)
        if family == CONVENTION_PAREN_ROMAN:
            candidate_value = _ROMAN_TO_VALUE.get(token)
        else:
            candidate_value = int(token)
        if candidate_value != value:
            return None
        leading = text[: len(text) - len(text.lstrip())]
        rest = text[match.end():]
        separator = "" if (not rest or rest.startswith((" ", "\t"))) else " "
        return leading + label + separator + rest
    return None


def format_enum_label(value: int, convention: str) -> str | None:
    """按惯例族渲染序号：4 + paren_roman → \"(IV)\"。"""
    if value < 1:
        return None
    if convention == CONVENTION_PAREN_ROMAN:
        if value > len(_ROMAN_NUMERALS):
            return None
        return "(%s)" % _ROMAN_NUMERALS[value - 1]
    if convention == CONVENTION_PAREN_ARABIC:
        return "(%d)" % value
    if convention == CONVENTION_ARABIC_DOT:
        return "%d." % value
    return None


def deterministic_numbering_fix(
    target_text: str,
    *,
    convention: str,
    target_lang: str = "",
    source_text: str = "",
) -> str | None:
    """
    对 numbering_prefix 残留做零 API 的确定性替换。

    只改前缀，正文零接触。修复来源按可信度排序：
      1. 源段锚定：源段本身以阿拉伯数字编号开头（5.5.3 / 3.1）→ 抄回源编号。
         优先级最高——它不依赖任何推断，且覆盖「模型把编号改掉」的失败形态。
      2. 同级惯例：（X）族按 convention 投票结果替换。
      3. 第X节/章：按目标语词表替换（仅收录语言）。
    改不了（未知惯例、数字解析失败、未收录语言的第X节、裸「X、」无源锚点）
    就返回 None，让上层走外科修补或人工复核，绝不猜。
    """
    text = str(target_text or "")
    prefix = NUMBERING_PREFIX_RE.match(text)
    if prefix:
        label = _source_arabic_prefix_label(source_text)
        if label is not None:
            # 「第一、」只匹配到「第一」时会留下悬空顿号，一并吃掉
            rest = _DANGLING_ENUM_SEPARATOR_RE.sub("", text[prefix.end():], count=1)
            separator = "" if (not rest or rest.startswith((" ", "\t"))) else " "
            return (text[: prefix.start()] + label + separator + rest).rstrip()

    paren = _PAREN_CN_PREFIX_RE.match(text)
    if paren:
        value = parse_cn_numeral(paren.group(2))
        if value is None:
            return None
        label = format_enum_label(value, convention)
        if label is None:
            return None
        rest = text[paren.end():]
        # 全角括号在中文排版里不带后随空格，替换成拉丁序号后需要补一个
        separator = "" if (not rest or rest.startswith((" ", "\t"))) else " "
        return text[: paren.start()] + paren.group(1) + label + separator + rest

    section = _SECTION_CN_PREFIX_RE.match(text)
    if section:
        words = _SECTION_WORD_BY_LANG.get((target_lang or "").lower())
        if not words:
            return None
        word = words.get(section.group(3))
        if word is None:
            return None
        value = parse_cn_numeral(section.group(2))
        if value is None:
            return None
        rest = text[section.end():]
        separator = "" if rest.startswith((" ", "\t")) else " "
        return text[: section.start()] + section.group(1) + f"{word} {value}{separator}" + rest
    return None


# ---------------------------------------------------------------------------
# 外科修补验收器（diff 受限）
# ---------------------------------------------------------------------------

_NUMBER_TOKEN_RE = re.compile(r"\d+(?:[\.,]\d+)?")

# 修补稿相对原译文的净增长上限：残留片段每字符 6 字符 + 12。中文术语译成
# 法/英文的展开率通常在 6 倍以内；超过即视为模型在窗口内塞入自造内容。
_SURGICAL_GROWTH_PER_SPAN_CHAR = 6
_SURGICAL_GROWTH_BASE = 12


def extract_number_tokens(text: str) -> list[str]:
    """按出现顺序提取数字 token（全角归一为半角，小数逗号归一为点）。"""
    normalized = str(text or "").translate(_FW_DIGIT_DOT_TRANS)
    return [token.replace(",", ".") for token in _NUMBER_TOKEN_RE.findall(normalized)]


def surgical_repair_ok(
    original: str,
    repaired: str,
    spans,
    *,
    window: int = 12,
    target_lang: str = "",
) -> tuple[bool, str]:
    """
    机器验收外科修补稿：修补只允许发生在残留片段 ±window 字符内。

    spans: [(start, length), ...] —— 原译文中被允许修补的残留片段位置。
    四条硬规则（设计文档 §3.3）：
      1. 阻断级/可修复级残留必须已消除（复跑分类器，仅容忍 quantity_unit）；
      2. 所有编辑操作都落在允许窗口内（SequenceMatcher opcodes 逐一检查），
         且每处编辑与全文的净增长都有上限——窗口只约束原文侧偏移，不设
         增长上限的话一次窗口内 replace 就能塞进任意长度的自造内容；
      3. 数字序列完全不变（含顺序：窗口内的数字互换也必须拒收）。
    不满足任何一条即拒收，理由随返回值给出。
    """
    original = str(original or "")
    repaired = str(repaired or "")
    if not repaired.strip():
        return False, "repaired text is empty"

    leftover = classify_residual_spans(repaired, target_lang=target_lang)
    bad = [s for s in leftover if s.category != CATEGORY_QUANTITY_UNIT]
    if bad:
        return False, "repaired text still has blocking CJK: " + "、".join(
            s.text for s in bad
        )

    windows = [
        (max(0, int(start) - window), int(start) + int(length) + window)
        for start, length in spans
    ]
    if not windows:
        return False, "no repair spans given"
    span_char_total = sum(max(1, int(length)) for _start, length in spans)
    max_growth = (
        _SURGICAL_GROWTH_PER_SPAN_CHAR * span_char_total + _SURGICAL_GROWTH_BASE
    )
    if len(repaired) - len(original) > max_growth:
        return False, (
            f"repaired text grew too much: +{len(repaired) - len(original)} chars "
            f"(limit {max_growth})"
        )
    matcher = difflib.SequenceMatcher(None, original, repaired, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if not any(lo <= i1 and i2 <= hi for lo, hi in windows):
            return False, (
                f"edit outside allowed window: {tag} original[{i1}:{i2}]="
                f"{original[i1:i2]!r}"
            )
        if (j2 - j1) - (i2 - i1) > max_growth:
            return False, (
                f"edit inserts too much text: {tag} +{(j2 - j1) - (i2 - i1)} chars "
                f"(limit {max_growth})"
            )

    numbers_original = extract_number_tokens(original)
    numbers_repaired = extract_number_tokens(repaired)
    if numbers_original != numbers_repaired:
        return False, f"numbers changed: {numbers_original} -> {numbers_repaired}"
    return True, "ok"


# ---------------------------------------------------------------------------
# 文档级标题一致性
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HeadingObservation:
    """一条节标题的译文写法观测。"""

    source_text: str
    target_text: str
    unit_key: str            # 由调用方决定（段索引 / 单元格地址 / unit_id）
    form: str | None = None  # HEADING_FORM_* 或 None（无法识别）
    value: int | None = None # 序号数值


@dataclass(frozen=True)
class HeadingConsistencyResult:
    majority_form: str | None = None
    observations: tuple[HeadingObservation, ...] = ()
    outliers: tuple[HeadingObservation, ...] = ()
    fixes: dict = field(default_factory=dict)  # unit_key -> 修复后的完整译文


_SOURCE_HEADING_RE = re.compile(
    r"^\s*第\s*[%s0-9０-９]{1,3}\s*[节章]" % _CN_NUM_CHARS
)


def is_section_heading_source(source_text: str) -> bool:
    return bool(_SOURCE_HEADING_RE.match(str(source_text or "")))


def _observe_heading(source_text: str, target_text: str, unit_key: str) -> HeadingObservation:
    text = str(target_text or "")
    match = _HEADING_SECTION_N_RE.match(text)
    if match:
        return HeadingObservation(
            source_text=source_text, target_text=text, unit_key=unit_key,
            form=HEADING_FORM_SECTION_N, value=int(match.group(2)),
        )
    match = _HEADING_ORDINAL_RE.match(text)
    if match:
        value = _FR_ORDINAL_VALUE.get(match.group(1).lower())
        if value is not None:
            return HeadingObservation(
                source_text=source_text, target_text=text, unit_key=unit_key,
                form=HEADING_FORM_ORDINAL_WORD, value=value,
            )
    return HeadingObservation(
        source_text=source_text, target_text=text, unit_key=unit_key,
    )


def check_heading_consistency(
    observations, *, target_lang: str = "", majority_form: str | None = None
) -> HeadingConsistencyResult:
    """
    对全篇「第X节/章」译文做写法聚类：多数派为准，离群者生成仅前缀重写稿。

    observations: (source_text, target_text, unit_key) 可迭代对象，调用方
    只送入 is_section_heading_source 为真的单元。逐段看每条都可能是正确
    译文，只有全文档聚在一起才看得出两套写法——所以这是独立的落盘后阶段。

    majority_form：调用方已在更大范围（全篇）投出的多数派。子集（如 TM
    入库配对）自行投票可能与全篇结论相反，把库改成与交付文件相左的写法；
    传入后跳过本地投票，直接以它为准。
    """
    observed = [
        _observe_heading(source_text, target_text, unit_key)
        for source_text, target_text, unit_key in observations
    ]
    votes: dict[str, int] = {}
    for item in observed:
        if item.form:
            votes[item.form] = votes.get(item.form, 0) + 1
    majority = majority_form or (max(votes, key=lambda form: votes[form]) if votes else None)
    if not majority:
        return HeadingConsistencyResult(observations=tuple(observed))
    outliers = tuple(
        item for item in observed if item.form and item.form != majority
    )
    fixes: dict = {}
    for item in outliers:
        fixed = _rewrite_heading_prefix(item, majority, target_lang=target_lang)
        if fixed is not None:
            fixes[item.unit_key] = fixed
    return HeadingConsistencyResult(
        majority_form=majority,
        observations=tuple(observed),
        outliers=outliers,
        fixes=fixes,
    )


def _rewrite_heading_prefix(
    observation: HeadingObservation,
    majority_form: str,
    *,
    target_lang: str = "",
) -> str | None:
    """离群标题 → 多数派写法，只动前缀。当前仅支持归一到 Section N 系。"""
    if majority_form != HEADING_FORM_SECTION_N or observation.value is None:
        # 归一到序数词写法需要词形变化知识，不做确定性改写，只报告。
        return None
    text = observation.target_text
    match = _HEADING_ORDINAL_RE.match(text)
    if not match:
        return None
    section_word = match.group(2)
    section_word = section_word[:1].upper() + section_word[1:].lower()
    rest = text[match.end():].lstrip(" —–-")
    return f"{section_word} {observation.value} {rest}".rstrip()
