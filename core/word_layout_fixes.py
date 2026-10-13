"""Word 译文排版的三处小修：软换行并句、固定行高放开、东亚断行语言。

都是从「PDF 转成 Word 再翻译」那条路上学来的（PR #62 的 core/pdf_word_route.py），
普通 Word 稿同样会遇到，只是规则更保守：pdf2docx 的输出几乎每处换行都是排版折行，
普通 Word 稿里的手动换行很多是作者有意为之（地址、落款、表头「名称\\nName」）。

1. join_wrapped_line_breaks：从 PDF 复制过来的段落常带手动换行（w:br），一句话被
   折成几行。整段送去翻译时模型照着换行逐行翻，每行半句，译文里还留着原文的折行
   位置。只有明显是句中折行的才换成空格（见 _is_wrapped_break）。
2. loosen_row_height：模板表格常把行高定成「固定值」，正好装下原文。译文换行位置
   不同、法文比中文长两三倍、双语还多一行，多出来的部分被裁掉，看起来像漏译。
   写过译文的行改成「最小值」：原文一样高，译文长了行跟着长。
3. apply_east_asian_language：英文、法文原稿的默认东亚语言多是 en-US，译成中文、
   日文、韩文后 Word 不按东亚规则断行，句号逗号会跑到行首。改成译文的语言。
   只改「东亚语言」这一项（只管汉字、假名、谚文怎么排），西文部分的语言不动。
"""
from __future__ import annotations

import re

from docx.document import Document as DocumentObject
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

# 句末标点：前一行以这些结尾，换行就是有意的分行
_SENTENCE_END = tuple(".!?:;。！？：；…")
# 后一行以列表符号、编号开头：是新的一项
_LIST_START = re.compile(
    r"^\s*(?:[-•▪●○◦*–—·]|\(?\d{1,3}[.)、]|\[\d+\]|[a-zA-Z][.)]\s|[（(][一二三四五六七八九十\d]+[)）]|[一二三四五六七八九十]+、)"
)
_CJK = re.compile(r"[㐀-鿿豈-﫿]")
_LETTER = re.compile(r"[^\W\d_]")
# 判定为「排满一行后折行」的最短长度：中文按字、西文按字符
_MIN_WRAPPED_CJK_LINE = 20
_MIN_WRAPPED_LATIN_LINE = 50
# 后一行以小写字母开头时，前一行只要不是太短的标签就行
_MIN_LOWERCASE_CONTINUATION_LINE = 15

_SKIP_PARAGRAPH_TAGS = frozenset(
    {qn("w:fldChar"), qn("w:instrText"), qn("w:fldSimple"), qn("w:drawing"), qn("w:pict"), qn("w:object")}
)


def _is_wrapped_break(head: str, tail: str) -> bool:
    """head 是换行前这一行，tail 是换行后这一行（都去掉了首尾空白）。"""
    if not head or not tail or head.endswith(_SENTENCE_END) or _LIST_START.match(tail):
        return False
    # 「instal-\nlation」这种断词连字符，并成空格反而多出一截，不动
    if head.endswith(("-", "‐", "­")):
        return False
    if not _LETTER.search(head) or not _LETTER.match(tail[0]):
        return False
    if tail[0].islower() and len(head) >= _MIN_LOWERCASE_CONTINUATION_LINE:
        return True
    if _CJK.search(head[-1]) and _CJK.match(tail[0]):
        return len(head) >= _MIN_WRAPPED_CJK_LINE
    return len(head) >= _MIN_WRAPPED_LATIN_LINE and not _CJK.search(head)


def _paragraph_lines(paragraph_element) -> list[tuple[str, object | None]]:
    """按 w:br / w:cr 切开段落文字：[(这一行的文字, 行尾的换行节点或 None)]。"""
    lines: list[tuple[str, object | None]] = []
    buffer: list[str] = []
    for node in paragraph_element.iter(qn("w:t"), qn("w:tab"), qn("w:br"), qn("w:cr")):
        if node.tag == qn("w:t"):
            buffer.append(node.text or "")
        elif node.tag == qn("w:tab"):
            buffer.append("\t")
        else:
            lines.append(("".join(buffer), node))
            buffer = []
    lines.append(("".join(buffer), None))
    return lines


