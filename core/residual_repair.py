# -*- coding: utf-8 -*-
"""
残留修复阶梯：外科修补 + 带反馈重译（Word / Excel 共用，设计 §3.3-3.4）。

分工：
- 本模块负责 prompt 构造、回复解析、验收判定与阶梯编排，全部纯函数可测；
- 传输层（engine.chat 的并发、限流、停止信号）由调用方以回调注入；
- diff 受限验收器 surgical_repair_ok 在 core/residual_classifier 中（P1 已落地）。

阶梯（按残留类别路由，验收不过绝不覆盖原译文）：
  term_fragment  → 外科修补 → 验收不过 → 带反馈重译 → 再不过 → 保留原译文待复核
  sentence_block → 带反馈重译 → 不过 → 保留原译文待复核
  cn_date_unit   → 阻断重译（现状规则保留），直接待复核，不花 API
  numbering_prefix（上游确定性修复已失败）→ 不猜，直接待复核，不花 API
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from collections import Counter

from loguru import logger

from core.residual_classifier import (
    CATEGORY_CN_DATE_UNIT,
    CATEGORY_NUMBERING_PREFIX,
    CATEGORY_QUANTITY_UNIT,
    CATEGORY_TERM_FRAGMENT,
    align_enum_prefix_to_convention,
    classify_residual_spans,
    extract_number_tokens,
    surgical_repair_ok,
)
from engines.base_engine import get_target_lang_name, strip_markdown_json

METHOD_SURGICAL = "surgical"
METHOD_FEEDBACK_RETRANSLATION = "feedback_retranslation"


@dataclass(frozen=True)
class RepairOutcome:
    """一次修复阶梯的最终结论。text 为验收通过的译文；未通过时保持原译文。"""

    accepted: bool
    text: str
    method: str = ""
    reject_reasons: tuple[str, ...] = ()


def build_surgical_repair_messages(
    source_text: str,
    target_text: str,
    span_texts,
    target_lang: str,
) -> tuple[str, str]:
    """构造外科修补请求。返回 (system, user)，回复协议为 JSON {"repaired": ...}。"""
    lang_name = get_target_lang_name(target_lang)
    system = (
        "你是一名专业技术文档译审。给你一段中文源文、它的当前译文"
        f"（目标语言：{lang_name}），以及译文中残留未翻译的中文片段。\n"
        "任务：只把残留片段替换为正确的目标语言译法，其余内容一个字都不许改。\n"
        "硬性要求：\n"
        "1. 只允许修改残留片段本身及其紧邻的连接词，不得改写句子的其他部分；\n"
        "2. 不得改动任何数字；\n"
        "3. 修补后的译文不得再含有中文（数量单位「万/亿」除外）；\n"
        '4. 只返回 JSON：{"repaired": "<完整的修补后译文>"}，不要任何解释。'
    )
    user = json.dumps(
        {
            "source": str(source_text or ""),
            "translation": str(target_text or ""),
            "residual_fragments": [str(t) for t in span_texts],
        },
        ensure_ascii=False,
    )
    return system, user


def build_feedback_note(span_texts) -> str:
    """结构化失败反馈，附在重译请求里（Word 的重试 prompt 与 Excel 共用这句话）。"""
    fragments = "、".join(f"«{str(t)}»" for t in span_texts)
    return (
        f"上一稿译文残留了未翻译的中文片段：{fragments}。"
        "本稿必须把源文完整翻译成目标语言，不得出现任何中文字符"
        "（数量单位「万/亿」除外），并保持所有数字与源文一致。"
    )


def build_feedback_retry_messages(
    source_text: str,
    span_texts,
    target_lang: str,
    *,
    base_prompt: str = "",
) -> tuple[str, str]:
    """构造带反馈的整句重译请求（Excel 传输用）。回复协议同外科修补。"""
    lang_name = get_target_lang_name(target_lang)
    feedback = build_feedback_note(span_texts)
    parts = []
    if str(base_prompt or "").strip():
        parts.append(str(base_prompt).strip())
    parts.append(
        f"请把用户给出的中文源文完整翻译为{lang_name}。\n"
        f"重要反馈：{feedback}\n"
        '只返回 JSON：{"repaired": "<译文>"}，不要任何解释。'
    )
    system = "\n\n".join(parts)
    return system, str(source_text or "")


def parse_repair_reply(raw: str) -> str | None:
    """解析修补/重译回复。协议外的回复一律返回 None（宁缺毋滥）。"""
    try:
        payload = json.loads(strip_markdown_json(str(raw or "")))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    repaired = payload.get("repaired")
    if not isinstance(repaired, str) or not repaired.strip():
        return None
    return repaired.strip()


def verify_feedback_retranslation(
    source_text: str,
    candidate: str,
    *,
    target_lang: str,
) -> tuple[bool, str]:
    """重译稿验收：非空、不是原文复读、无阻断级残留、源文数字一个不能丢。

    数字规则是包含而非相等：译文允许多出数字（「一、」译成「1.」、日期
    重排都会新增 token），但源文里的每个数字都必须原样出现——养护天数、
    坍落度被改写的重译稿绝不允许覆盖原译文。汉字数字（三十天）不在此列，
    宁可放过也不为它做数值换算猜测。
    """
    candidate = str(candidate or "")
    if not candidate.strip():
        return False, "重译稿为空"
    if candidate.strip().lower() == str(source_text or "").strip().lower():
        return False, "重译稿与源文相同"
    leftover = classify_residual_spans(candidate, target_lang=target_lang)
    bad = [s for s in leftover if s.category != CATEGORY_QUANTITY_UNIT]
    if bad:
        return False, "重译稿仍有残留：" + "、".join(f"«{s.text}»" for s in bad)
    missing = Counter(extract_number_tokens(source_text)) - Counter(
        extract_number_tokens(candidate)
    )
    if missing:
        lost = "、".join(sorted(missing.elements()))
        return False, f"重译稿丢失或改动了源文中的数字：{lost}"
    return True, ""


def repair_unit(
    source_text: str,
    target_text: str,
    *,
    target_lang: str,
    surgical_send=None,
    retranslate_send=None,
) -> RepairOutcome:
    """
    对一个残留单元跑完整修复阶梯。

    surgical_send(system, user) -> 原始回复 | None
    retranslate_send(system, user) -> 原始回复 | None
    回调返回 None 表示传输失败（引擎不支持 chat / 请求异常），按拒收处理。
    """
    target = str(target_text or "")
    spans = classify_residual_spans(target, target_lang=target_lang)
    blocking = [s for s in spans if s.category != CATEGORY_QUANTITY_UNIT]
    if not blocking:
        return RepairOutcome(accepted=True, text=target)

    categories = {span.category for span in blocking}
    # 0 API 类别只在「整单元全是它们」时才短路：路由按片段分流（设计 §3），
    # 序号/日期残留与整句未译共存的单元必须还能走带反馈重译，否则挂着一个
    # 序号前缀就把 12 个字的漏译永远锁死在人工复核里。
    zero_api_categories = {CATEGORY_CN_DATE_UNIT, CATEGORY_NUMBERING_PREFIX}
    if categories <= zero_api_categories:
        reasons = []
        if CATEGORY_CN_DATE_UNIT in categories:
            reasons.append("数字+中文单位残留按既有规则阻断重译")
        if CATEGORY_NUMBERING_PREFIX in categories:
            reasons.append("序号残留缺乏确定性修复依据，不做模型猜测")
        return RepairOutcome(
            accepted=False, text=target, reject_reasons=tuple(reasons)
        )

    reasons: list[str] = []
    span_texts = [span.text for span in blocking]

    if categories == {CATEGORY_TERM_FRAGMENT} and surgical_send is not None:
        system, user = build_surgical_repair_messages(
            source_text, target, span_texts, target_lang
        )
        raw = surgical_send(system, user)
        candidate = parse_repair_reply(raw) if raw is not None else None
        if candidate is None:
            reasons.append("外科修补：未取得协议内回复")
        else:
            ok, why = surgical_repair_ok(
                target,
                candidate,
                [(span.start, len(span.text)) for span in blocking],
                target_lang=target_lang,
            )
            if ok:
                return RepairOutcome(
                    accepted=True,
                    text=candidate,
                    method=METHOD_SURGICAL,
                    reject_reasons=tuple(reasons),
                )
            reasons.append(f"外科修补拒收：{why}")

    if retranslate_send is not None:
        system, user = build_feedback_retry_messages(
            source_text, span_texts, target_lang
        )
        raw = retranslate_send(system, user)
        candidate = parse_repair_reply(raw) if raw is not None else None
        if candidate is None:
            reasons.append("带反馈重译：未取得协议内回复")
        else:
            ok, why = verify_feedback_retranslation(
                source_text, candidate, target_lang=target_lang
            )
            if ok:
                return RepairOutcome(
                    accepted=True,
                    text=candidate,
                    method=METHOD_FEEDBACK_RETRANSLATION,
                    reject_reasons=tuple(reasons),
                )
            reasons.append(f"带反馈重译拒收：{why}")

    if not reasons:
        reasons.append("无可用修复通道（引擎不支持 chat）")
    return RepairOutcome(
        accepted=False, text=target, reject_reasons=tuple(reasons)
    )


# 修复阶梯批量护栏的默认值：单次任务最多修多少个单元（超出的直接待复核，
# 调用方必须把 over_cap_count 说出去，不许静默截断）；传输层连续失败多少
# 次后熔断（引擎已经不通，继续逐条硬撞只会拖长任务）。
DEFAULT_REPAIR_MAX_UNITS = 120
DEFAULT_REPAIR_BREAKER_THRESHOLD = 4


@dataclass
class RepairLadderResult:
    """一批残留单元跑完修复阶梯的汇总。remaining 保持原有单元对象。"""

    accepted: dict = field(default_factory=dict)  # source_text -> 验收通过的译文
    method_counts: dict = field(default_factory=dict)  # method -> 条数
    remaining: list = field(default_factory=list)  # 仍需人工复核的单元
    reject_reasons: dict = field(default_factory=dict)  # source_text -> tuple[str]
    over_cap_count: int = 0
    breaker_tripped: bool = False


def run_repair_ladder(
    units,
    *,
    target_lang: str,
    send,
    convention: str = "",
    max_units: int = DEFAULT_REPAIR_MAX_UNITS,
    breaker_threshold: int = DEFAULT_REPAIR_BREAKER_THRESHOLD,
    should_stop=None,
    on_progress=None,
) -> RepairLadderResult:
    """
    对一批残留单元（带 source_text / target_text 属性）跑修复阶梯，
    Word / Excel 主流程共用这一个入口，护栏只维护一处：

    - 上限：超过 max_units 的单元不发请求，直接进 remaining；
    - 熔断：send 连续 breaker_threshold 次拿不到回复（抛异常或返回
      None）就停止后续请求——引擎已经不通，逐条硬撞只会拖长任务；
      协议外回复不算传输失败（那是模型的问题，不是通道的问题）；
    - 停止：should_stop() 为真时剩余单元原样进 remaining；
    - on_progress(done, total)：每个单元开跑前回调一次，供界面报进度。

    convention 为主流程投出的文档级序号惯例：验收通过的重译稿如果把
    「（三）」写成了另一族序号（如「3.」而全篇是「(III)」），在这里按
    惯例做确定性对齐——数值对不上或惯例未知则原样保留，不猜。
    """
    result = RepairLadderResult()
    queue = list(units)
    over_cap: list = []
    if max_units and len(queue) > max_units:
        over_cap = queue[max_units:]
        queue = queue[:max_units]
        result.over_cap_count = len(over_cap)

    consecutive_failures = 0

    def guarded_send(system: str, user: str):
        nonlocal consecutive_failures
        try:
            reply = send(system, user)
        except Exception as send_exc:
            logger.debug(f"残留修复请求失败：{send_exc!r}")
            consecutive_failures += 1
            return None
        if reply is None:
            consecutive_failures += 1
        else:
            consecutive_failures = 0
        return reply

    for index, unit in enumerate(queue, start=1):
        if should_stop is not None and should_stop():
            result.remaining.append(unit)
            continue
        if consecutive_failures >= breaker_threshold:
            result.breaker_tripped = True
            result.remaining.append(unit)
            continue
        if on_progress is not None:
            on_progress(index, len(queue))
        outcome = repair_unit(
            unit.source_text,
            unit.target_text,
            target_lang=target_lang,
            surgical_send=guarded_send,
            retranslate_send=guarded_send,
        )
        if outcome.accepted and outcome.method:
            accepted_text = outcome.text
            if outcome.method == METHOD_FEEDBACK_RETRANSLATION and convention:
                aligned = align_enum_prefix_to_convention(
                    unit.source_text, accepted_text, convention=convention
                )
                if aligned is not None:
                    accepted_text = aligned
            result.accepted[unit.source_text] = accepted_text
            result.method_counts[outcome.method] = (
                result.method_counts.get(outcome.method, 0) + 1
            )
        else:
            result.remaining.append(unit)
            if outcome.reject_reasons:
                result.reject_reasons[unit.source_text] = outcome.reject_reasons
    result.remaining.extend(over_cap)
    return result
