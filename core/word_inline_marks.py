"""Word 段内局部格式：用标记随原文发给模型，译完按标记还原。

整段翻译时译文写进一个 run，段内的局部格式（「注意：」加粗、句中某个词斜体、带链接
的文字、m² 的上标）就合成了一种。这里沿用专业翻译软件和 DeepL、BabelDOC 的做法：
段落里格式和主体不同的那几截用 ``<g1>…</g1>`` 包住随原文发出，模型把标记放到对应
的译文上，写回时每截按原来的格式（和链接）各写一个 run。

保底：只有确实带局部格式、结构简单（只有文字 run 和超链接，没有制表符、换行、域、
图片、脚注引用）的段落才加标记；译文里的标记少了、多了、乱了，就去掉标记、按现在的
整段写法写回，不报错。去掉标记后的纯文本照常走校验、记忆库和复核，和以前完全一样。
"""
from __future__ import annotations

import re
import threading
from copy import deepcopy
from dataclasses import dataclass, field, replace

from docx.oxml import OxmlElement
from docx.oxml.ns import qn

# 一段里最多加几对标记；再多说明格式很碎，模型放错的概率也高，按整段写法处理
MAX_GROUPS_PER_UNIT = 12

_TAG_RE = re.compile(r"<\s*(/?)\s*g\s*(\d+)\s*>", re.IGNORECASE)
_LITERAL_TAG_RE = re.compile(r"</?g\d+>", re.IGNORECASE)

MARK_PROMPT_RULE = (
    "部分词条带有 <g1>…</g1> 这样成对的格式标记，标出原文中加粗、斜体、链接等局部格式。"
    "译文必须保留每一对标记，各包住与原文标记内容对应的译文；标记位置可以随译文语序调整，"
    "但不得增加、删除、合并、嵌套或改动标记。标记两侧的空格按译文语言的习惯书写，"
    "如「<g1>Note :</g1> les cotes…」冒号后照常留空格。"
)

_PARAGRAPH_CHILDREN = frozenset(
    qn(tag) for tag in ("w:pPr", "w:r", "w:hyperlink", "w:bookmarkStart", "w:bookmarkEnd", "w:proofErr",
                        "w:permStart", "w:permEnd")
)
_HYPERLINK_CHILDREN = frozenset((qn("w:r"), qn("w:proofErr")))
_RUN_CHILDREN = frozenset((qn("w:rPr"), qn("w:t"), qn("w:lastRenderedPageBreak")))
# 不影响「看起来是不是同一种格式」的属性：字体名（中西文混排时 run 常按文种拆开）、
# 语言、拼写检查、复杂文种的对应项、字距、Word 2010 的文字效果
_IGNORED_RPR = frozenset(
    qn(tag) for tag in ("w:rFonts", "w:lang", "w:noProof", "w:szCs", "w:bCs", "w:iCs", "w:kern", "w:spacing",
                        "w:rtl", "w:cs", "w:eastAsianLayout", "w:specVanish", "w:webHidden", "w:snapToGrid",
                        "w:fitText", "w:w")
)
_ON_OFF = frozenset(
    qn(tag) for tag in ("w:b", "w:i", "w:caps", "w:smallCaps", "w:strike", "w:dstrike", "w:vanish", "w:outline",
                        "w:shadow", "w:emboss", "w:imprint")
)
_W14 = "{http://schemas.microsoft.com/office/word/2010/wordml}"


@dataclass(frozen=True)
class MarkGroup:
    rpr: object | None
    hyperlink: object | None = None


@dataclass
class ParagraphMarks:
    """一段（或单元格里的一段）带标记的原文，以及每对标记对应的格式。"""

    marked_text: str
    plain_text: str
    groups: dict[int, MarkGroup] = field(default_factory=dict)
    base_rpr: object | None = None


