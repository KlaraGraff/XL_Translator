"""Word 里的代码段原样保留，不送翻（maple 2026-11-26 选定）。

依据是字体：一段（或一个表格格子）里所有的字都是等宽字体（Courier、Consolas、Menlo、
LaTeX 的 LM Mono / CMTT 这类），而全文正文不是等宽字体，就是作者标出来的代码、命令。
模型拿到「class Stack:」只会译成「类 栈:」，所以这种段落不送翻，交付前的体检也不算漏翻。

- 夹着一个普通字体的字就不算（正文里的 ``collections.deque``、表格里的「show changed files」照翻）。
- 全文八成以上都是等宽字体（老式打字稿整篇 Courier New）时这条不生效，全文照翻。
- 代码里的注释跟着整段原样保留，不翻。

PDF 转 Word 和直接翻 Word 都走这里（core.word_document 抽取时、core.word_coverage 体检时）。
"""
from __future__ import annotations

import re

from docx.oxml.ns import qn

# 「Mono」后面不能是 type（Monotype Corsiva 不是等宽字体）。
_MONOSPACE_FONT = re.compile(
    r"mono(?!type)|courier|consolas|menlo|monaco|typewriter|console|inconsolata|source ?code|"
    r"fira ?code|cascadia|jetbrains|andale|^cmtt|^lmtt|^sfmono",
    re.IGNORECASE,
)


def is_monospace_font(name: str) -> bool:
    return bool(name) and bool(_MONOSPACE_FONT.search(name))


class _FontResolver:
    """一段字实际用的西文字体：字上直接设的 → 字符样式 → 段落样式（含 basedOn）→ 文档默认。"""

    def __init__(self, doc) -> None:
        self._styles: dict[str, object] = {}
        self._default = ""
        try:
            styles = doc.styles.element
        except Exception:  # noqa: BLE001 - a document without styles.xml just has no inherited fonts
            return
        for style in styles.iterfind(qn("w:style")):
            style_id = style.get(qn("w:styleId"))
            if style_id:
                self._styles[style_id] = style
        default = styles.find(f"{qn('w:docDefaults')}/{qn('w:rPrDefault')}/{qn('w:rPr')}")
        self._default = self._font_of(default)

    @staticmethod
    def _font_of(r_pr) -> str:
        fonts = r_pr.find(qn("w:rFonts")) if r_pr is not None else None
        if fonts is None:
            return ""
        return str(fonts.get(qn("w:ascii")) or fonts.get(qn("w:hAnsi")) or "")

    def _style_font(self, style_id: str | None, depth: int = 0) -> str:
        if not style_id or depth > 20:
            return ""
        style = self._styles.get(style_id)
        if style is None:
            return ""
        font = self._font_of(style.find(qn("w:rPr")))  # type: ignore[union-attr]
        if font:
            return font
        based = style.find(qn("w:basedOn"))  # type: ignore[union-attr]
        return self._style_font(based.get(qn("w:val")) if based is not None else None, depth + 1)

    def run_font(self, run, paragraph_style: str | None) -> str:
        r_pr = run.find(qn("w:rPr"))
        font = self._font_of(r_pr)
        if font:
            return font
        if r_pr is not None:
            run_style = r_pr.find(qn("w:rStyle"))
            font = self._style_font(run_style.get(qn("w:val")) if run_style is not None else None)
            if font:
                return font
        return self._style_font(paragraph_style) or self._style_font("Normal") or self._default


def _paragraph_style(p) -> str | None:
    style = p.find(f"{qn('w:pPr')}/{qn('w:pStyle')}")
    return style.get(qn("w:val")) if style is not None else None


def _text_runs(p):
    """段落里有字的 run（含超链接里的），连同它的字。"""
    for run in p.iter(qn("w:r")):
        text = "".join(node.text or "" for node in run.iter(qn("w:t")))
        if text.strip():
            yield run, text


class CodeFonts:
    def __init__(self, doc) -> None:
        self._fonts = _FontResolver(doc)
        mono = other = 0
        for p in doc.element.body.iter(qn("w:p")):
            style = _paragraph_style(p)
            for run, text in _text_runs(p):
                count = sum(1 for char in text if not char.isspace())
                if is_monospace_font(self._fonts.run_font(run, style)):
                    mono += count
                else:
                    other += count
        #: 全文八成以上的字都是等宽字体（整篇 Courier 的打字稿）：没法用字体区分代码，这条不生效。
        #: 门槛不取一半：编程讲义里代码占一半以上很常见，那时代码照样要认出来。
        self.body_is_monospace = mono >= 4 * other and mono > 0

    def is_code_paragraph(self, p) -> bool:
        if self.body_is_monospace:
            return False
        style = _paragraph_style(p)
        runs = list(_text_runs(p))
        return bool(runs) and all(is_monospace_font(self._fonts.run_font(run, style)) for run, _ in runs)

    def is_code_cell(self, tc) -> bool:
        paragraphs = [p for p in tc.iterchildren(qn("w:p")) if any(True for _ in _text_runs(p))]
        return bool(paragraphs) and all(self.is_code_paragraph(p) for p in paragraphs)


def code_only_texts(doc) -> frozenset[str]:
    """只以代码出现的段落、格子文字（抽取和写回都按文字对，同一句话在别处是正文就照翻）。"""
    from core.word_document import (
        _cell_source_text,
        _iter_unique_table_cells,
        _paragraph_source_text,
    )

    fonts = CodeFonts(doc)
    if fonts.body_is_monospace:
        return frozenset()
    code: set[str] = set()
    prose: set[str] = set()
    for paragraph in doc.paragraphs:
        text = _paragraph_source_text(paragraph)
        if text:
            (code if fonts.is_code_paragraph(paragraph._p) else prose).add(text)
    for table in doc.tables:
        for cell in _iter_unique_table_cells(table):
            text = _cell_source_text(cell)
            if text:
                (code if fonts.is_code_cell(cell._tc) else prose).add(text)
    return frozenset(code - prose)