def _join_paragraph_breaks(paragraph_element) -> int:
    if any(True for tag in _SKIP_PARAGRAPH_TAGS for _ in paragraph_element.iter(tag)):
        return 0
    lines = _paragraph_lines(paragraph_element)
    if len(lines) < 2:
        return 0
    joined = 0
    for index, (raw_head, node) in enumerate(lines[:-1]):
        # 分页符、分栏符和绕排图片用的 w:clear 换行都不是折行
        if node is None or node.get(qn("w:type")) not in (None, "textWrapping") or node.get(qn("w:clear")):
            continue
        head, tail = raw_head.strip(), lines[index + 1][0].strip()
        # 制表符对齐的行（「姓名\t张三」）是表格式排版，不动
        if "\t" in raw_head or "\t" in lines[index + 1][0] or not _is_wrapped_break(head, tail):
            continue
        cjk_join = bool(_CJK.search(head[-1]) and _CJK.match(tail[0]))
        text = "" if cjk_join or raw_head.endswith(" ") or lines[index + 1][0].startswith(" ") else " "
        replacement = OxmlElement("w:t")
        replacement.text = text
        replacement.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        node.getparent().replace(node, replacement)
        joined += 1
    return joined


def join_wrapped_line_breaks(doc: DocumentObject) -> int:
    """正文（含表格、文本框）里句中折行的手动换行换成空格，返回换掉的个数。"""
    return sum(_join_paragraph_breaks(p) for p in doc.element.body.iter(qn("w:p")))


def join_wrapped_line_breaks_in_file(path) -> int:
    """翻译前的工作副本上做一次：抽取原文和写回译文读的是同一份，前后一致。"""
    from docx import Document

    doc = Document(str(path))
    joined = join_wrapped_line_breaks(doc)
    if joined:
        doc.save(str(path))
    return joined


def loosen_row_height(cell) -> bool:
    """单元格所在行的固定行高改成最小行高；改了返回 True。"""
    row = cell._tc.getparent()
    tr_pr = row.find(qn("w:trPr")) if row is not None else None
    height = tr_pr.find(qn("w:trHeight")) if tr_pr is not None else None
    if height is None or height.get(qn("w:hRule")) != "exact":
        return False
    height.set(qn("w:hRule"), "atLeast")
    return True


_EAST_ASIAN_LOCALES = {
    "zh": "zh-CN", "zh-cn": "zh-CN", "zh-hans": "zh-CN", "zh-tw": "zh-TW", "zh-hant": "zh-TW",
    "zh-hk": "zh-HK", "ja": "ja-JP", "ko": "ko-KR",
}


def east_asian_locale(target_lang: str | None) -> str:
    lang = str(target_lang or "").strip().lower().replace("_", "-")
    return _EAST_ASIAN_LOCALES.get(lang) or _EAST_ASIAN_LOCALES.get(lang.split("-")[0], "")


def _is_east_asian_value(value: str | None) -> bool:
    return str(value or "").lower().split("-")[0] in ("zh", "ja", "ko")


def apply_east_asian_language(doc: DocumentObject, target_lang: str | None) -> int:
    """译文是中文、日文、韩文时，文档默认和样式里的东亚语言改成译文的语言（日文模板
    做的文档译成中文，汉字也该按中文排）；正文 run 上只改写成西文的，原本就是东亚
    语言的不动（中译日的双语稿里原文那几段还是中文）。返回改动处数。"""
    locale = east_asian_locale(target_lang)
    if not locale:
        return 0
    styles = doc.styles.element
    changed = 0
    for root, keep_east_asian in ((styles, False), (doc.element.body, True)):
        for lang in root.iter(qn("w:lang")):
            value = lang.get(qn("w:eastAsia"))
            if value is None or value == locale or (keep_east_asian and _is_east_asian_value(value)):
                continue
            lang.set(qn("w:eastAsia"), locale)
            changed += 1
    # 文档默认里没写东亚语言时，Word 用的是系统的编辑语言，在西文系统上就是英文
    defaults = styles.find(qn("w:docDefaults"))
    if defaults is None:
        defaults = OxmlElement("w:docDefaults")
        styles.insert(0, defaults)
    r_pr_default = defaults.find(qn("w:rPrDefault"))
    if r_pr_default is None:
        r_pr_default = OxmlElement("w:rPrDefault")
        defaults.insert(0, r_pr_default)
    r_pr = r_pr_default.find(qn("w:rPr"))
    if r_pr is None:
        r_pr = OxmlElement("w:rPr")
        r_pr_default.append(r_pr)
    lang = r_pr.find(qn("w:lang"))
    if lang is None:
        lang = OxmlElement("w:lang")
        _insert_lang_in_order(r_pr, lang)
    if lang.get(qn("w:eastAsia")) is None:
        lang.set(qn("w:eastAsia"), locale)
        changed += 1
    return changed


# w:rPr 子元素的固定顺序里 w:lang 之后的那几个；新建的 w:lang 要插在它们前面
_RPR_AFTER_LANG = (qn("w:eastAsianLayout"), qn("w:specVanish"), qn("w:oMath"))


def _insert_lang_in_order(r_pr, lang) -> None:
    for child in r_pr:
        if child.tag in _RPR_AFTER_LANG:
            child.addprevious(lang)
            return
    r_pr.append(lang)