@dataclass
class CellMarks:
    marked_text: str
    paragraphs: list[ParagraphMarks]

    @property
    def groups(self) -> dict[int, MarkGroup]:
        merged: dict[int, MarkGroup] = {}
        for item in self.paragraphs:
            merged.update(item.groups)
        return merged


def _on_off_value(element) -> bool:
    value = str(element.get(qn("w:val")) or "").lower()
    return value not in ("0", "false", "off", "none")


def _look_key(rpr) -> tuple:
    if rpr is None:
        return ()
    items = []
    for child in rpr:
        tag = child.tag
        if not isinstance(tag, str) or tag in _IGNORED_RPR or tag.startswith(_W14):
            continue
        if tag in _ON_OFF:
            if _on_off_value(child):
                items.append((tag,))
            continue
        if tag == qn("w:u") and str(child.get(qn("w:val")) or "").lower() == "none":
            continue
        items.append((tag, tuple(sorted(child.attrib.items()))))
    return tuple(sorted(items))


def _run_text(run_element) -> str:
    return "".join(node.text or "" for node in run_element.iter(qn("w:t")))


def _content_runs(paragraph_element) -> list[tuple[object, object | None]] | None:
    """[(w:r, 所在的 w:hyperlink 或 None)]；结构不简单时返回 None。"""
    runs: list[tuple[object, object | None]] = []
    for child in paragraph_element:
        tag = child.tag
        if not isinstance(tag, str) or tag not in _PARAGRAPH_CHILDREN:
            return None
        if tag == qn("w:r"):
            runs.append((child, None))
        elif tag == qn("w:hyperlink"):
            for nested in child:
                if not isinstance(nested.tag, str) or nested.tag not in _HYPERLINK_CHILDREN:
                    return None
                if nested.tag == qn("w:r"):
                    runs.append((nested, child))
    for run, _hyperlink in runs:
        if any(not isinstance(node.tag, str) or node.tag not in _RUN_CHILDREN for node in run):
            return None
    return runs


def paragraph_marks(paragraph, *, start_id: int = 1) -> ParagraphMarks | None:
    """这一段能不能加标记：能就返回（groups 为空表示格式统一，不需要标记），不能返回 None。"""
    runs = _content_runs(paragraph._p)
    if runs is None:
        return None
    pieces: list[tuple[str, tuple, object | None, object]] = []
    for run, hyperlink in runs:
        text = _run_text(run)
        if text:
            rpr = run.find(qn("w:rPr"))
            pieces.append((text, _look_key(rpr), hyperlink, rpr))
    plain_full = "".join(piece[0] for piece in pieces)
    plain = plain_full.strip()
    if not plain or plain != (paragraph.text or "").strip() or _LITERAL_TAG_RE.search(plain):
        return None

    weights: dict[tuple, int] = {}
    for text, look, _hyperlink, _rpr in pieces:
        weights[look] = weights.get(look, 0) + len(text.strip())
    base_look = max(weights, key=lambda look: (weights[look], -_first_index(pieces, look)))
    base_rpr = next((rpr for _t, look, _h, rpr in pieces if look == base_look), None)

    # 每一截归到哪一组：和主体格式一样、又不在链接里的不加标记；只有空白的那截
    # 夹在同一组中间就并进去，否则归主体
    keys: list[tuple | None] = []
    for text, look, hyperlink, _rpr in pieces:
        if hyperlink is None and look == base_look:
            keys.append(None)
        else:
            keys.append((look, id(hyperlink) if hyperlink is not None else 0))
    for index, (text, _look, hyperlink, _rpr) in enumerate(pieces):
        if text.strip() or hyperlink is not None:
            continue
        before = keys[index - 1] if index else None
        after = keys[index + 1] if index + 1 < len(keys) else None
        keys[index] = before if before is not None and before == after else None

    # 去掉段首段尾的空白（和 paragraph.text.strip() 对齐）
    lead = len(plain_full) - len(plain_full.lstrip())
    trail = len(plain_full) - len(plain_full.rstrip())
    trimmed: list[tuple[str, tuple | None, int]] = []
    offset = 0
    for index, (text, *_rest) in enumerate(pieces):
        start, end = offset, offset + len(text)
        offset = end
        lo, hi = max(start, lead), min(end, len(plain_full) - trail)
        if lo < hi:
            trimmed.append((plain_full[lo:hi], keys[index], index))

    groups: dict[int, MarkGroup] = {}
    parts: list[str] = []
    next_id = start_id
    current_key: tuple | None = None
    for text, key, index in trimmed:
        if key != current_key:
            if current_key is not None:
                parts.append(f"</g{next_id - 1}>")
            if key is not None:
                _t, _look, hyperlink, rpr = pieces[index]
                groups[next_id] = MarkGroup(rpr=rpr, hyperlink=hyperlink)
                parts.append(f"<g{next_id}>")
                next_id += 1
            current_key = key
        parts.append(text)
    if current_key is not None:
        parts.append(f"</g{next_id - 1}>")
    if len(groups) > MAX_GROUPS_PER_UNIT:
        return None
    marked = "".join(parts)
    if strip_marks(marked) != plain:
        return None
    return ParagraphMarks(marked_text=marked if groups else plain, plain_text=plain, groups=groups, base_rpr=base_rpr)


