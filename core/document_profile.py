"""「自动识别场景」领域模式：翻译前先让模型读一遍文档，写一段文档说明。

逐批翻译时模型一次只看到二十来条文字，看不到整份文档在讲什么。图纸里单独
出现的「甲」「乙」、表格里的「详建施」「FM」，离开上下文只能猜：防火门等级
的「甲」被译成了合同甲方的「Party A」。

「自动识别场景」在正式翻译前多发一次请求：从这批文档去重后的文字里挑几百条有
代表性的（出现最多的短标签，加上最长的几段说明），让模型写两三句话——这是
什么文档、什么专业，以及文中有歧义的短词在这里是什么意思。之后每一批翻译的
提示词都带上这段说明。

只要说明、不要术语表：概括比翻译简单，翻译用的模型就能做；也不会产出一份
要和记忆库、用户术语表比优先级的硬性对照表。这一步失败（接口报错、返回空）
就按没有说明继续翻译，不让任务卡住。

记忆库里的短词同样依赖上下文：以前自动存下的「甲 → Party A」不能套到图纸上。
:func:`is_context_sensitive` 判断一条文字是不是这种短词；「自动识别场景」下，这类
文字只认用户手动录入或置顶的记忆，自动存下的不直接套用，新译文也不自动存入。
"""
from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping

SMART_PRESET = "自动识别场景"

# 送去概括的样本：出现次数最多的短标签 + 最长的几段说明，合计不超过这么多字。
_SAMPLE_CHAR_BUDGET = 6000
_SAMPLE_FREQUENT = 200
_SAMPLE_LONGEST = 60
_SAMPLE_ITEM_CHARS = 160
# 没有成词文字的代号（FM、C1518）对判断文档类型也有用，但只留少量。
_SAMPLE_CODES = 30
_PROFILE_MAX_CHARS = 600

_CJK_RE = re.compile(r"[㐀-鿿豈-﫿]")
_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)
_CONTENT_RE = re.compile(r"[^\W_]", re.UNICODE)
_ABBREVIATION_RE = re.compile(r"[A-Za-z]{1,4}\.?")

_PROFILE_SYSTEM = (
    "你是文档翻译前的审阅助手。用户会给出一份待翻译文档里抽出的部分文字（每行一条，已去重，"
    "按出现次数从多到少排列，后面附几段较长的说明）。请写一段文档说明，供之后逐段翻译时参考，"
    "不超过 150 字，包含：\n"
    "1. 这是什么文档、属于什么专业或场景；\n"
    "2. 文中单独出现、离开上下文容易译错的单字、短词和缩写，在本文档里是什么意思，"
    "可附上在{target}中的常用译法。只写确实有歧义的，最多 8 个；没有就不写。"
    "如果后面附了「短词在原文中和相邻文字连起来的样子」，按这些连读判断短词的意思"
    "（例如和门窗编号连在一起的字通常是等级或型号的一部分）。\n"
    "只输出说明正文，不要标题，不要翻译全文，不要解释你的推理。"
)


def is_smart_preset(preset: object) -> bool:
    return str(preset or "").strip() == SMART_PRESET


def is_context_sensitive(text: object) -> bool:
    """离开上下文就可能译错的短词：两个字以内的文字，或四个字母以内的缩写。

    「甲」「乙」「留」「详」「FM」「B」都算；「设备基坑」「Door」「M1523」不算
    （后者是代号，本来就原样保留）。
    """
    value = str(text or "").strip()
    if not value:
        return False
    content = _CONTENT_RE.findall(value)
    if not content or any(char.isdigit() for char in content):
        return False
    if _CJK_RE.search(value):
        return len(content) <= 2
    if not _ABBREVIATION_RE.fullmatch(value):
        return False
    # 缩写：全大写（FM、TYP）、带缩写点（Rev.）或不超过两个字母；「Door」这种普通词不算。
    letters = value.rstrip(".")
    return letters.isupper() or value.endswith(".") or len(letters) <= 2


def _has_letters(text: str) -> bool:
    return bool(_LETTER_RE.search(text))


def _has_words(text: str) -> bool:
    if _CJK_RE.search(text):
        return True
    return any(len(word) >= 3 for word in re.findall(r"[^\W\d_]+", text))


