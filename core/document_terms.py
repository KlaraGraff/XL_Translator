"""本文档统一用词：翻译前找出在多处反复出现的词，请模型给每个词定一个译法。

一份文档分成几十上百批同时翻译，各批互相看不到别的批怎么译。法语里有好几种
合理说法的行业词，就会前后不一：实测一份土方开挖施工方案，31 个反复出现的词
里 13 个用了两三种说法（「基坑开挖」fouille ×13、fosse de fondation ×4；「破碎机」
还有两处误译成碎石机 concasseur）。

做法分三步，都在「自动识别场景」开着时才做：

1. :func:`find_recurring_terms` 在本机统计：出现在至少 3 段不同文字里的词（中文取
   2–10 个字的重复片段，只留最长的；其他文字取两到四个词的词组和句中大写的专名），
   附上一两处用法不同的原文。不调模型。
2. :class:`TermPicker` 把候选词连同文档说明交给模型，请它挑出需要全文统一的专业
   术语和专名，各给一个译法和一句适用场景说明；定错一个就全文一起错，所以请它拿不准
   的不选。优先用「记忆库清洗模型」（一般配得比翻译模型好）；没配、不可用或请求失败
   时改用文本翻译模型；都不行就跳过，照常翻译，运行日志写明原因。
3. 定下的词由 :class:`core.tm_terms.TermIndex` 随批附上（只附本批出现的），翻完核对
   哪些位置采用了；在大多数位置被采用的，按「自动保存」存进记忆库，备注为适用场景
   （:mod:`core.term_session`）。

记忆库里已能在句中采用的词条（手动维护的、带备注的自动保存词条）一律优先，不再
重新定译法。
"""

from __future__ import annotations

import json
import re
import threading
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass

from loguru import logger

from engines.base_engine import engine_supports_chat, strip_markdown_json

# 至少出现在几段不同的文字里才算「反复出现」。
MIN_TEXTS = 3
# 交给模型挑的候选上限，其中两个字的词最多占这么多（两个字的常用词很多，免得挤掉长词）。
# 复测时两字词 20 个名额被「施工」「开挖」这类泛称占满，「监理」没进候选。
MAX_CANDIDATES = 150
MAX_TWO_CHAR_CANDIDATES = 40
# 模型最多定这么多个。复测时降到 30 个，名额被「电梯」「车间」这类本来就不会译乱的词占满，
# 「技术交底」「分项工程」反而没选进去，前后不一的词从 3 个回升到 6 个。
MAX_TERMS = 50
# 送去模型的文字总量少于这么多字时不做：两三批就翻完，批与批之间的出入很少。
MIN_DOCUMENT_CHARS = 3000
# 中文候选的长度范围（字）。
_CJK_MIN, _CJK_MAX = 2, 10
# 拉丁文字：一个更长的词组包含它、且出现段数相差不到这个比例时，短的那个只是长词组的一截。
_FRAGMENT_RATIO = 1.2
# 两个字的候选只算不在三到六个字的候选里的那些位置（七个字以上的多半是一截句子，不算）。
_CJK_COVER_MAX = 6
# 一个词左边（或右边）同一个字占了这么大比例，它多半只是更长的词的一截。
_EDGE_DOMINANCE = 0.8
_EXAMPLE_RADIUS = 16
_TARGET_MAX_CHARS = 80
_NOTE_MAX_CHARS = 40

_CJK_RANGE = "㐀-鿿豈-﫿"
_CJK_RUN_RE = re.compile(f"[{_CJK_RANGE}]{{2,}}")
_CJK_CHAR_RE = re.compile(r"[㐀-鿿豈-﫿぀-ヿ가-힯]")
# 拉丁文字的词组不跨标点、不跨汉字。
_LATIN_SEGMENT_SPLIT_RE = re.compile(r"[^\w\s'’-]+|[㐀-鿿豈-﫿぀-ヿ가-힯]+")
_LATIN_TOKEN_RE = re.compile(r"[^\W\d_][^\W_]*(?:['’-][^\W\d_]+)*", re.UNICODE)

