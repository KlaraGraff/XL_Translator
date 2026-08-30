"""TM 文本标准化工具。"""

import re

_INVISIBLE_CHAR_TRANSLATION = str.maketrans(
    "",
    "",
    "​‌‍⁠﻿­᠎",
)
_SPACE_CHAR_TRANSLATION = str.maketrans({
    " ": " ",  # no-break space
    " ": " ",  # figure space
    " ": " ",  # narrow no-break space
})
_WHITESPACE_RUN_RE = re.compile(r"\s+")
# 行内空白（不含换行）：折叠连续空格/制表符时不能把换行一起吃掉。
_INLINE_SPACE_RUN_RE = re.compile(r"[^\S\n]+")


def normalize_tm_text_for_storage(text: str) -> str:
    """
    统一 TM 文本的存储形态。

    仅处理不可见字符和空白字符，不做语义改写。**换行是内容的一部分**
    （多行单元格重放时要原样还原），所以只统一换行符写法、折叠行内空白，
    绝不把换行折成空格——那会让二次跑同一份文档时多行单元格塌成单行。
    """
    normalized = str(text or "")
    if not normalized:
        return ""

    normalized = normalized.translate(_INVISIBLE_CHAR_TRANSLATION)
    normalized = normalized.translate(_SPACE_CHAR_TRANSLATION)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.replace("\t", " ")
    normalized = _INLINE_SPACE_RUN_RE.sub(" ", normalized)
    normalized = "\n".join(line.strip() for line in normalized.split("\n"))
    return normalized.strip()


def normalize_tm_text_for_compare(text: str) -> str:
    """
    统一 TM 文本的比较形态：在存储形态之上再把换行折成空格。

    这一步同时承担旧库兼容：历史版本入库时就是把换行折成空格存的，
    所以「归一化后的比较形态」与旧库里的存储形态逐字相同，命中判定
    对新旧两种写法都成立。
    """
    return _WHITESPACE_RUN_RE.sub(" ", normalize_tm_text_for_storage(text)).strip()
