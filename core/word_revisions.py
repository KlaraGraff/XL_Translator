"""Word 内容控件（w:sdt）与未接受的修订（w:ins / w:del）。

python-docx 有两处看不见的正文：

- 内容控件里的段落不在 ``doc.paragraphs`` 里——控件是 body 的直接子节点，段落挂在
  它下面的 ``w:sdtContent`` 里，读不到、也写不进去；
- 未接受的修订不在段落的可见文本里——``w:ins`` 包着的 ``w:t`` 读不到，删除的文字在
  ``w:del`` 里的 ``w:delText``。这些字以前一律漏译，只有任务跑起来之后一句「存在无法
  读取的内容」告警（V10.5 之前）。

现在的约定：

- 内容控件（自动目录除外）里的段落当成普通正文段落，参与抽取和写回，不由用户选择；
- 修订在**扫描时**就数出来（``tracked_change_count``），用户在点「开始翻译」时决定：
  ``skip`` 保持原文不动（默认），``translate`` 连修订里的文字一起翻译、修订标记
  （作者、日期、id）原样保留。

本模块只负责这两件事的读写，不在模块顶层 import ``core.word_document``（那边要 import
这里），避免循环导入；自动目录的判定复用那边同一套实现，函数内延迟导入。
"""
from __future__ import annotations

from dataclasses import dataclass

from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph

# 用户对未接受的修订的两种处理方式。缺省（旧客户端不传）按 skip。
REVISION_MODE_SKIP = "skip"
REVISION_MODE_TRANSLATE = "translate"
REVISION_MODES = (REVISION_MODE_SKIP, REVISION_MODE_TRANSLATE)


def normalize_revision_mode(value: object) -> str:
    """把请求里的 revision_mode 归一成两个合法值之一；认不出的按 skip。

    旧客户端根本不传这个字段。绝不能因为缺一个字段就改变已有行为（把修订里的原文
    翻掉），所以默认值是不会改动任何修订内容的 skip。
    """
    cleaned = str(value or "").strip().lower()
    return cleaned if cleaned in REVISION_MODES else REVISION_MODE_SKIP


@dataclass(frozen=True)
class WordRevisionReport:
    """扫描期数到的未接受修订：``w:ins`` + ``w:del``，只数有可见文字的。"""

    tracked_change_count: int = 0

    @property
    def found(self) -> bool:
        return self.tracked_change_count > 0

    def as_dict(self) -> dict[str, int]:
        return {"tracked_change_count": self.tracked_change_count}


@dataclass(frozen=True)
class RevisionWriteStats:
    """一次写回里修订的处理结果（逐文件记录与日志用）。"""

    mode: str = REVISION_MODE_SKIP
    tracked_change_count: int = 0
    translated_count: int = 0

    def describe(self) -> str:
        if self.mode == REVISION_MODE_TRANSLATE:
            return f"翻译了 {self.translated_count} 处修订（保留修订标记）"
        return f"跳过了 {self.tracked_change_count} 处修订（保持原文）"

    def as_dict(self) -> dict[str, object]:
        # 键名不叫 "text"：任务中心的脱敏会把 text 当成正文内容整键丢掉，那句「跳过了
        # N 处修订（保持原文）」就到不了界面上。
        return {
            "mode": self.mode,
            "count": (
                self.translated_count
                if self.mode == REVISION_MODE_TRANSLATE
                else self.tracked_change_count
            ),
            "label": self.describe(),
        }


# ── 结构遍历 ─────────────────────────────────────────────────────────


def _has_ancestor_tag(element, tag: str, root) -> bool:
    qualified = qn(tag)
    parent = element.getparent()
    while parent is not None and parent is not root:
        if parent.tag == qualified:
            return True
        parent = parent.getparent()
    return False


def _iter_outermost_elements(root, tag: str):
    """root 下同名嵌套里最外层的那一个，避免嵌套结构被重复计数。"""
    qualified = qn(tag)
    for element in root.iter(qualified):
        if not _has_ancestor_tag(element, tag, root):
            yield element


def _is_toc_control(element) -> bool:
    from core.word_document import _is_toc_content_control

    return _is_toc_content_control(element)


