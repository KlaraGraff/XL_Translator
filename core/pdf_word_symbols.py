"""PDF 转 Word 翻译后，数学符号（∨ ∧ ⇒ ⊆ ∅ …）一律用 Cambria Math 显示。

讲义里的数学符号原本用 LaTeX 的字体（CMSY10 等），转换时已改成 Cambria Math
（core.pdf_word_route.normalize_fonts_xml）。但译文是新写的：译文里的「A ∨ B」用的是
这一段正文的字体（讲义的 Chivo 之类），用户电脑上没有这个字体，Mac 上的 Word 换成
Cambria 也没有这些字形，就显示成方框（反馈 #28：∧ 47 处、¬ 以外的 ∨ ⇒ ⊆ 等几十处）。

做法：翻译完的 Word 里，凡是夹着数学符号的一段字，把符号拆出来单独成一段，字体设成
Cambria Math（西文、东亚、复杂文种几种字体都设，主题字体去掉，否则主题字体优先）。
其余的字保持原来的字体。只在 PDF 转 Word 这条路上用：普通 Word 稿的字体是作者装着的。
"""
from __future__ import annotations

import copy
from pathlib import Path

from loguru import logger

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"

#: Office 在 Windows 和 macOS 上都带，有全部数学符号。
MATH_FONT = "Cambria Math"

# 箭头、数学运算符、杂项技术符号里的取整号、数学符号补充 A/B、补充箭头、补充运算符、
# 数学字母数字符号。¬ × ÷ ± 在拉丁-1 里，普通字体都有，不用换。
_MATH_RANGES = (
    (0x2190, 0x21FF),
    (0x2200, 0x22FF),
    (0x2308, 0x230B),
    (0x27C0, 0x27FF),
    (0x2900, 0x2AFF),
    (0x1D400, 0x1D7FF),
)


def is_math_symbol(char: str) -> bool:
    code = ord(char)
    return any(low <= code <= high for low, high in _MATH_RANGES)


def _set_math_font(r_pr) -> None:
    fonts = r_pr.find(f"{_W}rFonts")
    if fonts is None:
        fonts = r_pr.makeelement(f"{_W}rFonts", {})
        r_pr.insert(0, fonts)  # rFonts 在 rPr 里排第一个（rStyle 之后）
        style = r_pr.find(f"{_W}rStyle")
        if style is not None:
            style.addnext(fonts)
    for name in list(fonts.attrib):
        if name.endswith("Theme") or name == f"{_W}hint":
            del fonts.attrib[name]
    for slot in ("ascii", "hAnsi", "eastAsia", "cs"):
        fonts.set(f"{_W}{slot}", MATH_FONT)


def _has_math_font(r_pr) -> bool:
    fonts = r_pr.find(f"{_W}rFonts") if r_pr is not None else None
    if fonts is None:
        return False
    return all(fonts.get(f"{_W}{slot}") == MATH_FONT for slot in ("ascii", "hAnsi", "eastAsia", "cs")) and not any(
        name.endswith("Theme") for name in fonts.attrib
    )


def _pieces(text: str) -> list[tuple[str, bool]]:
    """按「是不是数学符号」切开；空格跟着前面那一截。"""
    pieces: list[tuple[str, bool]] = []
    for char in text:
        math = is_math_symbol(char) if not char.isspace() else (pieces[-1][1] if pieces else False)
        if pieces and pieces[-1][1] == math:
            pieces[-1] = (pieces[-1][0] + char, math)
        else:
            pieces.append((char, math))
    return pieces


def _new_run(run, r_pr, math: bool):
    new_run = run.makeelement(f"{_W}r", dict(run.attrib))
    if r_pr is not None:
        new_run.append(copy.deepcopy(r_pr))
    if math:
        if new_run.find(f"{_W}rPr") is None:
            new_run.insert(0, new_run.makeelement(f"{_W}rPr", {}))
        _set_math_font(new_run.find(f"{_W}rPr"))
    return new_run


def _fix_run(run) -> int:
    """返回这一段改了几处。符号拆成单独的一段；制表符、换行等原样留在各自那一段里。"""
    texts = [child for child in run if child.tag == f"{_W}t"]
    text = "".join(node.text or "" for node in texts)
    if not any(is_math_symbol(char) for char in text):
        return 0
    r_pr = run.find(f"{_W}rPr")
    if _has_math_font(r_pr):
        return 0
    # 按顺序切成（内容, 是不是符号）：文字按符号切开，别的子元素（制表符、换行）自成一截
    parts: list[tuple[object, bool]] = []
    for child in run:
        if child.tag == f"{_W}rPr":
            continue
        if child.tag == f"{_W}t":
            parts.extend(_pieces(child.text or ""))
        else:
            parts.append((child, False))
    if all(math or (isinstance(part, str) and not part.strip()) for part, math in parts):
        if r_pr is None:
            r_pr = run.makeelement(f"{_W}rPr", {})
            run.insert(0, r_pr)
        _set_math_font(r_pr)  # 整段都是符号：不用拆
        return 1
    changed = 0
    anchor = run
    current = None
    current_math = None
    for part, math in parts:
        if current is None or math != current_math or math:
            current = _new_run(run, r_pr, math)
            current_math = math
            anchor.addnext(current)
            anchor = current
            changed += 1 if math else 0
        if isinstance(part, str):
            node = current.makeelement(f"{_W}t", {})
            node.text = part
            if part != part.strip():
                node.set(_XML_SPACE, "preserve")
            current.append(node)
        else:
            current.append(part)  # 挪过来（lxml 会从原来那段里摘下）
    run.getparent().remove(run)
    return changed


def math_symbols_in_math_font(docx_path: Path) -> int:
    """正文、页眉页脚里的数学符号都改用 Cambria Math，返回改了几处。"""
    from docx import Document

    doc = Document(str(docx_path))
    roots = [doc.element.body]
    for section in doc.sections:
        for part in (section.header, section.footer, section.first_page_header, section.first_page_footer,
                     section.even_page_header, section.even_page_footer):
            if not part.is_linked_to_previous:
                roots.append(part._element)
    changed = 0
    seen: set = set()
    for root in roots:
        if id(root) in seen:
            continue
        seen.add(id(root))
        for run in list(root.iter(f"{_W}r")):
            changed += _fix_run(run)
    if changed:
        doc.save(str(docx_path))
        logger.info(f"[PDF] 译文里 {changed} 处数学符号改用 {MATH_FONT}")
    return changed
