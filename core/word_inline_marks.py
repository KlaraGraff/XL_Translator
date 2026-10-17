"""Word 段内局部格式：用标记随原文发给模型，译完按标记还原。

整段翻译时译文写进一个 run，段内的局部格式（「注意：」加粗、句中某个词斜体、带链接
的文字、m² 的上标）就合成了一种。这里沿用专业翻译软件和 DeepL、BabelDOC 的做法：
段落里格式和主体不同的那几截用 ``<g1>…</g1>`` 包住随原文发出，模型把标记放到对应
的译文上，写回时每截按原来的格式（和链接）各写一个 run。

段里的制表符、软换行、行内图片、脚注编号这类不能翻译的东西，写成自闭合的占位符
``<x1/>`` 随原文发出，模型把它放到译文里对应的位置，写回时那里原样放回原来的节点
（「1.2⇥**施工范围**见附图」的制表符和加粗都保得住）。段首段尾的占位符不发给模型，
写回时固定留在段首段尾。占位符去掉标记后还原成纯文本里对应的字符（制表符 ``\t``、
换行 ``\n``，图片和脚注编号为空），下游看到的纯文本和以前一样。

标记的写法和校验与 PPT、Excel 共用：成对的 ``<gN>…</gN>`` 管格式，``<xN/>`` 管占位，
两套编号各自从 1 起。

保底：只有带局部格式、或段中间有占位符，且结构简单（只有文字 run、超链接和上面这些
占位内容，没有域、修订）的段落才加标记；译文里的标记少了、多了、乱了，就去掉标记、
按现在的整段写法写回，不报错。去掉标记后的纯文本照常走校验、记忆库和复核，和以前
完全一样。
"""
from __future__ import annotations

import re
import threading
from copy import deepcopy
from dataclasses import dataclass, field, replace

from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from core.prompt_cache import append_batch_block

# 一段里最多加几对标记；再多说明格式很碎，模型放错的概率也高，按整段写法处理
MAX_GROUPS_PER_UNIT = 12
# 一段里最多几个占位符（相邻的已并成一个）；用制表符排成几列的段落常有七八个
MAX_PLACEHOLDERS_PER_UNIT = 16

_TAG_RE = re.compile(r"<\s*(/?)\s*g\s*(\d+)\s*>", re.IGNORECASE)
_SLOT_RE = re.compile(r"<\s*x\s*(\d+)\s*/\s*>", re.IGNORECASE)
_ANY_MARK_RE = re.compile(r"<\s*(/?)\s*g\s*(\d+)\s*>|<\s*x\s*(\d+)\s*/\s*>", re.IGNORECASE)

MARK_PROMPT_RULE = (
    "部分词条带有 <g1>…</g1> 这样成对的格式标记，标出原文中加粗、斜体、链接等局部格式。"
    "译文必须保留每一对标记，各包住与原文标记内容对应的译文；标记位置可以随译文语序调整，"
    "但不得增加、删除、合并、嵌套或改动标记。标记两侧的空格按译文语言的习惯书写，"
    "如「<g1>Note :</g1> les cotes…」冒号后照常留空格。"
    "部分词条还带有 <x1/> 这样的自闭合占位符，代表制表符、换行、图片、脚注编号等不翻译的内容："
    "每个占位符在译文中必须原样出现恰好一次，放在译文里对应的位置，可以在标记里面，"
    "不得删除、复制或改写。"
)