def toc_controls(body) -> list:
    """正文里的自动目录控件（``w:sdt``）。目录是域，翻译它没有意义。"""
    return [
        control
        for control in _iter_outermost_elements(body, "w:sdt")
        if _is_toc_control(control)
    ]


def _inside(element, containers) -> bool:
    return any(element in list(container.iter()) for container in containers)


def _visible_text(element, tags: tuple[str, ...]) -> str:
    return "".join(
        node.text or "" for node in element.iter(*(qn(tag) for tag in tags))
    ).strip()


# ── 未接受的修订 ─────────────────────────────────────────────────────


def iter_tracked_revisions(body, *, excluded=()) -> list:
    """正文里未接受的修订元素（最外层 ``w:ins`` / ``w:del``），只留有可见文字的。

    ``excluded`` 是不参与处理的容器（调用方传自动目录控件）。

    ``w:ins`` 也用来标记「这个段落标记是新插入的」（``w:pPr/w:rPr/w:ins``），那种
    节点一个 ``w:t`` 都没有，不算一处修订；``w:del`` 里的文字在 ``w:delText`` 里。
    """
    found: list = []
    for tag, text_tags in (
        ("w:ins", ("w:t",)),
        ("w:del", ("w:delText", "w:t")),
    ):
        for element in _iter_outermost_elements(body, tag):
            if not _visible_text(element, text_tags):
                continue
            if excluded and _inside(element, excluded):
                continue
            found.append(element)
    return found


def count_tracked_changes(body, *, excluded=()) -> int:
    """正文里未接受的修订处数（扫描期的 ``tracked_change_count``）。"""
    return len(iter_tracked_revisions(body, excluded=excluded))


def revision_text(element) -> str:
    """这一处修订里的原文：插入读 ``w:t``，删除读 ``w:delText``。"""
    if element.tag == qn("w:del"):
        text = _visible_text(element, ("w:delText",))
        if text:
            return text
    return _visible_text(element, ("w:t",))


def set_revision_text(element, text: str) -> bool:
    """把译文写进这一处修订，修订元素本身（作者、日期、id）原样不动。

    译文写在第一个承载文字的 run 里，同一处修订后面几个 run 的文字清空——一处修订
    = 一个翻译单元，拆成多个 run 只是为了保留局部格式，不是多句话。返回是否写入。
    """
    is_deletion = element.tag == qn("w:del")
    text_tag = qn("w:delText") if is_deletion else qn("w:t")
    nodes = [
        node
        for node in element.iter(text_tag)
        if (node.text or "").strip()
    ]
    if not nodes:
        # 删除标记里个别文档会写成 w:t（不规范但 Word 能读）：退回按 w:t 写。
        nodes = [node for node in element.iter(qn("w:t")) if (node.text or "").strip()]
    if not nodes:
        return False
    nodes[0].text = text
    nodes[0].set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    for extra in nodes[1:]:
        extra.text = ""
    return True


def paragraph_of(element):
    """element 所在的 ``w:p``。"""
    parent = element.getparent()
    while parent is not None and parent.tag != qn("w:p"):
        parent = parent.getparent()
    return parent


# ── 内容控件 ─────────────────────────────────────────────────────────


def _owning_content_control(element, body):
    """这个节点所在的最内层内容控件（``w:sdtContent`` 的父节点）。"""
    parent = element.getparent()
    while parent is not None and parent is not body:
        if parent.tag == qn("w:sdtContent"):
            holder = parent.getparent()
            return holder if holder is not None and holder.tag == qn("w:sdt") else None
        parent = parent.getparent()
    return None


def content_control_paragraph_entries(
    body, *, excluded=(), parent=None
) -> list[tuple[Paragraph, int, int]]:
    """内容控件里的段落：[(Paragraph, 控件序号, 段落在控件内的序号), ...]。

    嵌套控件里的段落只归最内层那个控件；``excluded``（自动目录）整块跳过。

    ``parent`` 传文档的 ``_Body``：段落包装对象要带上它才读得到样式（``paragraph.style``
    得经 ``part`` 找样式表），复制段内格式、判断标题都靠这一步。不传也改得了文字，
    只是样式相关的格式复制会退化成默认。
    """
    controls = [
        control
        for control in _iter_outermost_elements(body, "w:sdt")
        if not (excluded and _inside(control, excluded))
    ]
    entries: list[tuple[Paragraph, int, int]] = []
    for control_index, control in enumerate(controls):
        own = [
            paragraph
            for paragraph in control.iter(qn("w:p"))
            if _owning_content_control(paragraph, body) is control
        ]
        for paragraph_index, paragraph in enumerate(own):
            entries.append((Paragraph(paragraph, parent), control_index, paragraph_index))
    return entries


