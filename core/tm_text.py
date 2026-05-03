"""TM 文本标准化工具。"""

import re

_INVISIBLE_CHAR_TRANSLATION = str.maketrans(
    "",
    "",
    "\u200b\u200c\u200d\u2060\ufeff\u00ad\u180e",
)
_SPACE_CHAR_TRANSLATION = str.maketrans({
    "\u00a0": " ",  # no-break space
    "\u2007": " ",  # figure space
    "\u202f": " ",  # narrow no-break space
})
_WHITESPACE_RUN_RE = re.compile(r"\s+")


def normalize_tm_text_for_storage(text: str) -> str:
    """
    统一 TM 文本的存储形态。

    仅处理不可见字符和空白字符，不做语义改写。
    """
    normalized = str(text or "")
    if not normalized:
        return ""

    normalized = normalized.translate(_INVISIBLE_CHAR_TRANSLATION)
    normalized = normalized.translate(_SPACE_CHAR_TRANSLATION)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.replace("\n", " ").replace("\t", " ")
    normalized = _WHITESPACE_RUN_RE.sub(" ", normalized)
    return normalized.strip()


def normalize_tm_text_for_compare(text: str) -> str:
    """
    统一 TM 文本的比较形态。

    当前与存储标准化保持一致，用于屏蔽首尾空白、零宽字符等无意义差异。
    """
    return normalize_tm_text_for_storage(text)
