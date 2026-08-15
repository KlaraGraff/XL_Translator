"""页眉页脚专用通道：整行送判、整行回填，行里已有的译文一个字都不许动。

页眉一行里常常挤着三样东西——中文项目名、它的法文法定译名、还没翻的中文文档名：

    贝特瑞地中海负极项目总包一标段PROJETBTRANODEMÉDITERRANÉE(Lot1)      地面裂缝修复施工方案

按普通词条翻，模型看不到「这半截已经有译名了」：要么整行重译一遍，把 PROJET BTR ANODE
MÉDITERRANÉE 换成它自己的说法——那是合同上签过字的名字，改不得；要么把已有译名连同中文
一起再译一遍，行尾挂出两份译文。

所以这条通道的规矩是：

1. 只接管「行里已经混着非源语言内容」的行。整行纯中文的页眉没有这个毛病，照旧走普通
   通道（翻完接在行尾），不进这里。
2. 先查记忆库。整行命中、或者行里只剩一处源语言残片而它命中，直接拼接，不调模型。
3. 都没命中才送模型，让它返回**整行成品**，程序整体替换这一段的文字。
4. 机器验收只认一条硬规矩：产出必须是原行「只增不改」的结果——原行的每一段字符按原顺序
   原样都还在，模型只能往缝里插内容。这条规矩不依赖任何语义判断，却足以保证法定译名
   既改不掉也删不掉。判定标准是「非源语言」而不是「目标语言」：目标语设成英文时，行里
   那截法文签署名同样一个字都不能动。
5. 验收不过就退回追加模式，用模型另外给出的「新译出的部分」接在行尾；连它也拿不到，
   就整行保持原样并留一条报告。宁可这行没翻，也不能把签署译名改了。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

from core.language_registry import get_source_lang_display, get_target_lang_display
from core.translation_protocol import REPLACE_TRANSLATION_PREFIX

# 一批送多少行。页眉页脚全篇通常只有 1~6 行，这个值实际上等于「一次装得下」，
# 留着是防某些文档每节都有不同页眉时请求数失控。
HEADER_FOOTER_BATCH_SIZE = 12

# 产出相对原行的长度上限。整行成品 = 原行 + 插进去的译文，正常不会超过两倍多一点；
# 超过说明模型在自由发挥（补背景、加说明），一律退回追加模式。
_MAX_GROWTH_RATIO = 3.0
_MAX_GROWTH_SLACK = 60

_CJK_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
_LATIN_WORD_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ]{2,}")
# 记忆库回查用的源语言残片：连续中文，允许中间夹带数字和中文标点，但不允许空格——
# 一旦允许空格，「…一标段 PROJET… 地面裂缝修复施工方案」会被并成一整条，查不到也
# 没法用。
_CJK_FRAGMENT_RE = re.compile(
    r"[一-鿿][一-鿿0-9０-９·、，．。（）()／/\-—]*[一-鿿]"
    r"|[一-鿿]"
)

RESOLUTION_TM_LINE = "tm_line"
RESOLUTION_TM_FRAGMENT = "tm_fragment"
RESOLUTION_MODEL_REPLACE = "model_replace"
RESOLUTION_MODEL_APPEND = "model_append"
RESOLUTION_UNRESOLVED = "unresolved"


@dataclass
class HeaderFooterResolution:
    """一行页眉页脚的最终处置。"""

    source: str
    translation: str = ""
    replace_line: bool = False
    resolution: str = RESOLUTION_UNRESOLVED
    reject_reason: str = ""

    @property
    def resolved(self) -> bool:
        return bool(self.translation)

    def as_translation_value(self) -> str:
        """转成写入器认识的形式：整体替换要带标记，追加是纯译文。"""
        if self.replace_line:
            return f"{REPLACE_TRANSLATION_PREFIX}{self.translation}"
        return self.translation


@dataclass
class HeaderFooterOutcome:
    resolutions: list[HeaderFooterResolution] = field(default_factory=list)
    model_line_count: int = 0

    @property
    def translations(self) -> dict[str, str]:
        return {
            item.source: item.as_translation_value()
            for item in self.resolutions
            if item.resolved
        }

    @property
    def rejected(self) -> list[HeaderFooterResolution]:
        return [item for item in self.resolutions if item.reject_reason]


def line_has_foreign_content(text: str, *, source_lang: str) -> bool:
    """这一行是不是已经混着非源语言的实质内容。

    只有这种行才需要专用通道：纯源语言的页眉直接翻完接在后面就行，没有「别动已有
    译名」的问题，多绕一圈只是多花钱。
    """
    value = str(text or "")
    if not value.strip():
        return False
    if str(source_lang or "").strip().lower() == "zh":
        return bool(_LATIN_WORD_RE.search(value))
    # 源语言不是中文时，「非源语言」没法靠字符集分辨——拉丁字母两边都在用。这条通道
    # 只在中文源文下接管，其余照旧走普通通道，不做没把握的判定。
    return False


def source_fragments(text: str, *, source_lang: str) -> list[str]:
    """行内的源语言残片，按出现顺序，用于回查记忆库。"""
    if str(source_lang or "").strip().lower() != "zh":
        return []
    return [match.group(0) for match in _CJK_FRAGMENT_RE.finditer(str(text or ""))]


def _script_class(char: str) -> str:
    if char.isspace():
        return "space"
    if _CJK_RE.match(char):
        return "cjk"
    return "other"


def _preserved_token_spans(text: str) -> list[tuple[int, int]]:
    """把原行切成「同一书写体系的连续片段」。

    切分点是空白，以及中文↔非中文的交界——后者不能少：真实页眉里「…一标段」和
    「PROJETBTRANODE…」中间一个空格都没有，不按书写体系切开的话，模型想在这两截
    之间插译文就会被判成「改动了原文」。
    """
    value = str(text or "")
    spans: list[tuple[int, int]] = []
    start = -1
    current_class = ""
    for index, char in enumerate(value):
        klass = _script_class(char)
        if klass == "space":
            if start >= 0:
                spans.append((start, index))
            start = -1
            current_class = ""
            continue
        if klass != current_class and start >= 0:
            spans.append((start, index))
            start = -1
        if start < 0:
            start = index
        current_class = klass
    if start >= 0:
        spans.append((start, len(value)))
    # 纯标点的片段不参与校验：它们既不承载信息，又常被模型顺手调整（半角括号改全角），
    # 为这个把整行判死不值得。真正要护住的是带字母、数字、汉字的片段。
    return [
        (begin, end)
        for begin, end in spans
        if any(char.isalnum() or _CJK_RE.match(char) for char in value[begin:end])
    ]


def _preserved_tokens(text: str) -> list[str]:
    value = str(text or "")
    return [value[begin:end] for begin, end in _preserved_token_spans(value)]


def _locate_tokens(
    original: str,
    produced: str,
) -> tuple[list[tuple[int, int]], list[tuple[int, int]], str]:
    """在产出里按顺序找回原行的每个片段，找不到就是「改动了原文」。"""
    origin_spans = _preserved_token_spans(original)
    produced_spans: list[tuple[int, int]] = []
    cursor = 0
    for begin, end in origin_spans:
        token = original[begin:end]
        found = produced.find(token, cursor)
        if found < 0:
            return [], [], f"原行片段「{token}」在整行成品里被改写或删掉了"
        produced_spans.append((found, found + len(token)))
        cursor = found + len(token)
    return origin_spans, produced_spans, ""


def verify_insert_only(original: str, produced: str) -> tuple[bool, str]:
    """产出是不是「只往原行里插内容」的结果。

    这是整条通道唯一的验收标准，也是它敢做整体替换的全部底气：原行的每个片段按原顺序
    原样都在，模型就删不掉也改不了任何一个已有译名，程序不需要去判断「这段法文译得对
    不对」——那种判断做不准，做错的代价是改掉合同上的名字。
    """
    source = str(original or "")
    result = str(produced or "").strip()
    if not result:
        return False, "模型没有返回整行内容"
    if "\n" in result or "\r" in result:
        return False, "整行成品里出现了换行，页眉高度是固定的"

    source_len = len("".join(source.split()))
    result_len = len("".join(result.split()))
    if result_len > source_len * _MAX_GROWTH_RATIO + _MAX_GROWTH_SLACK:
        return False, "整行成品比原行长出太多，疑似自由发挥"

    _, _, reason = _locate_tokens(source, result)
    return (False, reason) if reason else (True, "")


def _gap_insertion(produced_gap: str, origin_gap: str) -> str:
    """模型在这道缝里多写了什么（原行本来就有的那部分不算）。"""
    added = str(produced_gap or "").strip()
    if not added:
        return ""
    origin_core = str(origin_gap or "").strip()
    if origin_core and origin_core in added:
        added = added.replace(origin_core, "", 1).strip()
    if not added or added == origin_core:
        return ""
    return added


def compose_replacement(original: str, produced: str) -> tuple[str, str]:
    """验收通过后，把模型插进去的内容拼回**原行**，返回 (整行成品, 拒收理由)。

    不直接用模型返回的那一行，是因为它会顺手把原行的排版空格压掉——真实页眉靠一长串
    空格把文档名推到右边，压成一个空格整行就跑位了。这里只取模型「多写出来的那几段」，
    其余每一个字符都从原行原样取，原行因此是逐字节不变的，不只是「看起来没变」。
    """
    source = str(original or "")
    result = str(produced or "").strip()
    ok, reason = verify_insert_only(source, result)
    if not ok:
        return "", reason

    origin_spans, produced_spans, _ = _locate_tokens(source, result)
    parts: list[str] = []

    def _append(text: str) -> None:
        if not text:
            return
        joined = "".join(parts)
        if joined and not joined[-1].isspace() and not text[:1].isspace():
            parts.append(" ")
        parts.append(text)

    prev_origin = 0
    prev_produced = 0
    for (origin_begin, origin_end), (produced_begin, produced_end) in zip(
        origin_spans, produced_spans
    ):
        origin_gap = source[prev_origin:origin_begin]
        # 译文紧跟在它翻译的那一段后面，所以插在原行空白之前——空白留给它和下一段做间隔。
        _append(_gap_insertion(result[prev_produced:produced_begin], origin_gap))
        parts.append(origin_gap)
        parts.append(source[origin_begin:origin_end])
        prev_origin, prev_produced = origin_end, produced_end

    tail_gap = source[prev_origin:]
    parts.append(tail_gap)
    _append(_gap_insertion(result[prev_produced:], tail_gap))
    return "".join(parts), ""


def build_header_footer_prompt(
    *,
    source_lang: str,
    target_lang: str,
    extra_instructions: str = "",
) -> str:
    """页眉页脚专用系统提示词。

    和正文提示词分开，因为要求正好相反：正文是「把这段翻译出来」，这里是「这一行绝大
    部分都别动，只把还没译的那几个字补进去」。
    """
    source_name = get_source_lang_display(source_lang)
    target_name = get_target_lang_display(target_lang, include_optional=True)
    lines = [
        "你在处理工程与合同文件的页眉页脚。页眉页脚常常一行里既有原文，也有早就写定的译文。",
        f"源语言：{source_name}。需要补出的译文语言：{target_name}。",
        "",
        "铁律（违反任何一条，本次输出作废）：",
        "1. 原行里已经存在的内容，一个字符都不许改、不许删、不许调整顺序、不许改大小写或空格。"
        "包括你认为拼写有误、缺空格、译得不准、用词不统一的地方——照原样留着。",
        "2. 行里凡是不属于源语言的内容（例如外文的单位名、项目名、法定译名、合同签署译名、"
        "编号、缩写），一律视为已经定稿的译文，即使它和字面翻译对不上，也原样保留、位置不动，"
        "不要重译、不要润色、不要替换成你觉得更好的说法。",
        "3. 你唯一能做的事是：把行里还没有对应译文的源语言部分译出来，插在它的紧后面。",
        "4. 已经有对应译文的源语言部分，不要再译一遍。",
        "5. 输出必须是一行，不能出现换行符。",
        "6. 数字、编号、日期、标段号原样保留。",
        "",
        "输入是 JSON 数组，每项有 id 和 line。对每一项输出：",
        'line —— 处理后的整行成品（= 原行 + 你插进去的译文）；',
        'added —— 只包含你这次新译出来的文字，不含原行任何内容；如果整行都已经有译文、'
        "无需补充，line 原样返回、added 留空字符串。",
        "",
        '只输出一个 JSON 数组，不要 markdown、不要解释。格式：'
        '[{"id":0,"line":"...","added":"..."}]',
    ]
    extra = str(extra_instructions or "").strip()
    if extra:
        lines.extend(["", "补充要求（不得与上面的铁律冲突）：", extra])
    return "\n".join(lines)


def build_header_footer_payload(sources: Sequence[str]) -> str:
    return json.dumps(
        [{"id": index, "line": text} for index, text in enumerate(sources)],
        ensure_ascii=False,
    )


def parse_header_footer_response(raw: str, count: int) -> list[tuple[str, str]]:
    """解析模型返回，按 id 对齐成 (整行成品, 新增译文)。

    对不上的、缺项的、类型不对的，一律返回空串——上层会当成「这行没结果」退回原样，
    绝不拿位置去猜 id：页眉一行错位就是把 A 文档的名字写到 B 文档上。
    """
    slots: list[tuple[str, str]] = [("", "")] * max(0, int(count))
    try:
        payload = json.loads(raw)
    except Exception:  # noqa: BLE001 - 解析不了就是没结果
        return slots
    if not isinstance(payload, list):
        return slots
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("id"))
        except Exception:  # noqa: BLE001
            continue
        if not 0 <= index < len(slots):
            continue
        line = item.get("line")
        added = item.get("added")
        slots[index] = (
            str(line).strip() if isinstance(line, str) else "",
            str(added).strip() if isinstance(added, str) else "",
        )
    return slots


def resolve_header_footer_lines(
    sources: Iterable[str],
    *,
    source_lang: str,
    target_lang: str,
    tm_lookup: Callable[[list[str]], dict[str, str]] | None = None,
    ask_model: Callable[[list[str]], list[tuple[str, str]]] | None = None,
    batch_size: int = HEADER_FOOTER_BATCH_SIZE,
) -> HeaderFooterOutcome:
    """按记忆库 → 模型 → 机器验收的顺序，给每一行定一个处置。

    ``ask_model(lines)`` 收一批原行，返回等长的 (整行成品, 新增译文)；给 None 表示
    没有模型可用（本地引擎、没配 key），此时只走记忆库那一段。
    """
    lines = [str(text or "").strip() for text in sources]
    lines = [text for text in lines if text]
    outcome = HeaderFooterOutcome()
    if not lines:
        return outcome

    known: dict[str, str] = {}
    if tm_lookup is not None:
        probes: list[str] = []
        for line in lines:
            probes.append(line)
            probes.extend(source_fragments(line, source_lang=source_lang))
        try:
            known = {
                str(key): str(value)
                for key, value in (tm_lookup(list(dict.fromkeys(probes))) or {}).items()
                if str(value or "").strip()
            }
        except Exception:  # noqa: BLE001 - 记忆库只是省一次模型调用，坏了不该拦住翻译
            known = {}

    pending: list[str] = []
    for line in lines:
        hit = known.get(line)
        if hit:
            outcome.resolutions.append(
                HeaderFooterResolution(
                    source=line,
                    translation=hit,
                    resolution=RESOLUTION_TM_LINE,
                )
            )
            continue
        fragments = [
            fragment
            for fragment in source_fragments(line, source_lang=source_lang)
            if fragment != line
        ]
        # 只在「行里只剩一处源语言残片」时才用残片的译名拼接。两处以上时程序无从判断
        # 哪一处才是缺译文的那一处，硬拼会把译文接错位置，交给模型看整行。
        if len(fragments) == 1 and known.get(fragments[0]):
            outcome.resolutions.append(
                HeaderFooterResolution(
                    source=line,
                    translation=known[fragments[0]],
                    resolution=RESOLUTION_TM_FRAGMENT,
                )
            )
            continue
        pending.append(line)

    if not pending or ask_model is None:
        outcome.resolutions.extend(
            HeaderFooterResolution(
                source=line,
                resolution=RESOLUTION_UNRESOLVED,
                reject_reason="没有可用的模型通道" if ask_model is None else "",
            )
            for line in pending
        )
        return outcome

    size = max(1, int(batch_size or HEADER_FOOTER_BATCH_SIZE))
    for start in range(0, len(pending), size):
        batch = pending[start : start + size]
        outcome.model_line_count += len(batch)
        try:
            answers = ask_model(batch)
        except Exception:  # noqa: BLE001 - 由调用方决定要不要记日志；这里只保证不丢行
            answers = []
        if len(answers) != len(batch):
            answers = list(answers) + [("", "")] * (len(batch) - len(answers))
        for line, (produced, added) in zip(batch, answers[: len(batch)]):
            outcome.resolutions.append(
                _decide(line, produced, added)
            )
    return outcome


def _decide(line: str, produced: str, added: str) -> HeaderFooterResolution:
    composed, reason = compose_replacement(line, produced)
    if composed:
        # 模型判断整行都已经有译文时会原样退回来，这时候没有任何东西要写。
        if composed == line:
            return HeaderFooterResolution(
                source=line,
                resolution=RESOLUTION_UNRESOLVED,
            )
        return HeaderFooterResolution(
            source=line,
            translation=composed,
            replace_line=True,
            resolution=RESOLUTION_MODEL_REPLACE,
        )
    fallback = str(added or "").strip()
    if fallback and "\n" not in fallback:
        return HeaderFooterResolution(
            source=line,
            translation=fallback,
            resolution=RESOLUTION_MODEL_APPEND,
            reject_reason=reason,
        )
    return HeaderFooterResolution(
        source=line,
        resolution=RESOLUTION_UNRESOLVED,
        reject_reason=reason or "模型没有返回可用结果",
    )