# 词的开头或结尾是这些字的，多半是句子片段（「的设备」「施工时」「完成后」）。开头的方位字
# （上下内外前后）不在里面：「外墙」「后浇带」「上部结构」都是正经的词。
_CJK_EDGE_STOP = set("的了着过和与及或并且而则把被将从向到由给使让对于以在为是等之其该此即也个各每这那所")
_CJK_END_STOP = _CJK_EDGE_STOP | set("时中后前约第")
# 出现在中间也说明是片段的字。
_CJK_INNER_STOP = set("的了着过吗呢吧")
# 施工、技术文件里到处都是、在哪种语言里都不需要统一说法的两字词。
_CJK_COMMON_WORDS = set(
    "进行 采用 组织 编制 按照 根据 及时 确保 保证 要求 相关 有关 以及 其中 同时 如果 可以 不得 严禁 "
    "完成 方可 应当 应该 需要 必须 负责 进入 设置 使用 开始 结束 通过 由于 因此 所有 各种 一般 符合 "
    "满足 规定 情况 方式 方法 时间 过程 期间 以上 以下 左右 必要 主要 其他 一定 不能 能够 进一步 "
    "已经 正在 包括 存在 发现 处理 检查 做好 加强 注意 认真 严格 有效 合理 及其 具体 实际 达到 采取".split()
)

_LATIN_STOP = {
    # English
    "a", "an", "the", "of", "and", "or", "to", "in", "on", "for", "with", "by", "at", "from", "as",
    "is", "are", "be", "this", "that", "these", "those", "it", "its", "into", "per", "all", "any",
    "shall", "must", "will", "should", "can", "may", "not", "no", "when", "if", "then", "than",
    # French
    "le", "la", "les", "un", "une", "des", "du", "de", "d", "l", "et", "ou", "en", "au", "aux",
    "pour", "par", "sur", "dans", "avec", "est", "sont", "ce", "cet", "cette", "ces", "son", "sa", "ses",
    "qui", "que", "doit", "doivent", "être", "pas", "ne",
    # German / Spanish basics
    "der", "die", "das", "und", "el", "los", "las", "y", "del",
}


@dataclass(frozen=True)
class TermCandidate:
    """本机找到的一个候选词。"""

    source: str
    texts: int
    example: str
    known: str = ""


@dataclass(frozen=True)
class DocumentTerm:
    """模型为本文档定下的一个词。"""

    source: str
    target: str
    note: str = ""
    texts: int = 0


# ── 第一步：本机找词 ───────────────────────────────────────────────────────────


def _clean(text: object) -> str:
    return " ".join(str(text or "").split())


def _cjk_ok(term: str) -> bool:
    if term[0] in _CJK_EDGE_STOP or term[-1] in _CJK_END_STOP or term in _CJK_COMMON_WORDS:
        return False
    return not any(ch in _CJK_INNER_STOP for ch in term)


def _cjk_substrings(text: str) -> set[str]:
    found: set[str] = set()
    for run in _CJK_RUN_RE.findall(text):
        run = run[:400]
        for start in range(len(run)):
            for size in range(_CJK_MIN, min(_CJK_MAX, len(run) - start) + 1):
                found.add(run[start:start + size])
    return found


def _positions(text: str, term: str) -> list[int]:
    found: list[int] = []
    start = text.find(term)
    while start >= 0:
        found.append(start)
        start = text.find(term, start + 1)
    return found