_PARAGRAPH_CHILDREN = frozenset(
    qn(tag) for tag in ("w:pPr", "w:r", "w:hyperlink", "w:bookmarkStart", "w:bookmarkEnd", "w:proofErr",
                        "w:permStart", "w:permEnd")
)
_HYPERLINK_CHILDREN = frozenset((qn("w:r"), qn("w:proofErr")))
_RUN_CHILDREN = frozenset((qn("w:rPr"), qn("w:t"), qn("w:lastRenderedPageBreak")))
# run 里排在文字中间、但不是文字的东西，作为占位符随原文发出、写回时照抄原节点。
# 版式类（制表符、软换行、不断行连字符、符号）在双语版式新插的译文段里照样复制一份；
_LAYOUT_SLOT_TAGS = frozenset(
    qn(tag) for tag in ("w:tab", "w:br", "w:cr", "w:ptab", "w:noBreakHyphen", "w:softHyphen", "w:sym")
)
# 对象类（图片、嵌入对象、文本框、脚注/尾注/批注的引用）只能有一份，双语的译文段里不复制
_OBJECT_SLOT_TAGS = frozenset(
    (
        *(qn(tag) for tag in ("w:drawing", "w:pict", "w:object", "w:footnoteReference", "w:endnoteReference",
                              "w:footnoteRef", "w:endnoteRef", "w:commentReference", "w:annotationRef")),
        "{http://schemas.openxmlformats.org/markup-compatibility/2006}AlternateContent",
    )
)
_SLOT_TAGS = _LAYOUT_SLOT_TAGS | _OBJECT_SLOT_TAGS
# python-docx 的 Run.text 把这几种读成字符（见 docx.oxml.text.run.CT_R.text），其余读成空
_TEXT_EQUIVALENT_TAGS = frozenset(qn(tag) for tag in ("w:tab", "w:br", "w:cr", "w:ptab", "w:noBreakHyphen"))
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


@dataclass(frozen=True)
class Slot:
    """译文里的一个占位符 ``<xN/>``（parse_marked 拆出来的）。"""

    id: int


@dataclass(frozen=True)
class Placeholder:
    """原文里一处占位内容：一个或几个相邻的节点，各带所在 run 的 rPr，写回时原样放回。"""

    nodes: tuple  # ((节点, rPr 或 None), ...)
    text: str  # 在纯文本里对应的字符

    @property
    def layout_only(self) -> bool:
        """只有制表符、软换行这类版式（不含分页、分栏），双语的译文段里可以再复制一份。"""
        return all(
            node.tag in _LAYOUT_SLOT_TAGS
            and not (node.tag == qn("w:br") and (node.get(qn("w:type")) or "textWrapping") != "textWrapping")
            for node, _rpr in self.nodes
        )


@dataclass
class ParagraphMarks:
    """一段（或单元格里的一段）带标记的原文，以及每对标记对应的格式、每个占位符对应的节点。"""

    marked_text: str
    plain_text: str
    groups: dict[int, MarkGroup] = field(default_factory=dict)
    base_rpr: object | None = None
    # 段中间的占位符（随原文发出）
    placeholders: dict[int, Placeholder] = field(default_factory=dict)
    # 段首、段尾不发给模型、写回时固定放回的内容：[(文字或 Placeholder, None)]
    lead: list = field(default_factory=list)
    trail: list = field(default_factory=list)

    @property
    def is_marked(self) -> bool:
        return bool(self.groups or self.placeholders)

    @property
    def placeholder_texts(self) -> dict[int, str]:
        return {key: item.text for key, item in self.placeholders.items()}

    def pieces_for(self, translation: str):
        """带标记的译文拆成可写回的片段（占位符换成 Placeholder，前后接上段首段尾）；
        标记对不上返回 None。"""
        pieces = parse_marked(translation, self.groups, self.placeholders)
        if pieces is None:
            return None
        resolved = [(self.placeholders[text.id] if isinstance(text, Slot) else text, group) for text, group in pieces]
        return [*self.lead, *resolved, *self.trail]


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

    @property
    def is_marked(self) -> bool:
        return any(item.is_marked for item in self.paragraphs)

    @property
    def plain_text(self) -> str:
        return "\n".join(item.plain_text for item in self.paragraphs)

    @property
    def placeholder_texts(self) -> dict[int, str]:
        merged: dict[int, str] = {}
        for item in self.paragraphs:
            merged.update(item.placeholder_texts)
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


@dataclass
class _Item:
    """段落里按顺序的一截：文字，或（node_pairs 非空时）一个占位符。"""

    text: str
    rpr: object | None
    hyperlink: object | None
    node_pairs: list = field(default_factory=list)

    @property
    def is_slot(self) -> bool:
        return bool(self.node_pairs)


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
        if any(not isinstance(node.tag, str) or node.tag not in _RUN_CHILDREN | _SLOT_TAGS for node in run):
            return None
    return runs