def _first_index(pieces, look) -> int:
    return next(index for index, piece in enumerate(pieces) if piece[1] == look)


def cell_marks(paragraphs: list) -> CellMarks | None:
    """单元格：各段原文用换行拼起来（同 word_document._cell_source_text），标记编号跨段连续。"""
    items: list[ParagraphMarks] = []
    next_id = 1
    for paragraph in paragraphs:
        marks = paragraph_marks(paragraph, start_id=next_id)
        if marks is None:
            return None
        items.append(marks)
        next_id += len(marks.groups)
    if not items or next_id == 1 or next_id - 1 > MAX_GROUPS_PER_UNIT:
        return None
    return CellMarks(marked_text="\n".join(item.marked_text for item in items), paragraphs=items)


def strip_marks(text: str) -> str:
    return _TAG_RE.sub("", str(text or ""))


# 标点紧贴标记、标记后紧跟字母：「<g1>Note :</g1>les…」。原文是中文时冒号后本来没有空格，
# 模型在标记边界常照抄，去掉标记就成了「Note :les」。只补这一种明显缺的空格：
# 冒号、分号、逗号、问号、叹号、右括号后跟字母，或句号后跟大写字母；空格放在格式外面
# （闭标记之后、开标记之前）。上标「m<g1>2</g1>」、半个词加粗这类不碰。
_BOUNDARY_GAP_RE = re.compile(
    r"(?:(?<=[:;,!?)\]»])|(?<=\.)(?=(?:<[^>]*>)+[A-ZÀ-ÖØ-Þ]))"
    r"((?:<\s*/\s*[gG]\s*\d+\s*>)*)((?:<\s*[gG]\s*\d+\s*>)*)(?=[^\W\d_])"
)


def add_boundary_spaces(translation: str, target_lang: str | None) -> str:
    """译文用空格分词时，补上标记边界处模型漏掉的空格（见 _BOUNDARY_GAP_RE）。"""
    lang = str(target_lang or "").strip().lower().replace("_", "-").split("-")[0]
    if lang in ("zh", "ja", "ko", "th", "lo", "km", "my"):
        return translation

    def fill(match: re.Match) -> str:
        closing, opening = match.group(1), match.group(2)
        return f"{closing} {opening}" if closing or opening else match.group(0)

    return _BOUNDARY_GAP_RE.sub(fill, str(translation or ""))


