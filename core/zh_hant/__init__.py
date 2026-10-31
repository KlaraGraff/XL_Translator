"""繁體中文：语言码、判断、以及把译文里漏出来的简体字改成繁体。

内置目标语言「繁體中文」的代码是 ``zh_hant``。用下划线而不是 ``zh-Hant``：记忆库的
语言对写成 ``源-目标``（``en-zh_hant``），按第一个连字符拆开，语言码里不能再有连字符。
按 ``[-_]`` 取主语言的地方（``zh_hant`` → ``zh``）都自然把它当中文。

模型偶尔在繁体译文里夹几个简体字（「這個软件」）。``to_traditional`` 只改「只有简体才
用的字」：夹了简体字的那一段，先按 OpenCC 的词组表整词换（「头发」→「頭髮」「干燥」→
「乾燥」，避免一字多义换错），再逐字换简体专用字；两岸通用的字（「台、后、干、里」等）
不在词里时原样不动。没有简体字的译文一个字都不改。
词表来自 OpenCC（Apache-2.0），见 THIRD_PARTY_NOTICES.md。
"""

from __future__ import annotations

import gzip
import re
from functools import lru_cache
from pathlib import Path

TRADITIONAL_CHINESE = "zh_hant"
SIMPLIFIED_CHINESE = "zh"

# 已经写成繁体的说法都认（API 调用方、扩展可能传 zh-TW / zh-Hant）。
_TRADITIONAL_CODES = frozenset({
    "zh_hant", "zh-hant", "zh_tw", "zh-tw", "zh_hk", "zh-hk", "zh_mo", "zh-mo",
    "zh-hant-tw", "zh_hant_tw", "zh-hant-hk", "zh_hant_hk",
})

_DATA_FILE = Path(__file__).with_name("opencc_s2twp.txt.gz")

# OpenCC 的简→繁字表里，这些字在繁体文本里本来就常用（「皇后」「干涉」「公里」「台灣」
# 「准許」「茶几」……），不能当成简体字。其余字表里的字繁体文本都不该出现。
_SHARED_CHARACTERS = frozenset(
    "丑么于云仆余佣凄准几凶划占台吁吃后咸唇岩岳峰干床征恒斗朴杰栖涂灶炖熏瓮痴皂秘粽群肴腌霉雇辟里范采郁游伙"
    "苧沄蒏蔂跖蚝蝎虱"
)

_HAN_RE = re.compile(r"[㐀-鿿豈-﫿]")


def lang_base(code: object) -> str:
    """``"zh_hant"`` / ``"zh-TW"`` / ``" ZH "`` → ``"zh"``。"""
    return re.split(r"[-_]", str(code or "").strip().lower(), maxsplit=1)[0]


def is_chinese_lang(code: object) -> bool:
    """简体、繁体都算中文。"""
    return lang_base(code) == "zh"


def is_traditional_chinese(code: object) -> bool:
    return str(code or "").strip().lower() in _TRADITIONAL_CODES


@lru_cache(maxsize=1)
def _tables() -> tuple[dict[str, str], dict[str, str], dict[str, str], int]:
    """(词组表, 单字表, 台湾异体字表, 最长词组长度)。只在第一次用到时读文件。"""
    sections: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    with gzip.open(_DATA_FILE, "rt", encoding="utf-8") as handle:
        for line in handle:
            line = line.rstrip("\n")
            if not line:
                continue
            if line.startswith("#"):
                current = sections.setdefault(line[1:], {})
                continue
            key, _, value = line.partition(" ")
            if current is not None and key and value:
                current[key] = value.split(" ", 1)[0]
    phrases = {**sections.get("STPhrases", {}), **sections.get("STPhrases_GeneratedFromRegionalPhrases", {})}
    characters = sections.get("STCharacters", {})
    variants = {**sections.get("TWVariants", {})}
    longest = max((len(key) for key in phrases), default=1)
    return phrases, characters, variants, longest


@lru_cache(maxsize=1)
def _simplified_only() -> frozenset[str]:
    _phrases, characters, _variants, _longest = _tables()
    return frozenset(ch for ch in characters if ch not in _SHARED_CHARACTERS)


def simplified_characters(text: object) -> list[str]:
    """文本里只有简体才用的字（按出现顺序去重）。"""
    value = str(text or "")
    if not value or not _HAN_RE.search(value):
        return []
    simplified = _simplified_only()
    seen: list[str] = []
    for ch in value:
        if ch in simplified and ch not in seen:
            seen.append(ch)
    return seen


def has_simplified_characters(text: object) -> bool:
    value = str(text or "")
    if not value or not _HAN_RE.search(value):
        return False
    simplified = _simplified_only()
    return any(ch in simplified for ch in value)


def is_traditional_text(text: object) -> bool:
    """有汉字、且没有简体专用字：可以当作已经是繁體中文。"""
    value = str(text or "")
    return bool(_HAN_RE.search(value)) and not has_simplified_characters(value)


def to_traditional(text: str) -> str:
    """只把简体专用字换成繁体（台湾用字），其余字一律不动。"""
    if not text or not has_simplified_characters(text):
        return text
    phrases, characters, variants, longest = _tables()
    simplified = _simplified_only()
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if not _HAN_RE.match(ch):
            out.append(ch)
            i += 1
            continue
        matched = False
        for size in range(min(longest, n - i), 1, -1):
            piece = text[i:i + size]
            target = phrases.get(piece)
            # 这一段已经确认夹了简体字，整词表里的词都换（「干燥」→「乾燥」）。
            if target is not None:
                out.append("".join(variants.get(c, c) for c in target))
                i += size
                matched = True
                break
        if matched:
            continue
        if ch in simplified:
            converted = characters.get(ch, ch)
            out.append(variants.get(converted, converted))
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def fix_translation(text: object, target_lang: object) -> object:
    """目标语言是繁体时，把译文里漏出的简体字改掉；其他情况原样返回。"""
    if isinstance(text, str) and is_traditional_chinese(target_lang):
        return to_traditional(text)
    return text


__all__ = [
    "SIMPLIFIED_CHINESE",
    "TRADITIONAL_CHINESE",
    "fix_translation",
    "has_simplified_characters",
    "is_chinese_lang",
    "is_traditional_chinese",
    "is_traditional_text",
    "lang_base",
    "simplified_characters",
    "to_traditional",
]