def _free_edges(term: str, texts: Sequence[str], where: Sequence[int]) -> bool:
    """词的两头在文中是活的：左边、右边都接过不同的字（标点、段首段尾每次都算一种新的），
    而且没有哪一个字占了八成以上。「坑开挖」左边永远是「基」，「下一道工」右边几乎总是
    「序」，「合格后方可进行下一道」两头永远是同样的字（同一句话反复出现），都只是
    更长片段的一截。"""
    left: Counter[str] = Counter()
    right: Counter[str] = Counter()
    edges = 0
    size = len(term)
    total = 0
    for index in where:
        text = texts[index]
        for start in _positions(text, term):
            total += 1
            before = text[start - 1] if start > 0 else ""
            after = text[start + size] if start + size < len(text) else ""
            if before and _CJK_RUN_RE.match(before + before):
                left[before] += 1
            else:
                edges += 1
                left[f"#{edges}"] += 1
            if after and _CJK_RUN_RE.match(after + after):
                right[after] += 1
            else:
                edges += 1
                right[f"#{edges}"] += 1
    if len(left) < 2 or len(right) < 2:
        return False
    limit = total * _EDGE_DOMINANCE
    return max(left.values()) <= limit and max(right.values()) <= limit


def _cjk_candidates(texts: Sequence[str]) -> dict[str, int]:
    """反复出现的中文词及其出现段数。

    先去掉两头不活的片段（:func:`_free_edges`）。两个字的再看一层：三到六个字的词
    在各段里占住的位置不算，「开挖」只出现在「土方开挖」「基坑开挖」里时不单列，
    「土方」「项目」也一样。三个字以上的不这样扣：「报项目部审批」这类反复出现的短句
    两头也是活的，扣了就把「项目部」吞掉了。
    """
    occurs: dict[str, list[int]] = {}
    for index, text in enumerate(texts):
        for term in _cjk_substrings(text):
            occurs.setdefault(term, []).append(index)
    pool = [
        term
        for term, where in occurs.items()
        if len(where) >= MIN_TEXTS and _cjk_ok(term) and _free_edges(term, texts, where)
    ]
    pool.sort(key=lambda term: (-len(term), -len(occurs[term]), term))
    covered: dict[int, list[tuple[int, int]]] = {}
    accepted: dict[str, int] = {}
    for term in pool:
        size = len(term)
        if size > _CJK_MIN:
            accepted[term] = len(occurs[term])
            if size <= _CJK_COVER_MAX:
                for index in occurs[term]:
                    covered.setdefault(index, []).extend(
                        (start, start + size) for start in _positions(texts[index], term)
                    )
            continue
        free = 0
        for index in occurs[term]:
            spans = covered.get(index, ())
            if any(
                not any(left <= start and start + size <= right for left, right in spans)
                for start in _positions(texts[index], term)
            ):
                free += 1
        if free >= MIN_TEXTS:
            accepted[term] = free
    return accepted


class _LatinStats:
    """一个拉丁文字词组：出现在哪几段，左右各接过哪些词（标点、句首句尾每次都算一种新的）。"""

    __slots__ = ("texts", "left", "right")

    def __init__(self) -> None:
        self.texts: set[int] = set()
        self.left: set[str] = set()
        self.right: set[str] = set()


def _latin_candidates(texts: Sequence[str]) -> dict[str, int]:
    """两到四个词的词组（小写），首尾不是虚词、两头接过不同的词；外加句中大写开头的
    单词（专名）。返回 {词组: 出现段数}。"""
    stats: dict[str, _LatinStats] = {}
    proper: Counter[str] = Counter()
    edge = 0
    for index, text in enumerate(texts):
        seen_proper: set[str] = set()
        for segment in _LATIN_SEGMENT_SPLIT_RE.split(text):
            tokens = _LATIN_TOKEN_RE.findall(segment)
            lowered = [token.lower() for token in tokens]
            for start in range(len(tokens)):
                if lowered[start] in _LATIN_STOP or len(lowered[start]) < 2:
                    continue
                for size in range(2, 5):
                    end = start + size
                    if end > len(tokens):
                        break
                    if lowered[end - 1] in _LATIN_STOP or len(lowered[end - 1]) < 2:
                        continue
                    item = stats.setdefault(" ".join(lowered[start:end]), _LatinStats())
                    item.texts.add(index)
                    edge += 1
                    item.left.add(lowered[start - 1] if start else f"#{edge}")
                    edge += 1
                    item.right.add(lowered[end] if end < len(tokens) else f"#{edge}")
            for position, token in enumerate(tokens):
                # 分句后第一个词大写不说明什么（句首）；句中大写、三个字母以上的才可能是专名。
                if position and token[:1].isupper() and len(token) >= 3 and lowered[position] not in _LATIN_STOP:
                    seen_proper.add(lowered[position])
        proper.update(seen_proper)
    counts = {
        phrase: len(item.texts)
        for phrase, item in stats.items()
        if len(item.texts) >= MIN_TEXTS and len(item.left) >= 2 and len(item.right) >= 2
    }
    for word, count in proper.items():
        if count >= MIN_TEXTS:
            counts[word] = max(count, counts.get(word, 0))
    return counts