def parse_marked(text: str, allowed_ids) -> list[tuple[str, int | None]] | None:
    """把带标记的译文拆成 [(文字, 组号或 None)]。每个组号必须恰好一开一闭、不嵌套，
    组号集合和原文完全一致；否则返回 None。"""
    allowed = set(allowed_ids)
    pieces: list[tuple[str, int | None]] = []
    seen: set[int] = set()
    current: int | None = None
    position = 0
    for match in _TAG_RE.finditer(text):
        chunk = text[position:match.start()]
        if chunk:
            pieces.append((chunk, current))
        position = match.end()
        closing, group = match.group(1) == "/", int(match.group(2))
        if group not in allowed:
            return None
        if closing:
            if current != group:
                return None
            current = None
        else:
            if current is not None or group in seen:
                return None
            seen.add(group)
            current = group
    tail = text[position:]
    if tail:
        pieces.append((tail, current))
    if current is not None or seen != allowed:
        return None
    return pieces


def _new_run(text: str, rpr):
    run = OxmlElement("w:r")
    if rpr is not None:
        run.append(deepcopy(rpr))
    t = OxmlElement("w:t")
    t.text = text
    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    run.append(t)
    return run


def build_run_nodes(pieces, groups: dict[int, MarkGroup], base_rpr) -> list:
    """按拆好的译文生成 w:r（链接那截包在 w:hyperlink 里），返回要插进段落的节点。"""
    nodes = []
    for text, group in pieces:
        if not text:
            continue
        if group is None:
            nodes.append(_new_run(text, base_rpr))
            continue
        style = groups[group]
        run = _new_run(text, style.rpr)
        if style.hyperlink is None:
            nodes.append(run)
            continue
        link = OxmlElement("w:hyperlink")
        for key, value in style.hyperlink.attrib.items():
            link.set(key, value)
        link.append(run)
        nodes.append(link)
    return nodes


def replace_paragraph_content(paragraph, nodes) -> None:
    """删掉段落里原有的文字 run 和超链接，把新节点放在第一个被删节点的位置。"""
    element = paragraph._p
    old = [child for child in element if child.tag in (qn("w:r"), qn("w:hyperlink"))]
    if old:
        anchor_index = list(element).index(old[0])
        for child in old:
            element.remove(child)
    else:
        anchor_index = len(element)
    for offset, node in enumerate(nodes):
        element.insert(anchor_index + offset, node)


def iter_new_runs(nodes):
    for node in nodes:
        if node.tag == qn("w:hyperlink"):
            yield from node.iterchildren(qn("w:r"))
        else:
            yield node


class InlineMarkEngine:
    """包住翻译引擎：带局部格式的段落换成带标记的原文发出，回来的译文校验标记、记下，
    再去掉标记交回，下游（校验、记忆库、复核）看到的仍是纯文本。"""

    # engines.base_engine.engine_supports_chat 据此看里面那个引擎
    _delegates_capabilities = True

    def __init__(self, inner, marked_sources: dict[str, str]):
        self.inner = inner
        self._marked_sources = dict(marked_sources)
        self._lock = threading.Lock()
        self.marked_translations: dict[str, tuple[str, str]] = {}

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def _outgoing(self, texts: list[str], system_prompt: str) -> tuple[list[str], str]:
        sent = [self._marked_sources.get(text, text) for text in texts]
        if sent == list(texts):
            return sent, system_prompt
        return sent, f"{system_prompt}\n\n{MARK_PROMPT_RULE}".strip()

    def _receive(self, plain: str, sent: str, translation: str, target_lang: str) -> str:
        if sent is plain or sent == plain:
            return translation
        ok = marks_match(sent, translation)
        if ok:
            translation = add_boundary_spaces(translation, target_lang)
        with self._lock:
            if ok:
                self.marked_translations[plain] = (sent, translation)
            else:
                self.marked_translations.pop(plain, None)
        return strip_marks(translation)

    def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
        sent, prompt = self._outgoing(list(texts), system_prompt)
        if prompt is system_prompt:
            return self.inner.translate_batch(texts, target_lang, system_prompt, source_lang=source_lang)
        raw = self.inner.translate_batch(sent, target_lang, prompt, source_lang=source_lang)
        result: dict[str, str] = {}
        for plain, outgoing in zip(texts, sent):
            if outgoing in raw:
                result[plain] = self._receive(plain, outgoing, raw[outgoing], target_lang)
        return result

    def translate_batch_with_sources(self, texts, target_lang, system_prompt, source_lang="auto"):
        sent, prompt = self._outgoing(list(texts), system_prompt)
        if prompt is system_prompt:
            return self.inner.translate_batch_with_sources(texts, target_lang, system_prompt, source_lang=source_lang)
        reported = self.inner.translate_batch_with_sources(sent, target_lang, prompt, source_lang=source_lang)
        plain_by_sent = dict(zip(sent, texts))
        out = []
        for item in reported:
            plain = plain_by_sent.get(item.source_text, item.source_text)
            out.append(replace(item, source_text=plain,
                               translation=self._receive(plain, item.source_text, item.translation,
                                                         target_lang)))
        return out