def _content_items(runs) -> list[_Item]:
    """文字一截一项；相邻的占位内容（中间没有文字）并成一项。"""
    items: list[_Item] = []
    for run, hyperlink in runs:
        rpr = run.find(qn("w:rPr"))
        for node in run:
            if node.tag == qn("w:t"):
                if node.text:
                    items.append(_Item(node.text, rpr, hyperlink))
            elif node.tag in _SLOT_TAGS:
                text = str(node) if node.tag in _TEXT_EQUIVALENT_TAGS else ""
                if items and items[-1].is_slot:
                    items[-1].text += text
                    items[-1].node_pairs.append((node, rpr))
                else:
                    items.append(_Item(text, rpr, hyperlink, [(node, rpr)]))
    return items


def paragraph_marks(paragraph, *, start_id: int = 1, start_slot: int = 1) -> ParagraphMarks | None:
    """这一段能不能加标记：能就返回（is_marked 为 False 表示格式统一、中间也没有占位内容，
    不需要标记），不能返回 None。"""
    runs = _content_runs(paragraph._p)
    if runs is None:
        return None
    items = _content_items(runs)
    plain_full = "".join(item.text for item in items)
    plain = plain_full.strip()
    if (
        not plain
        or plain != (paragraph.text or "").strip()
        or _TAG_RE.search(plain)
        or _SLOT_RE.search(plain)
    ):
        return None

    looks = [None if item.is_slot else _look_key(item.rpr) for item in items]
    weights: dict[tuple, int] = {}
    for item, look in zip(items, looks):
        if look is not None:
            weights[look] = weights.get(look, 0) + len(item.text.strip())
    first_index = {}
    for index, look in enumerate(looks):
        if look is not None:
            first_index.setdefault(look, index)
    base_look = max(weights, key=lambda look: (weights[look], -first_index[look]))
    base_rpr = items[first_index[base_look]].rpr

    # 每一截归到哪一组：和主体格式一样、又不在链接里的不加标记。只有空白的那截和占位符
    # 夹在同一组中间就并进去，否则归主体
    keys: list[tuple | None] = []
    soft: list[bool] = []
    for item, look in zip(items, looks):
        is_soft = item.is_slot or (not item.text.strip() and item.hyperlink is None)
        soft.append(is_soft)
        if is_soft or (item.hyperlink is None and look == base_look):
            keys.append(None)
        else:
            keys.append((look, id(item.hyperlink) if item.hyperlink is not None else 0))
    for index, is_soft in enumerate(soft):
        if not is_soft:
            continue
        before = next((keys[i] for i in range(index - 1, -1, -1) if not soft[i]), None)
        after = next((keys[i] for i in range(index + 1, len(items)) if not soft[i]), None)
        keys[index] = before if before is not None and before == after else None

    # 去掉段首段尾的空白（和 paragraph.text.strip() 对齐）。段首段尾的空白和占位内容
    # 不发给模型，写回时固定放回段首段尾（段尾的空白文字照旧不留）
    lead_len = len(plain_full) - len(plain_full.lstrip())
    body_end = len(plain_full) - (len(plain_full) - len(plain_full.rstrip()))
    lead: list = []
    trail: list = []
    body: list[tuple[_Item, str, tuple | None]] = []
    offset = 0
    for item, key in zip(items, keys):
        start, end = offset, offset + len(item.text)
        offset = end
        if item.is_slot:
            if end <= lead_len:
                lead.append(item)
            elif start >= body_end:
                trail.append(item)
            elif start >= lead_len and end <= body_end:
                body.append((item, item.text, key))
            else:
                return None  # 占位内容跨在段首段尾的空白上，读不准，按整段写法
            continue
        if start < lead_len:
            lead.append(_Item(plain_full[start:min(end, lead_len)], item.rpr, None))
        lo, hi = max(start, lead_len), min(end, body_end)
        if lo < hi:
            body.append((item, plain_full[lo:hi], key))

    groups: dict[int, MarkGroup] = {}
    placeholders: dict[int, Placeholder] = {}
    parts: list[str] = []
    next_id, next_slot = start_id, start_slot
    current_key: tuple | None = None
    for item, text, key in body:
        if key != current_key:
            if current_key is not None:
                parts.append(f"</g{next_id - 1}>")
            if key is not None:
                rpr = item.node_pairs[0][1] if item.is_slot else item.rpr
                groups[next_id] = MarkGroup(rpr=rpr, hyperlink=item.hyperlink)
                parts.append(f"<g{next_id}>")
                next_id += 1
            current_key = key
        if item.is_slot:
            placeholders[next_slot] = Placeholder(nodes=tuple(item.node_pairs), text=item.text)
            parts.append(f"<x{next_slot}/>")
            next_slot += 1
        else:
            parts.append(text)
    if current_key is not None:
        parts.append(f"</g{next_id - 1}>")
    if len(groups) > MAX_GROUPS_PER_UNIT or len(placeholders) > MAX_PLACEHOLDERS_PER_UNIT:
        return None
    marked = "".join(parts)
    texts = {key: item.text for key, item in placeholders.items()}
    if strip_marks(marked, texts) != plain or placeholder_texts(marked, plain) != texts:
        return None
    return ParagraphMarks(
        marked_text=marked if groups or placeholders else plain,
        plain_text=plain,
        groups=groups,
        base_rpr=base_rpr,
        placeholders=placeholders,
        lead=[_fixed_piece(item) for item in lead],
        trail=[_fixed_piece(item) for item in trail if item.is_slot],
    )