def _drop_fragments(counts: Mapping[str, int]) -> dict[str, int]:
    """拉丁文字词组：被更长词组包含、出现段数又差不多的，只是长词组的一截。"""
    dropped: set[str] = set()
    for longer in sorted(counts, key=len, reverse=True):
        words = longer.split(" ")
        parts = {
            " ".join(words[start:start + size])
            for size in range(1, len(words))
            for start in range(len(words) - size + 1)
        }
        for part in parts:
            if part in counts and counts[part] <= counts[longer] * _FRAGMENT_RATIO:
                dropped.add(part)
    return {key: value for key, value in counts.items() if key not in dropped}


def _weight(term: str, count: int, *, is_cjk: bool) -> float:
    """排序用：两个字的词常是泛泛的说法，七个字以上的多半是一截句子，都往后排。"""
    size = len(term) if is_cjk else len(term.split(" ")) + 2
    if size <= 2:
        factor = 0.6
    elif size == 3:
        factor = 1.0
    elif size <= 6:
        factor = 1.2
    elif size <= 8:
        factor = 0.9
    else:
        factor = 0.7
    return count * factor


def _snippet(text: str, position: int, size: int) -> str:
    start = max(0, position - _EXAMPLE_RADIUS)
    end = min(len(text), position + size + _EXAMPLE_RADIUS)
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")


def _examples(term: str, texts: Sequence[str], *, fold: bool) -> str:
    """一两处原文，截取词前后各十几个字。先挑够长的最短一段；再找一段词两头接的字
    不同的，让模型看到这个词在文中的另一种用法（「曳引钢丝绳」「限速器钢丝绳」）。
    ``fold`` 为真时不分大小写地找，截出来的仍是原写法。"""
    needle = term.lower() if fold else term
    found: list[tuple[bool, str, int]] = []
    for text in texts:
        position = (text.lower() if fold else text).find(needle)
        if position >= 0:
            found.append((len(text) < len(term) + 4, text, position))
    if not found:
        return ""
    found.sort(key=lambda item: (item[0], len(item[1])))
    size = len(term)

    def edges(text: str, position: int) -> tuple[str, str]:
        return text[position - 1:position], text[position + size:position + size + 1]

    _short, first, first_at = found[0]
    picked = [_snippet(first, first_at, size)]
    first_edges = edges(first, first_at)
    for _short, text, position in found[1:]:
        if edges(text, position) != first_edges:
            picked.append(_snippet(text, position, size))
            break
    return " ／ ".join(picked)


def _surface(term: str, texts: Sequence[str]) -> str:
    """拉丁文字候选按小写统计，交给模型时用文中最常见的原写法。"""
    pattern = re.compile(r"(?<![^\W_])" + re.escape(term) + r"(?![^\W_])", re.IGNORECASE)
    forms: Counter[str] = Counter()
    for text in texts:
        forms.update(match.group(0) for match in pattern.finditer(text))
    return forms.most_common(1)[0][0] if forms else term


