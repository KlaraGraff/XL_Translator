# -*- coding: utf-8 -*-
"""翻译单元台账的回归（设计文档 §3.6）。

台账要终结的三类历史问题，每类都有对应用例：
1. 报告条目双段号基准混排 —— report_entries 统一以 source_anchor 为主；
2. 单元静默蒸发 —— unresolved() 能审计出没有终态的单元；
3. 各环节条目互相打架 —— 状态改判必须在证据链留痕，轨迹可回放。
"""

from __future__ import annotations

import unittest

from core.unit_ledger import (
    STATE_CLEAN,
    STATE_NEEDS_REVIEW,
    STATE_RELEASED_WITH_NOTE,
    STATE_SKIPPED,
    SKIP_REASON_TOC_FIELD,
    UnitLedger,
    text_fingerprint,
)


class LedgerBasicsTest(unittest.TestCase):
    def test_register_and_resolve(self):
        ledger = UnitLedger()
        ledger.register("u1", source_text="地面裂缝", source_anchor="body.paragraph[3]")
        ledger.set_state("u1", STATE_CLEAN)
        record = ledger.get("u1")
        self.assertTrue(record.resolved)
        self.assertEqual(record.state, STATE_CLEAN)

    def test_duplicate_registration_rejected(self):
        ledger = UnitLedger()
        ledger.register("u1")
        with self.assertRaises(ValueError):
            ledger.register("u1")

    def test_unknown_unit_and_unknown_state_rejected(self):
        ledger = UnitLedger()
        with self.assertRaises(KeyError):
            ledger.set_state("ghost", STATE_CLEAN)
        ledger.register("u1")
        with self.assertRaises(ValueError):
            ledger.set_state("u1", "definitely_not_a_state")

    def test_skip_reason_only_with_skipped(self):
        ledger = UnitLedger()
        ledger.register("u1")
        with self.assertRaises(ValueError):
            ledger.set_state("u1", STATE_CLEAN, skip_reason=SKIP_REASON_TOC_FIELD)
        ledger.set_state("u1", STATE_SKIPPED, skip_reason=SKIP_REASON_TOC_FIELD)
        self.assertEqual(ledger.get("u1").skip_reason, SKIP_REASON_TOC_FIELD)


class SilentSkipAuditTest(unittest.TestCase):
    def test_unresolved_units_are_auditable(self):
        # 「静默跳过归零」：登记了但没定稿的单元必须能被机器查出来
        ledger = UnitLedger()
        ledger.register("u1")
        ledger.register("u2")
        ledger.set_state("u1", STATE_CLEAN)
        self.assertEqual([r.unit_id for r in ledger.unresolved()], ["u2"])
        self.assertEqual(ledger.counts(), {STATE_CLEAN: 1, "(unresolved)": 1})


class TrajectoryTest(unittest.TestCase):
    def test_state_change_leaves_trace(self):
        ledger = UnitLedger()
        ledger.register("u1")
        ledger.set_state("u1", STATE_CLEAN)
        ledger.set_state(
            "u1", STATE_NEEDS_REVIEW, evidence="post-write heading outlier"
        )
        record = ledger.get("u1")
        self.assertEqual(record.state, STATE_NEEDS_REVIEW)
        self.assertIn("state: clean -> needs_review", record.evidence)
        self.assertIn("post-write heading outlier", record.evidence)


class ReportViewTest(unittest.TestCase):
    def test_entries_anchor_on_source(self):
        # 历史缺陷：仲裁条目用源段号、残留条目用输出段号，同一报告两套基准。
        # 台账视图必须以 source_anchor 为主键，output_anchor 只作辅助。
        ledger = UnitLedger()
        ledger.register(
            "u1",
            source_text="第1节 总则",
            source_anchor="body.paragraph[18]",
            output_anchor="body.paragraph[19]",
        )
        ledger.set_state(
            "u1", STATE_NEEDS_REVIEW, categories=["numbering_prefix"],
            evidence="residual: numbering_prefix«第1节»",
        )
        ledger.register("u2", source_text="干净段", source_anchor="body.paragraph[20]")
        ledger.set_state("u2", STATE_CLEAN)
        ledger.register("u3", source_text="1.2 万元", source_anchor="Sheet1!B7")
        ledger.set_state("u3", STATE_RELEASED_WITH_NOTE, categories=["quantity_unit"])

        entries = ledger.report_entries()
        self.assertEqual(len(entries), 2)  # 默认视图只导出值得人看的
        first = entries[0]
        self.assertEqual(first["source_anchor"], "body.paragraph[18]")
        self.assertEqual(first["output_anchor"], "body.paragraph[19]")
        self.assertEqual(first["state"], STATE_NEEDS_REVIEW)
        self.assertEqual(first["fingerprint"], text_fingerprint("第1节 总则"))
        self.assertEqual(entries[1]["source_anchor"], "Sheet1!B7")

    def test_fingerprint_normalizes_whitespace(self):
        self.assertEqual(
            text_fingerprint("第1节  总则"), text_fingerprint("第1节 总则")
        )
        self.assertNotEqual(
            text_fingerprint("第1节 总则"), text_fingerprint("第2节 总则")
        )


if __name__ == "__main__":
    unittest.main()