def _fixed_piece(item: _Item):
    if item.is_slot:
        return (Placeholder(nodes=tuple(item.node_pairs), text=item.text), None)
    return (item.text, None)


def cell_marks(paragraphs: list) -> CellMarks | None:
    """单元格：各段原文用换行拼起来（同 word_document._cell_source_text），标记和占位符的
    编号跨段连续。"""
    items: list[ParagraphMarks] = []
    next_id = next_slot = 1
    for paragraph in paragraphs:
        marks = paragraph_marks(paragraph, start_id=next_id, start_slot=next_slot)
        if marks is None:
            return None
        items.append(marks)
        next_id += len(marks.groups)
        next_slot += len(marks.placeholders)
    if (
        not items
        or not any(item.is_marked for item in items)
        or next_id - 1 > MAX_GROUPS_PER_UNIT
        or next_slot - 1 > MAX_PLACEHOLDERS_PER_UNIT
    ):
        return None
    return CellMarks(marked_text="\n".join(item.marked_text for item in items), paragraphs=items)


def strip_marks(text: str, placeholders: dict[int, str] | None = None) -> str:
    """去掉格式标记；占位符换回它在纯文本里对应的字符（placeholders 里没有的换成空）。"""
    texts = placeholders or {}
    without_tags = _TAG_RE.sub("", str(text or ""))
    return _SLOT_RE.sub(lambda match: texts.get(int(match.group(1)), ""), without_tags)


def placeholder_texts(marked_source: str, plain_source: str) -> dict[int, str] | None:
    """由带标记原文和纯文本原文反推每个占位符对应的字符；对不上返回 None。

    只凭两段字符串就能还原，扩展通道（只传字符串）也能用。生成时相邻的占位内容已并成
    一个、占位符之间至少隔着一个字，再加上 paragraph_marks 生成时核对过，不会有歧义。"""
    text = _TAG_RE.sub("", str(marked_source or ""))
    parts = _SLOT_RE.split(text)
    if len(parts) == 1:
        return {}
    ids = [int(value) for value in parts[1::2]]
    if len(set(ids)) != len(ids):
        return None
    pattern = "".join(re.escape(part) if index % 2 == 0 else "(.*?)" for index, part in enumerate(parts))
    match = re.fullmatch(pattern, str(plain_source or ""), re.DOTALL)
    if match is None:
        return None
    return dict(zip(ids, match.groups()))


def unmark(translation: str, marked_source: str, plain_source: str) -> str:
    """带标记的译文 → 纯文本译文（占位符按原文换回制表符、换行等）。"""
    return strip_marks(translation, placeholder_texts(marked_source, plain_source))


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