def document_char_count(texts: Iterable[str]) -> int:
    return sum(len(_clean(text)) for text in texts)


def find_recurring_terms(
    texts: Iterable[str],
    *,
    exclude: Callable[[str], bool] | None = None,
    known: Callable[[str], str] | None = None,
    limit: int = MAX_CANDIDATES,
) -> list[TermCandidate]:
    """在去重后的文字里找出反复出现的词（不调模型）。

    ``exclude(term)`` 为真的词跳过（记忆库里已能在句中采用的）；``known(term)`` 返回
    记忆库里这个词已有的整段译法（没有返回空串），一并交给模型参考。
    """
    distinct = [text for text in dict.fromkeys(_clean(text) for text in texts) if text]
    if not distinct:
        return []
    cjk = _cjk_candidates(distinct)
    latin = _drop_fragments(_latin_candidates(distinct))
    ranked = sorted(
        [(term, count, True) for term, count in cjk.items()]
        + [(term, count, False) for term, count in latin.items()],
        key=lambda item: (-_weight(item[0], item[1], is_cjk=item[2]), -len(item[0]), item[0]),
    )
    picked: list[TermCandidate] = []
    two_char = 0
    for term, count, is_cjk in ranked:
        if len(picked) >= limit:
            break
        surface = term if is_cjk else _surface(term, distinct)
        if exclude is not None and exclude(surface):
            continue
        if is_cjk and len(term) <= 2:
            if two_char >= MAX_TWO_CHAR_CANDIDATES:
                continue
            two_char += 1
        example = _examples(term, distinct, fold=not is_cjk)
        picked.append(
            TermCandidate(
                source=surface,
                texts=count,
                example=example,
                known=(known(surface) if known is not None else "") or "",
            )
        )
    return picked


# ── 第二步：请模型定译法 ───────────────────────────────────────────────────────

_PICK_SYSTEM = (
    "你是翻译前的统一用词审定助手。用户会给出一份待翻译文档的说明，以及文档中在多处反复出现的词"
    "（每个附一两处原文）。这份文档会分成许多批分别翻译，请为需要全文统一说法的词各定一个{target}译法，"
    "供每一批翻译时参照。定下的译法全文照用，定错了就全文一起错，所以宁缺毋滥。要求：\n"
    "1. 只选专业术语和专有名词：设备、材料、构件、工序、机构、职务、项目名、公司名、地名等，"
    "在{target}里有不止一种说法、需要统一的词。以下不选：普通词语；「施工」「安装」「开挖」「检查」"
    "这类单独的动作或泛称（译法随上下文变）；只有一种通行译法、各批不会译得不一样的词（如「电梯」）；"
    "不成词的片段；数字和代号。名额先给最容易前后译得不一样的词，如工序、做法、机构和职务的名称。\n"
    "2. 译法用{target}该行业里规范、通行的说法，不要逐字拼出一个行业里没有的说法；名词用原形，"
    "不加冠词、不用复数；专有名词按通行写法。附有「记忆库已有译法」的，没有明显错误就沿用。\n"
    "3. 一个词在文中指几种不同的东西时（看所附原文），译法要对每一种都成立，做不到的不选。\n"
    "4. 拿不准的不选：不选的词由各批照常翻译。\n"
    "5. 每个词附一句不超过 15 个字的适用场景，写这个译法成立的行业或领域（例如「建筑施工」"
    "「电梯工程」），不写本文档的具体内容；在这个范围里，这个译法都要成立。\n"
    "6. 最多 {limit} 个，没有合适的就返回空对象。只输出一个 JSON 对象：键是候选词原文（与列表中一字不差），"
    '值是 {{"译法": "…", "场景": "…"}}。不要输出其他内容。'
)