def _already_translated(paragraphs: list[Paragraph], index: int, *, target_lang: str, source_lang: str) -> bool:
    """补译：这一段后面紧跟着一段目标语言的段落，说明上次已经译过。"""
    from core.translation_coverage import looks_like_source_text, looks_like_target_text

    if index + 1 >= len(paragraphs):
        return False
    following = (paragraphs[index + 1].text or "").strip()
    return bool(
        following
        and looks_like_target_text(following, source_lang=source_lang, target_lang=target_lang)
        and not looks_like_source_text(following, source_lang=source_lang, target_lang=target_lang)
    )


def extract_content_control_segments(
    doc,
    *,
    target_lang: str,
    source_lang: str,
    untranslated_only: bool = False,
) -> list:
    """内容控件里的段落，作为 ``WordSegment`` 列表。

    控件里的段落和普通正文一样参与翻译（用户没有选择），所以定位用
    ``content_control[i].paragraph[j]``，写回时按同一个序号找回。自动目录整块不算。
    """
    from core.word_document import (
        WordSegment,
        _is_toc_or_field_paragraph,
        _is_translatable_source,
        _paragraph_source_text,
    )

    body = doc.element.body
    excluded = toc_controls(body)
    entries = content_control_paragraph_entries(body, excluded=excluded, parent=doc._body)
    # 每个控件里的段落按顺序归组，补译判定要看「下一段是不是已有译文」。
    control_paragraphs: dict[int, list[Paragraph]] = {}
    for paragraph, control_index, _paragraph_index in entries:
        control_paragraphs.setdefault(control_index, []).append(paragraph)

    segments = []
    for paragraph, control_index, paragraph_index in entries:
        source = _paragraph_source_text(paragraph)
        if not _is_translatable_source(source, target_lang=target_lang, source_lang=source_lang):
            continue
        if _is_toc_or_field_paragraph(paragraph):
            continue
        if untranslated_only and _already_translated(
            control_paragraphs.get(control_index, []),
            paragraph_index,
            target_lang=target_lang,
            source_lang=source_lang,
        ):
            continue
        segments.append(
            WordSegment(
                source=source,
                kind="content_control",
                location=f"content_control[{control_index}].paragraph[{paragraph_index}]",
                section_path="内容控件",
            )
        )
    return segments


def apply_content_control_translations(
    doc,
    translations: dict[str, str],
    *,
    target_lang: str,
    source_lang: str,
    output_translation_only: bool = False,
    untranslated_only: bool = False,
) -> int:
    """把译文写进内容控件里的段落（调用方负责 save）。返回写入的段数。

    控件里夹着域（日期占位之类）时走 ``_replace_paragraph_text_around_fields``，域
    留在原位；否则只输出译文就原地替换，双语版式就在控件里另起一段译文——控件本身
    不动，用户还在同一个表单格子里看到原文和译文。
    """
    from core.word_document import (
        _insert_translation_paragraph_after,
        _is_toc_or_field_paragraph,
        _is_translatable_source,
        _paragraph_has_field,
        _paragraph_source_text,
        _replace_paragraph_text,
        _replace_paragraph_text_around_fields,
        _resolve_translation,
    )

    body = doc.element.body
    excluded = toc_controls(body)
    entries = content_control_paragraph_entries(body, excluded=excluded, parent=doc._body)
    control_paragraphs: dict[int, list[Paragraph]] = {}
    for paragraph, control_index, _paragraph_index in entries:
        control_paragraphs.setdefault(control_index, []).append(paragraph)

    written = 0
    for paragraph, control_index, paragraph_index in entries:
        source = _paragraph_source_text(paragraph)
        if not _is_translatable_source(source, target_lang=target_lang, source_lang=source_lang):
            continue
        if _is_toc_or_field_paragraph(paragraph):
            continue
        if untranslated_only and _already_translated(
            control_paragraphs.get(control_index, []),
            paragraph_index,
            target_lang=target_lang,
            source_lang=source_lang,
        ):
            continue
        resolved = _resolve_translation(source, translations)
        if resolved is None:
            continue
        if (resolved.replace_only or output_translation_only) and not _paragraph_has_field(paragraph):
            _replace_paragraph_text(paragraph, resolved.text, target_lang=target_lang)
        elif (resolved.replace_only or output_translation_only) and _replace_paragraph_text_around_fields(
            paragraph, resolved.text, target_lang=target_lang
        ):
            pass
        else:
            _insert_translation_paragraph_after(paragraph, resolved.text, target_lang=target_lang)
        written += 1
    return written