def parse_marked(text: str, allowed_ids, placeholder_ids=()) -> list[tuple[str | Slot, int | None]] | None:
    """把带标记的译文拆成 [(文字或 Slot, 组号或 None)]。每个组号必须恰好一开一闭、不嵌套，
    每个占位符恰好出现一次，组号和占位符的集合都和原文完全一致；否则返回 None。"""
    allowed = set(allowed_ids)
    slots_allowed = set(placeholder_ids)
    pieces: list[tuple[str | Slot, int | None]] = []
    seen: set[int] = set()
    seen_slots: set[int] = set()
    current: int | None = None
    position = 0
    for match in _ANY_MARK_RE.finditer(text):
        chunk = text[position:match.start()]
        if chunk:
            pieces.append((chunk, current))
        position = match.end()
        if match.group(3) is not None:
            slot = int(match.group(3))
            if slot not in slots_allowed or slot in seen_slots:
                return None
            seen_slots.add(slot)
            pieces.append((Slot(slot), current))
            continue
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
    if current is not None or seen != allowed or seen_slots != slots_allowed:
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


def _placeholder_runs(placeholder: Placeholder, *, move: bool) -> list:
    """占位内容按原来的 run 格式各放进一个 w:r。move=True 时挪用原节点（原段落随后整段
    换掉，挪用比复制稳妥：文本框里的文字、图片的编号都还是原来那一份）。"""
    runs = []
    for node, rpr in placeholder.nodes:
        run = OxmlElement("w:r")
        if rpr is not None:
            run.append(deepcopy(rpr))
        run.append(node if move else deepcopy(node))
        runs.append(run)
    return runs


def _without_objects(pieces) -> list:
    """去掉图片、脚注编号这类占位内容；它两边的空格并成一个，后面紧跟标点的去掉。"""
    out: list = []
    dropped = False
    for text, group in pieces:
        if isinstance(text, Placeholder) and not text.layout_only:
            dropped = True
            continue
        if dropped and isinstance(text, str) and out and isinstance(out[-1][0], str) and out[-1][0].endswith(" "):
            if text.startswith(" ") or text[:1] in ",.;)]»":
                out[-1] = (out[-1][0][:-1], out[-1][1])
        dropped = False
        out.append((text, group))
    return out


def build_run_nodes(pieces, groups: dict[int, MarkGroup], base_rpr, *, keep_objects: bool = True) -> list:
    """按拆好的译文生成 w:r（链接那截包在 w:hyperlink 里，相邻同一个链接的并在一起），
    返回要插进段落的节点。片段里的 Placeholder 照抄原节点；keep_objects=False（双语版式
    新插的译文段）时只复制制表符、换行这类版式，图片、脚注编号留在原文段里。"""
    nodes = []
    last_link: tuple[object, object] | None = None  # (原链接, 新建的 w:hyperlink)
    if not keep_objects:
        pieces = _without_objects(pieces)
    for text, group in pieces:
        if isinstance(text, Placeholder):
            if not keep_objects and not text.layout_only:
                continue
            runs = _placeholder_runs(text, move=keep_objects)
        elif text:
            runs = [_new_run(text, base_rpr if group is None else groups[group].rpr)]
        else:
            continue
        source_link = groups[group].hyperlink if group is not None else None
        if source_link is None:
            nodes.extend(runs)
            last_link = None
            continue
        if last_link is None or last_link[0] is not source_link or nodes[-1] is not last_link[1]:
            link = OxmlElement("w:hyperlink")
            for key, value in source_link.attrib.items():
                link.set(key, value)
            nodes.append(link)
            last_link = (source_link, link)
        for run in runs:
            last_link[1].append(run)
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
    """新写的文字 run（占位内容的 run 照抄原样，不在其中）。"""
    for node in nodes:
        runs = node.iterchildren(qn("w:r")) if node.tag == qn("w:hyperlink") else (node,)
        for run in runs:
            if run.find(qn("w:t")) is not None:
                yield run


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
        return sent, append_batch_block(system_prompt, MARK_PROMPT_RULE)

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
        return unmark(translation, sent, plain)

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


def _line_slots(text: str) -> list[int]:
    return [int(slot) for slot in _SLOT_RE.findall(text)]