def build_pick_request(
    candidates: Sequence[TermCandidate],
    *,
    profile: str,
    target_language: str,
    limit: int = MAX_TERMS,
) -> tuple[str, str]:
    system = _PICK_SYSTEM.format(target=target_language or "目标语言", limit=limit)
    lines = [f"文档说明：{_clean(profile) or '（无）'}", "", "候选词（原文 ｜ 出现段数 ｜ 一两处原文）："]
    for number, item in enumerate(candidates, start=1):
        line = f"{number}. {item.source} ｜ {item.texts} 段 ｜ {item.example}"
        if item.known:
            line += f" ｜ 记忆库已有译法：{item.known}"
        lines.append(line)
    return system, "\n".join(lines)


def _value_fields(value: object) -> tuple[str, str]:
    if isinstance(value, str):
        return value, ""
    if isinstance(value, Mapping):
        target = next(
            (value.get(key) for key in ("译法", "translation", "target", "译文") if value.get(key)), ""
        )
        note = next((value.get(key) for key in ("场景", "note", "scene", "备注") if value.get(key)), "")
        return str(target or ""), str(note or "")
    return "", ""


def parse_pick_response(
    raw: object,
    candidates: Sequence[TermCandidate],
    *,
    target_is_cjk: bool,
    limit: int = MAX_TERMS,
) -> list[DocumentTerm]:
    """解析模型返回的 JSON；只收候选里有的词，译法不合格（空、过长、还带汉字）的丢掉。"""
    payload = json.loads(strip_markdown_json(str(raw or "")))
    if not isinstance(payload, Mapping):
        raise ValueError("统一用词返回的不是 JSON 对象")
    by_source = {_clean(item.source).lower(): item for item in candidates}
    terms: list[DocumentTerm] = []
    seen: set[str] = set()
    for key, value in payload.items():
        candidate = by_source.get(_clean(key).lower())
        if candidate is None or candidate.source.lower() in seen:
            continue
        target, note = _value_fields(value)
        target = _clean(target)
        note = _clean(note)[:_NOTE_MAX_CHARS]
        if not target or len(target) > _TARGET_MAX_CHARS or target.lower() == candidate.source.lower():
            continue
        if not target_is_cjk and _CJK_CHAR_RE.search(target):
            continue
        seen.add(candidate.source.lower())
        terms.append(DocumentTerm(source=candidate.source, target=target, note=note, texts=candidate.texts))
        if len(terms) >= limit:
            break
    return terms


_BAD_REPLY = "模型的回复格式不正确。"


def _failure_reason(exc: BaseException) -> str:
    """写进运行日志的失败原因：一句用户看得懂的话（地址或模型名不对、超时……）。"""
    from core.task_runner import user_facing_reason

    return user_facing_reason(exc, fallback="请求没有成功。")


