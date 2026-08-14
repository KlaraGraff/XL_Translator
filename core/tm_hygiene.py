# -*- coding: utf-8 -*-
"""
TM 入库卫生（Word / Excel 共用，0 API，设计 §3.7）。

两条写入前规则：
1. 惯例归一：序号前缀按本批次的文档惯例确定性修复；节标题写法按多数派
   归一——阻断「Section N」与「Nième section」两套写法在库里混存。
2. 分类器复检：译文仍带非 quantity_unit 残留的配对不入库。现有排除规则
   只覆盖重试/仲裁接受的段落，这里是入库口的结构性双保险。

存量词条的惯例归一走 core/tm_cleaner.build_convention_suggestions
（先建议、用户确认后写入），本模块只管新写入。
"""
from __future__ import annotations

from dataclasses import dataclass

from core.residual_classifier import (
    RESIDUAL_EXEMPT_TARGET_LANGS,
    check_heading_consistency,
    is_section_heading_source,
)
from core.residual_pipeline import run_residual_pass


@dataclass(frozen=True)
class TmHygieneResult:
    """写入前卫生结论。pairs 为归一后可入库的 (源文, 译文)。"""

    pairs: tuple[tuple[str, str], ...] = ()
    # 译文被归一（序号前缀 / 标题写法）的源文
    normalized: tuple[str, ...] = ()
    # (源文, 拦下原因)：带阻断级残留，不入库
    rejected: tuple[tuple[str, str], ...] = ()


def sanitize_tm_pairs(
    pairs,
    *,
    target_lang: str,
    convention: str = "",
    heading_majority: str | None = None,
) -> TmHygieneResult:
    """
    对将要写入 TM 的（源文, 译文）配对做写入前卫生处理。

    convention：主流程投出的文档级序号惯例。TM 只收到全篇配对的一个
    子集，让它自己投票可能与全篇结论相左——库里就会混入另一套写法。
    heading_majority：同理，节标题写法的文档级多数派。不传时子集自投，
    与全篇结论相反的话会把主流程刻意保留的条目改写后入库。

    豁免目标语言（中→日等）原样放行：译文含汉字是正常现象，
    序号/标题写法惯例也不适用。
    """
    materialized = [(str(s or ""), str(t or "")) for s, t in pairs]
    if not materialized or target_lang in RESIDUAL_EXEMPT_TARGET_LANGS:
        return TmHygieneResult(pairs=tuple(materialized))

    residual = run_residual_pass(
        materialized, target_lang=target_lang, convention=convention
    )
    rejected = {
        unit.source_text: "译文残留中文：" + "、".join(f"«{t}»" for t in unit.spans)
        for unit in residual.needs_review
    }
    # 序号修好但正文仍有阻断残留的配对最终没入库：只算「拦下」，不算「归一」，
    # 否则同一条源文会同时出现在两条日志里，对不上账
    normalized: set[str] = {
        source for source in residual.fixes if source not in rejected
    }
    kept = [
        (source, residual.fixes.get(source, target))
        for source, target in materialized
        if source not in rejected
    ]

    # 节标题写法归一：本批次多数派为准，unit_key 直接用源文
    heading_observations = [
        (source, target, source)
        for source, target in kept
        if is_section_heading_source(source)
    ]
    if heading_observations:
        consistency = check_heading_consistency(
            heading_observations,
            target_lang=target_lang,
            majority_form=heading_majority,
        )
        if consistency.fixes:
            kept = [(s, consistency.fixes.get(s, t)) for s, t in kept]
            normalized.update(consistency.fixes)

    return TmHygieneResult(
        pairs=tuple(kept),
        normalized=tuple(sorted(normalized)),
        rejected=tuple(
            (source, rejected[source])
            for source, _target in materialized
            if source in rejected
        ),
    )


def tm_hygiene_log_lines(hygiene: TmHygieneResult) -> list[tuple[str, str]]:
    """入库卫生留痕文案（Word / Excel 共用）：归一是顺手事提一句，
    拦下的必须说清为什么没进库。返回 (级别, 消息) 列表。"""
    lines: list[tuple[str, str]] = []
    if hygiene.normalized:
        lines.append((
            "INFO",
            f"TM 入库前惯例归一 {len(hygiene.normalized)} 条（序号前缀/标题写法）。",
        ))
    if hygiene.rejected:
        samples = "；".join(
            f"{source[:20]}…（{reason}）" if len(source) > 20 else f"{source}（{reason}）"
            for source, reason in hygiene.rejected[:3]
        )
        lines.append((
            "WARN",
            (
                f"TM 入库复检拦下 {len(hygiene.rejected)} 条带残留中文的配对，"
                f"未写入词库：{samples}"
            ),
        ))
    return lines
