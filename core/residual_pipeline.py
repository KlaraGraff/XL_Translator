# -*- coding: utf-8 -*-
"""
残留修复流水线的共享入口（Word / Excel 同一套，0 API）。

设计背景见 docs/redesign/2026-08-14_residual_repair_pipeline.md §3：
翻译主流程拿到全部（源文, 译文）配对后，先做文档级序号惯例投票，再逐条
分类残留中文——能确定性修复的（序号前缀）直接改译文，改不了的分级上报。

与 core/residual_replay 的分工：replay 面向已产出的文件、只诊断不修复；
本模块面向进行中的翻译任务、在写盘前修复。两者共用 residual_classifier。

「译文等于原文」的配对不参与体检：那是「未翻译」（API 失败 / 质量校验
回退），由未翻译告警通道负责，混进「残留」只会重复报警。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from core.residual_classifier import (
    CATEGORY_NUMBERING_PREFIX,
    CATEGORY_QUANTITY_UNIT,
    CJK_SPAN_RE,
    RESIDUAL_EXEMPT_TARGET_LANGS,
    classify_residual_spans,
    detect_sibling_convention,
    deterministic_numbering_fix,
)


@dataclass(frozen=True)
class ResidualUnitReport:
    """一条源文的残留体检结论。target_text 为修复后的最终译文。"""

    source_text: str
    target_text: str
    categories: tuple[str, ...]
    spans: tuple[str, ...]


@dataclass
class ResidualPassResult:
    convention: str = ""
    checked_count: int = 0
    # source_text -> 修复后的译文，供调用方直接更新翻译词典
    fixes: dict[str, str] = field(default_factory=dict)
    auto_fixed: list[ResidualUnitReport] = field(default_factory=list)
    needs_review: list[ResidualUnitReport] = field(default_factory=list)
    # 仅数量单位（万/亿）残留：放行不拦，但留有记录
    released_notes: list[ResidualUnitReport] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (self.auto_fixed or self.needs_review or self.released_notes)


def run_residual_pass(pairs, *, target_lang: str, convention: str = "") -> ResidualPassResult:
    """
    对（源文, 译文）配对做残留体检 + 确定性修复。

    pairs 为 (source_text, target_text) 可迭代对象——dict.items() 即可。
    convention 允许调用方传入已投出的文档级序号惯例——TM 卫生这类只看
    子集的调用必须沿用全篇的投票结果，各投各的会出现同一篇文档两套
    序号写法；不传时按本批配对自行投票。
    跳过规则（都属于「不该由残留通道管」）：
      - 译文为空 / 与源文相同（未翻译，另有告警通道）；
      - 源文本身不含中文（残留中文无从谈起）；
      - 目标语言豁免（中→日等，译文含汉字是正常现象）。
    """
    result = ResidualPassResult()
    if target_lang in RESIDUAL_EXEMPT_TARGET_LANGS:
        return result

    usable: list[tuple[str, str]] = []
    for source_text, target_text in pairs:
        source = str(source_text or "")
        target = str(target_text or "")
        if not target.strip():
            continue
        if source.strip().lower() == target.strip().lower():
            continue
        if not CJK_SPAN_RE.search(source):
            continue
        usable.append((source, target))
    result.checked_count = len(usable)
    if not usable:
        return result

    convention = str(convention or "") or detect_sibling_convention(usable)
    result.convention = convention

    for source, target in usable:
        spans = classify_residual_spans(target, target_lang=target_lang)
        if not spans:
            continue
        final_text = target
        if any(span.category == CATEGORY_NUMBERING_PREFIX for span in spans):
            fix = deterministic_numbering_fix(
                target,
                convention=convention,
                target_lang=target_lang,
                source_text=source,
            )
            if fix and fix != target:
                prefix_spans = tuple(
                    span.text
                    for span in spans
                    if span.category == CATEGORY_NUMBERING_PREFIX
                )
                final_text = fix
                result.fixes[source] = fix
                result.auto_fixed.append(
                    ResidualUnitReport(
                        source_text=source,
                        target_text=final_text,
                        categories=(CATEGORY_NUMBERING_PREFIX,),
                        spans=prefix_spans,
                    )
                )
                # 修复只动前缀；重分类看正文还剩什么
                spans = classify_residual_spans(final_text, target_lang=target_lang)
        if not spans:
            continue
        categories = tuple(sorted({span.category for span in spans}))
        report = ResidualUnitReport(
            source_text=source,
            target_text=final_text,
            categories=categories,
            spans=tuple(span.text for span in spans),
        )
        if categories == (CATEGORY_QUANTITY_UNIT,):
            result.released_notes.append(report)
        else:
            result.needs_review.append(report)
    return result