def _line_ids(text: str) -> list[int]:
    return [int(group) for closing, group in _TAG_RE.findall(text) if not closing]


def marks_match(marked_source: str, translation: str) -> bool:
    """译文里的标记和原文一一对应。单元格的原文按段用换行拼接，译文也必须一行对一段，
    标记不能跨行。"""
    sent_lines = marked_source.split("\n")
    lines = str(translation or "").split("\n")
    if len(lines) != len(sent_lines) or not _line_ids(marked_source):
        return False
    return all(parse_marked(line, _line_ids(sent_line)) is not None for line, sent_line in zip(lines, sent_lines))


PLACE_MARKS_BATCH = 20
PLACE_MARKS_PROMPT = (
    "你是文档排版助手。下面 JSON 数组的每一项给出带 <g1>…</g1> 成对格式标记的原文和它已经定稿的译文。"
    "请在译文中插入与原文相同的标记，各包住与原文标记内容对应的译文。只插入标记，译文的其他字符"
    "（包括空格、标点、换行）一个都不能改动；每对标记恰好出现一次，不得嵌套。"
    "按原顺序返回 JSON 字符串数组，长度与输入相同，每项是插好标记的译文；不要输出解释或 markdown 代码块。"
)


def place_marks(engine, items, *, should_stop=None, log=None) -> dict[str, tuple[str, str]]:
    """给已经定稿、但没有带标记版本的译文补上标记（记忆库直接套用的、夹杂多种语言那条路
    翻的、模型弄丢标记的、复查改过的）。只让模型插标记：插完去掉标记必须和定稿一字不差，
    否则放弃，按整段写法写回。items: [(原文, 带标记原文, 定稿译文)]。"""
    import json

    from engines.base_engine import engine_supports_chat, strip_markdown_json

    placed: dict[str, tuple[str, str]] = {}
    if not items or not engine_supports_chat(engine):
        return placed
    for start in range(0, len(items), PLACE_MARKS_BATCH):
        if should_stop is not None and should_stop():
            break
        batch = items[start:start + PLACE_MARKS_BATCH]
        payload = json.dumps([{"原文": marked, "译文": translation} for _s, marked, translation in batch],
                             ensure_ascii=False)
        try:
            answers = json.loads(strip_markdown_json(engine.chat(PLACE_MARKS_PROMPT, payload)))
        except Exception as exc:  # noqa: BLE001 - 补标记失败只是退回整段写法
            if log is not None:
                log(f"段内格式补标记失败：{exc!r}")
            continue
        if not isinstance(answers, list) or len(answers) != len(batch):
            continue
        for (source, marked, translation), answer in zip(batch, answers):
            if (
                isinstance(answer, str)
                and strip_marks(answer) == translation
                and marks_match(marked, answer)
            ):
                placed[source] = (marked, answer)
    return placed