def marks_match(marked_source: str, translation: str) -> bool:
    """译文里的标记和占位符与原文一一对应。单元格的原文按段用换行拼接，译文也必须一行
    对一段，标记和占位符不能跨行。"""
    sent_lines = marked_source.split("\n")
    lines = str(translation or "").split("\n")
    if len(lines) != len(sent_lines) or not (_line_ids(marked_source) or _line_slots(marked_source)):
        return False
    return all(
        parse_marked(line, _line_ids(sent_line), _line_slots(sent_line)) is not None
        for line, sent_line in zip(lines, sent_lines)
    )


PLACE_MARKS_BATCH = 20
PLACE_MARKS_PROMPT = (
    "你是文档排版助手。下面 JSON 数组的每一项给出带 <g1>…</g1> 成对格式标记的原文和它已经定稿的译文。"
    "请在译文中插入与原文相同的标记，各包住与原文标记内容对应的译文。只插入标记，译文的其他字符"
    "（包括空格、标点、换行）一个都不能改动；每对标记恰好出现一次，不得嵌套。"
    "原文里 <x1/> 这样的自闭合占位符（制表符、图片、脚注编号等）在译文中也必须恰好出现一次："
    "译文里已经有的保持原位，没有的插到对应的位置。"
    "按原顺序返回 JSON 字符串数组，长度与输入相同，每项是插好标记的译文；不要输出解释或 markdown 代码块。"
)


def _seed_slots(marked: str, translation: str, texts: dict[int, str]) -> str:
    """定稿译文里制表符、换行这类有字符的占位内容，按原文顺序先换成占位符，模型只需补
    图片、脚注编号和格式标记。按顺序找不齐就原样交给模型。"""
    seeded, cursor = [], 0
    for slot in _line_slots(marked):
        text = texts.get(slot, "")
        if not text:
            continue
        found = translation.find(text, cursor)
        if found < 0:
            return translation
        seeded.append(translation[cursor:found])
        seeded.append(f"<x{slot}/>")
        cursor = found + len(text)
    seeded.append(translation[cursor:])
    return "".join(seeded)


def place_marks(engine, items, *, should_stop=None, log=None) -> dict[str, tuple[str, str]]:
    """给已经定稿、但没有带标记版本的译文补上标记（记忆库直接套用的、夹杂多种语言那条路
    翻的、模型弄丢标记的、复查改过的）。只让模型插标记：插完去掉标记必须和定稿一字不差，
    否则放弃，按整段写法写回。items: [(原文, 带标记原文, 定稿译文)]。"""
    import json

    from engines.base_engine import engine_supports_chat, strip_markdown_json

    placed: dict[str, tuple[str, str]] = {}
    if not items:
        return placed
    pending = []
    for source, marked, translation in items:
        texts = placeholder_texts(marked, source)
        if texts is None:
            continue
        seeded = _seed_slots(marked, translation, texts)
        if not _TAG_RE.search(marked) and marks_match(marked, seeded):
            # 只有制表符、换行这类占位、按顺序都找到了：不必问模型
            placed[source] = (marked, seeded)
            continue
        pending.append((source, marked, translation, seeded, texts))
    if not pending or not engine_supports_chat(engine):
        return placed
    for start in range(0, len(pending), PLACE_MARKS_BATCH):
        if should_stop is not None and should_stop():
            break
        batch = pending[start:start + PLACE_MARKS_BATCH]
        payload = json.dumps([{"原文": marked, "译文": seeded} for _s, marked, _t, seeded, _x in batch],
                             ensure_ascii=False)
        try:
            answers = json.loads(strip_markdown_json(engine.chat(PLACE_MARKS_PROMPT, payload)))
        except Exception as exc:  # noqa: BLE001 - 补标记失败只是退回整段写法
            if log is not None:
                log(f"段内格式补标记失败：{exc!r}")
            continue
        if not isinstance(answers, list) or len(answers) != len(batch):
            continue
        for (source, marked, translation, _seeded, texts), answer in zip(batch, answers):
            if (
                isinstance(answer, str)
                and strip_marks(answer, texts) == translation
                and marks_match(marked, answer)
            ):
                placed[source] = (marked, answer)
    return placed
