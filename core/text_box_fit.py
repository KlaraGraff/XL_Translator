"""估算一段段文字在固定大小的框里要占多高，用来判断译文放不放得下、字要缩到多少。

Excel 的文本框、批注框和 Word 的文本框都是作者定好大小的，译文一长就被框裁掉，
程序又拿不到真正的排版结果，只能估。估的原则是宁可高估：高估的代价是字缩得
略小一点，低估的代价是译文被裁掉、读者看不到。

  - 字宽：汉字等全角字符按 1 个字号宽，其他字符按 0.55 个字号宽；
  - 折行：西文按单词折（单词放不下一整行时才从中间断），汉字逐字折；
  - 行高：单倍行距按 1.4 个字号（中文字体的单倍行高普遍比西文高），再乘段落自己的
    行距倍数；固定行距按给定的磅值；
  - 段前段后间距照加。
"""
from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass

# 字号缩到原来的这个比例还放不下，就不再缩，交给复核清单。
MIN_SCALE = 0.6
_HALF_WIDTH_EM = 0.55
_SINGLE_LINE_EM = 1.4


@dataclass(frozen=True)
class ParagraphMetrics:
    """一段文字的排版量。行距二选一：``line_factor``（单倍 = 1）或 ``exact_line_pt``。"""

    text: str
    size_pt: float
    line_factor: float = 1.0
    exact_line_pt: float | None = None
    spacing_pt: float = 0.0


def _char_em(char: str) -> float:
    return 1.0 if unicodedata.east_asian_width(char) in {"W", "F"} else _HALF_WIDTH_EM


def _tokens(line: str) -> list[str]:
    """折行单位：全角字符逐个；西文连续非空白算一个词；空白单独一段。"""
    tokens: list[str] = []
    word = ""
    for char in line:
        if char.isspace():
            if word:
                tokens.append(word)
                word = ""
            tokens.append(char)
        elif _char_em(char) == 1.0:
            if word:
                tokens.append(word)
                word = ""
            tokens.append(char)
        else:
            word += char
    if word:
        tokens.append(word)
    return tokens


def wrapped_line_count(text: str, size_pt: float, width_pt: float | None) -> int:
    """按框宽折行后的行数；width_pt 为 None 表示不折行。"""
    total = 0
    for line in (text or "").split("\n"):
        if width_pt is None or width_pt <= 0:
            total += 1
            continue
        lines = 1
        used = 0.0
        for token in _tokens(line):
            token_width = sum(_char_em(char) for char in token) * size_pt
            if token.isspace():
                used += token_width
                continue
            if used + token_width <= width_pt:
                used += token_width
                continue
            if token_width > width_pt:
                # 一个词比整行还宽：先另起一行，再按宽度硬断。
                if used > 0:
                    lines += 1
                pieces = math.ceil(token_width / width_pt)
                lines += pieces - 1
                used = token_width - (pieces - 1) * width_pt
                continue
            lines += 1
            used = token_width
        total += lines
    return total


def paragraphs_height(paragraphs: list[ParagraphMetrics], width_pt: float | None, scale: float = 1.0) -> float:
    total = 0.0
    for paragraph in paragraphs:
        size = paragraph.size_pt * scale
        lines = wrapped_line_count(paragraph.text, size, width_pt)
        if paragraph.exact_line_pt is not None:
            line_height = paragraph.exact_line_pt * scale
        else:
            line_height = _SINGLE_LINE_EM * size * paragraph.line_factor
        total += lines * line_height + paragraph.spacing_pt
    return total


def widest_line(paragraphs: list[ParagraphMetrics], scale: float = 1.0) -> float:
    return max(
        (
            sum(_char_em(char) for char in line) * paragraph.size_pt * scale
            for paragraph in paragraphs
            for line in (paragraph.text or "").split("\n")
        ),
        default=0.0,
    )


def fitting_scale(
    before: list[ParagraphMetrics],
    after: list[ParagraphMetrics],
    *,
    width_pt: float,
    height_pt: float,
    wrap: bool = True,
    min_scale: float = MIN_SCALE,
) -> tuple[float, bool]:
    """译文放进框要把字号缩到多少（1 = 不用缩），以及缩到下限时是否放得下。

    只拿框的大小比，不拿「原文估算占多高」当余量：估算本来就偏高，原文恰好贴边
    折一行时会被算成超出，拿它当余量反而放过了真放不下的译文（Mac 实测仿宋 1.5
    倍行距的说明框就是这样漏掉的）。不折行的框，宽度以原文最长一行为准。
    """
    wrap_width = width_pt if wrap else None
    room_height = height_pt
    room_width = max(width_pt, widest_line(before))

    def fits(scale: float) -> bool:
        if paragraphs_height(after, wrap_width, scale) > room_height:
            return False
        return wrap or widest_line(after, scale) <= room_width

    scale = 1.0
    while scale > min_scale and not fits(scale):
        scale = round(scale - 0.05, 2)
    scale = max(min_scale, scale)
    return scale, fits(scale)