def select_profile_sample(texts: Iterable[str]) -> list[str]:
    """Pick the lines the model reads to describe the document.

    ``texts`` may repeat (every occurrence counts towards frequency).  Pure
    numbers are dropped; codes are kept but capped; frequent short labels come
    first, then the longest notes, within a fixed character budget.
    """
    counts: Counter[str] = Counter()
    for raw in texts:
        text = " ".join(str(raw or "").split())
        if text and _has_letters(text):
            counts[text] += 1
    if not counts:
        return []
    ordered = sorted(counts, key=lambda text: (-counts[text], len(text), text))
    picked: list[str] = []
    seen: set[str] = set()
    codes = 0
    for text in ordered:
        if len(picked) >= _SAMPLE_FREQUENT:
            break
        if not _has_words(text):
            if codes >= _SAMPLE_CODES:
                continue
            codes += 1
        picked.append(text[:_SAMPLE_ITEM_CHARS])
        seen.add(text)
    longest = sorted((text for text in counts if text not in seen and _has_words(text)), key=len, reverse=True)
    picked.extend(text[:_SAMPLE_ITEM_CHARS] for text in longest[:_SAMPLE_LONGEST])
    sample: list[str] = []
    used = 0
    for text in picked:
        if used + len(text) + 1 > _SAMPLE_CHAR_BUDGET:
            break
        sample.append(text)
        used += len(text) + 1
    return sample


def _clean_profile(text: object) -> str:
    value = str(text or "").strip()
    if value.startswith("```"):
        value = value.split("\n", 1)[1] if "\n" in value else ""
        value = value.rsplit("```", 1)[0]
    value = "\n".join(line.strip() for line in value.strip().splitlines() if line.strip())
    if len(value) > _PROFILE_MAX_CHARS:
        value = value[:_PROFILE_MAX_CHARS].rstrip() + "…"
    return value


def request_document_profile(
    chat: Callable[[str, str], str],
    texts: Iterable[str],
    *,
    target_language: str,
    log: Callable[[str, str], None] | None = None,
    neighbor_rows: Iterable[str] = (),
) -> str:
    """Ask the model for a short description of the document; ``""`` on failure.

    ``chat(system, user)`` is the translation engine's single-call method.
    ``log(level, message)`` receives one line for the run log.
    ``neighbor_rows`` are short labels read together with the text beside them
    (CAD: 「FM 甲 1021」), appended under their own heading.
    """
    sample = select_profile_sample(texts)
    if not sample:
        return ""
    system = _PROFILE_SYSTEM.format(target=target_language or "目标语言")
    user = "\n".join(sample)
    rows = [" ".join(str(row).split())[:_SAMPLE_ITEM_CHARS] for row in neighbor_rows if str(row).strip()]
    if rows:
        user += "\n\n短词在原文中和相邻文字连起来的样子（按出现次数）：\n" + "\n".join(rows)
    try:
        profile = _clean_profile(chat(system, user))
    except Exception as exc:  # noqa: BLE001 - the profile is optional, translation goes on
        if log:
            log("WARN", f"自动识别场景：文档说明没有生成（{exc.__class__.__name__}），按普通方式继续翻译")
        return ""
    if log:
        if profile:
            log("INFO", f"自动识别场景：已读过 {len(sample)} 条文字，文档说明：{profile}")
        else:
            log("WARN", "自动识别场景：模型没有返回文档说明，按普通方式继续翻译")
    return profile


def append_profile(prompt: str, profile: str) -> str:
    """Attach the document description to a translation system prompt."""
    profile = str(profile or "").strip()
    if not profile:
        return prompt
    block = (
        "本文档说明（翻译前通读全文后概括，逐段翻译时据此理解术语、单字和缩写的含义）：\n"
        f"{profile}"
    )
    prompt = str(prompt or "").rstrip()
    return f"{prompt}\n\n{block}" if prompt else block


def drop_unlocked_short_hits(
    hits: dict[str, str],
    texts_by_pair: Mapping[str, Iterable[str]],
    lookup_locked: Callable[[list[str], str], Mapping[str, str | None]],
) -> int:
    """「自动识别场景」下的记忆库命中：短词只认用户定下的词条。

    ``hits`` 是普通查询的命中（原文 → 译文），就地修改：离开上下文会译错的短词，
    改用 ``lookup_locked(texts, lang_pair)``（只查置顶、手动录入、清洗锁定的词条）
    的结果；查不到或各语言对说法不一的，从命中里拿掉，交给模型按本文档重新翻译。
    返回拿掉的条数。
    """
    short = {text for text in hits if is_context_sensitive(text)}
    if not short:
        return 0
    locked: dict[str, list[str]] = {text: [] for text in short}
    for pair, pair_texts in texts_by_pair.items():
        subset = [text for text in pair_texts if text in locked]
        if not subset:
            continue
        for text, value in (lookup_locked(subset, pair) or {}).items():
            if text in locked and value is not None and str(value) not in locked[text]:
                locked[text].append(str(value))
    dropped = 0
    for text in short:
        values = locked[text]
        if len(values) == 1:
            hits[text] = values[0]
        else:
            hits.pop(text, None)
            dropped += 1
    return dropped


def lookup_locked_tm(texts: list[str], lang_pair: str) -> Mapping[str, str | None]:
    from core import tm_manager

    return tm_manager.lookup_batch(texts, lang_pair, locked_only=True)