class TermPicker:
    """请模型定译法：先用记忆库清洗模型，不行再用文本翻译模型。

    ``primary`` 返回清洗模型的引擎（没配好、不可用时返回 ``None``），第一次用到时才建；
    ``fallback`` 是文本翻译模型的引擎。``log(level, message)`` 写运行日志。
    :attr:`engine_name` 是最后定下译法的那个引擎，存进记忆库时记作来源。
    """

    def __init__(
        self,
        *,
        primary: Callable[[], object | None] | None,
        fallback: object | None,
        target_language: str,
        target_is_cjk: bool,
        log: Callable[[str, str], None] | None = None,
    ) -> None:
        self._primary_factory = primary
        self._primary: object | None = None
        self._primary_ready = False
        self._fallback = fallback if engine_supports_chat(fallback) else None
        self._target_language = target_language
        self._target_is_cjk = target_is_cjk
        self._log = log or (lambda _level, _message: None)
        self._lock = threading.Lock()
        self.engine_name = ""

    def _primary_engine(self) -> object | None:
        with self._lock:
            if not self._primary_ready:
                self._primary_ready = True
                if self._primary_factory is not None:
                    try:
                        engine = self._primary_factory()
                    except Exception as exc:  # noqa: BLE001 - fall back to the translation model
                        logger.info(f"记忆库清洗模型建不起来，统一用词改用文本翻译模型：{exc!r}")
                        engine = None
                    self._primary = engine if engine_supports_chat(engine) else None
            return self._primary

    def __call__(
        self,
        candidates: Sequence[TermCandidate],
        *,
        profile: str,
        log: Callable[[str, str], None] | None = None,
    ) -> list[DocumentTerm]:
        """定译法；``log`` 给了就写到那里（每份文件各有各的运行日志）。"""
        if not candidates:
            return []
        log = log or self._log
        system, user = build_pick_request(candidates, profile=profile, target_language=self._target_language)
        attempts: list[tuple[str, object]] = []
        primary = self._primary_engine()
        if primary is not None:
            attempts.append(("cleaner", primary))
        if self._fallback is not None:
            attempts.append(("translation", self._fallback))
        reason = ""
        for index, (role, engine) in enumerate(attempts):
            try:
                raw = engine.chat(system, user)
            except Exception as exc:  # noqa: BLE001 - optional step, translation goes on
                logger.info(f"统一用词请求失败（{role}）：{exc!r}")
                reason = _failure_reason(exc)
            else:
                try:
                    terms = parse_pick_response(raw, candidates, target_is_cjk=self._target_is_cjk)
                except Exception as exc:  # noqa: BLE001 - optional step, translation goes on
                    logger.info(f"统一用词返回无法解析（{role}）：{exc!r}")
                    reason = _BAD_REPLY
                else:
                    self.engine_name = str(getattr(engine, "engine_name", "") or "")
                    if terms:
                        log(
                            "INFO",
                            (
                                f"统一用词：记忆库清洗模型为文中反复出现的 {len(terms)} 个词确定了统一译法，将随批附上"
                                if role == "cleaner"
                                else f"统一用词：文本翻译模型为文中反复出现的 {len(terms)} 个词确定了统一译法，将随批附上"
                            ),
                        )
                    return terms
            if role == "cleaner" and index + 1 < len(attempts):
                log("INFO", f"统一用词：记忆库清洗模型未能确定译法，改用文本翻译模型。原因：{reason}")
        if attempts:
            log("WARN", f"统一用词：未能确定译法，按常规方式继续翻译。原因：{reason}")
        return []


def cleaner_engine_factory(settings) -> Callable[[], object | None]:
    """记忆库清洗模型的引擎，第一次用到时才建（用创建这一刻所在线程的密钥快照）。

    清洗模型没填模型名、测试结果为不可用时返回 ``None``，由调用方改用文本翻译模型。
    """
    from settings import current_key_overrides, provider_key_overrides

    key_overrides = current_key_overrides()

    def build() -> object | None:
        from core.engine_dispatcher import build_engine
        from core.model_roles import (
            ROLE_CLEANER,
            connection_key_overrides,
            resolve_effective_model_config,
            settings_for_text_role,
        )

        with provider_key_overrides(key_overrides):
            config = resolve_effective_model_config(settings, ROLE_CLEANER)
            if not str(config.model or "").strip() or config.availability_status == "unavailable":
                return None
            role_settings = settings_for_text_role(settings, ROLE_CLEANER)
        merged = dict(key_overrides or {})
        merged.update(connection_key_overrides(config))
        with provider_key_overrides(merged):
            return build_engine(role_settings)

    return build


def make_term_picker(settings, translation_engine, *, target_lang: str, log=None) -> TermPicker:
    """任务流程用的定译法入口：清洗模型优先，文本翻译模型兜底。"""
    from core.tm_terms import is_cjk_target
    from engines.base_engine import get_target_lang_name

    return TermPicker(
        primary=cleaner_engine_factory(settings) if settings is not None else None,
        fallback=translation_engine,
        target_language=get_target_lang_name(target_lang),
        target_is_cjk=is_cjk_target(target_lang),
        log=log,
    )