def extract_revision_segments(
    doc,
    *,
    target_lang: str,
    source_lang: str,
    untranslated_only: bool = False,
) -> list:
    """未接受修订里的原文，作为 ``WordSegment`` 列表（只在「修订一起翻译」时用）。"""
    from core.translation_coverage import looks_like_source_text, looks_like_target_text
    from core.word_document import WordSegment, _is_translatable_source

    body = doc.element.body
    excluded = toc_controls(body)
    segments = []
    for index, element in enumerate(iter_tracked_revisions(body, excluded=excluded)):
        source = revision_text(element)
        if not _is_translatable_source(source, target_lang=target_lang, source_lang=source_lang):
            continue
        if untranslated_only and looks_like_target_text(
            source, source_lang=source_lang, target_lang=target_lang
        ) and not looks_like_source_text(source, source_lang=source_lang, target_lang=target_lang):
            continue
        segments.append(
            WordSegment(
                source=source,
                kind="tracked_change",
                location=f"revision[{index}]",
                section_path="修订",
            )
        )
    return segments


def apply_revision_translations(
    doc,
    translations: dict[str, str],
    *,
    target_lang: str,
    source_lang: str,
    mode: str = REVISION_MODE_SKIP,
    untranslated_only: bool = False,
) -> RevisionWriteStats:
    """按用户选择的模式处理未接受的修订（调用方负责 save）。

    ``skip``：一个字都不动，只数出有几处（逐文件记录写「跳过了 N 处修订」）；
    ``translate``：把 ``w:ins`` / ``w:del`` 里的文字换成译文，修订元素本身（作者、
    日期、id）原样保留——对方打开译文仍然能看出改了哪些地方。
    """
    from core.translation_coverage import looks_like_source_text, looks_like_target_text
    from core.word_document import _is_translatable_source, _resolve_translation

    body = doc.element.body
    excluded = toc_controls(body)
    elements = iter_tracked_revisions(body, excluded=excluded)
    stats = RevisionWriteStats(
        mode=normalize_revision_mode(mode), tracked_change_count=len(elements)
    )
    if stats.mode != REVISION_MODE_TRANSLATE:
        return stats
    translated = 0
    for element in elements:
        source = revision_text(element)
        if not _is_translatable_source(source, target_lang=target_lang, source_lang=source_lang):
            continue
        if untranslated_only and looks_like_target_text(
            source, source_lang=source_lang, target_lang=target_lang
        ) and not looks_like_source_text(source, source_lang=source_lang, target_lang=target_lang):
            continue
        resolved = _resolve_translation(source, translations)
        if resolved is None or not resolved.text.strip():
            continue
        if set_revision_text(element, resolved.text.strip()):
            translated += 1
    return RevisionWriteStats(
        mode=stats.mode,
        tracked_change_count=stats.tracked_change_count,
        translated_count=translated,
    )


def inline_content_control_count(body, *, excluded=()) -> int:
    """行内内容控件（日期、勾选框之类）的个数。

    这类控件挂在 ``w:r`` 上、和正文混在同一段里，本版还没法把它们当成独立段落翻译。
    调用方据此保留「存在无法读取的内容」告警，而不是悄悄漏掉。
    """
    total = 0
    for control in _iter_outermost_elements(body, "w:sdt"):
        if excluded and _inside(control, excluded):
            continue
        if next(control.iter(qn("w:p")), None) is not None:
            continue
        if _visible_text(control, ("w:t",)):
            total += 1
    return total
