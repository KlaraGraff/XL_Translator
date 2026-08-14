# -*- coding: utf-8 -*-
"""
翻译单元台账（Word / Excel 两条流水线共用）。

设计背景见 docs/redesign/2026-08-14_residual_repair_pipeline.md §3.6。
核心立场：每个翻译单元从进入流水线到落盘只有一条状态轨迹；报告、底色、
TM 写入决策都是台账的过滤视图，而不是各环节各自攒的条目——段号基准
不一致、条目互相打架、单元静默蒸发这三类历史问题在此结构下不可能复发。

锚定纪律：所有对外条目一律以源文档锚点为主键（source_anchor），输出
锚点（output_anchor）只作辅助信息。历史报告曾出现仲裁条目用源段号、
残留条目用输出段号的双基准混排，这里从结构上消灭。

本模块只依赖标准库，不 import 任何流水线模块。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

# 单元终态（设计文档 §3.6 的枚举，逐字对应）
STATE_CLEAN = "clean"
STATE_AUTO_FIXED_DETERMINISTIC = "auto_fixed_deterministic"
STATE_AUTO_FIXED_SURGICAL = "auto_fixed_surgical"
STATE_RETRANSLATED_WITH_FEEDBACK = "retranslated_with_feedback"
STATE_ARBITRATION_ACCEPTED = "arbitration_accepted"
STATE_RELEASED_WITH_NOTE = "released_with_note"
STATE_NEEDS_REVIEW = "needs_review"
STATE_SKIPPED = "skipped"

ALL_STATES = frozenset(
    {
        STATE_CLEAN,
        STATE_AUTO_FIXED_DETERMINISTIC,
        STATE_AUTO_FIXED_SURGICAL,
        STATE_RETRANSLATED_WITH_FEEDBACK,
        STATE_ARBITRATION_ACCEPTED,
        STATE_RELEASED_WITH_NOTE,
        STATE_NEEDS_REVIEW,
        STATE_SKIPPED,
    }
)

# 报告层约定：只有 needs_review 上底色（8/12 定下的「底色只给需要人手的」）
REVIEW_MARK_STATES = frozenset({STATE_NEEDS_REVIEW})

# skipped 的常见原因（开放集合，仅作约定参考）
SKIP_REASON_FRONT_MATTER = "front_matter"
SKIP_REASON_TOC_FIELD = "toc_field"
SKIP_REASON_HEADER_FOOTER = "header_footer"


def text_fingerprint(text: str) -> str:
    """源文本指纹：空白归一后取 sha1 前 12 位。跨文档定位同一单元用。"""
    normalized = " ".join(str(text or "").split())
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]


@dataclass
class UnitRecord:
    """一个翻译单元的台账记录。"""

    unit_id: str
    source_text: str = ""
    source_anchor: str = ""   # 源文档锚点，如 body.paragraph[12] / Sheet1!B7
    output_anchor: str = ""   # 输出文档锚点（辅助信息）
    state: str = ""           # ALL_STATES 之一；空串表示尚未定稿
    skip_reason: str = ""     # 仅 state == skipped 时有值
    categories: tuple[str, ...] = ()  # 残留类别（residual_classifier 的 CATEGORY_*）
    evidence: list[str] = field(default_factory=list)  # 只增不删的证据链

    @property
    def fingerprint(self) -> str:
        return text_fingerprint(self.source_text)

    @property
    def resolved(self) -> bool:
        return bool(self.state)


class UnitLedger:
    """按 unit_id 索引的台账。注册与定稿分离，未定稿单元可被审计出来。"""

    def __init__(self) -> None:
        self._records: dict[str, UnitRecord] = {}
        self._order: list[str] = []

    def register(
        self,
        unit_id: str,
        *,
        source_text: str = "",
        source_anchor: str = "",
        output_anchor: str = "",
    ) -> UnitRecord:
        """登记一个翻译单元。重复登记同一 unit_id 是接线错误，直接拒绝。"""
        key = str(unit_id)
        if not key:
            raise ValueError("unit_id must be non-empty")
        if key in self._records:
            raise ValueError(f"unit already registered: {key}")
        record = UnitRecord(
            unit_id=key,
            source_text=str(source_text or ""),
            source_anchor=str(source_anchor or ""),
            output_anchor=str(output_anchor or ""),
        )
        self._records[key] = record
        self._order.append(key)
        return record

    def _require(self, unit_id: str) -> UnitRecord:
        record = self._records.get(str(unit_id))
        if record is None:
            raise KeyError(f"unit not registered: {unit_id}")
        return record

    def set_state(
        self,
        unit_id: str,
        state: str,
        *,
        skip_reason: str = "",
        evidence: str = "",
        categories=None,
    ) -> UnitRecord:
        """定稿或改判一个单元的终态。改判合法（如 clean → needs_review），
        但每次变更都会在证据链留痕，轨迹可回放。"""
        if state not in ALL_STATES:
            raise ValueError(f"unknown state: {state}")
        if skip_reason and state != STATE_SKIPPED:
            raise ValueError("skip_reason only valid with state=skipped")
        record = self._require(unit_id)
        if record.state and record.state != state:
            record.evidence.append(f"state: {record.state} -> {state}")
        record.state = state
        record.skip_reason = str(skip_reason or "") if state == STATE_SKIPPED else ""
        if categories is not None:
            record.categories = tuple(categories)
        if evidence:
            record.evidence.append(str(evidence))
        return record

    def add_evidence(self, unit_id: str, note: str) -> None:
        if note:
            self._require(unit_id).evidence.append(str(note))

    def set_output_anchor(self, unit_id: str, anchor: str) -> None:
        self._require(unit_id).output_anchor = str(anchor or "")

    def get(self, unit_id: str) -> UnitRecord | None:
        return self._records.get(str(unit_id))

    def records(self) -> list[UnitRecord]:
        return [self._records[key] for key in self._order]

    def unresolved(self) -> list[UnitRecord]:
        """尚无终态的单元。落盘定稿前必须为空——这就是「静默跳过归零」的
        机器检查点：任何可译单元不允许没有终态。"""
        return [record for record in self.records() if not record.resolved]

    def by_state(self, state: str) -> list[UnitRecord]:
        return [record for record in self.records() if record.state == state]

    def counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for record in self.records():
            key = record.state or "(unresolved)"
            result[key] = result.get(key, 0) + 1
        return result

    def report_entries(self, states=None) -> list[dict]:
        """报告视图：源锚点为主、输出锚点为辅的统一条目。

        states 为 None 时默认导出「值得人看」的状态（needs_review 与
        released_with_note）；报告层如需全量审计可显式传 ALL_STATES。
        """
        wanted = (
            frozenset(states)
            if states is not None
            else frozenset({STATE_NEEDS_REVIEW, STATE_RELEASED_WITH_NOTE})
        )
        entries: list[dict] = []
        for record in self.records():
            if record.state not in wanted:
                continue
            entries.append(
                {
                    "unit_id": record.unit_id,
                    "source_anchor": record.source_anchor,
                    "output_anchor": record.output_anchor,
                    "fingerprint": record.fingerprint,
                    "state": record.state,
                    "skip_reason": record.skip_reason,
                    "categories": list(record.categories),
                    "evidence": list(record.evidence),
                    "source_text": record.source_text,
                }
            )
        return entries
