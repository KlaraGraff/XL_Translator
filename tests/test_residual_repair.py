# -*- coding: utf-8 -*-
"""残留修复阶梯的回归（设计文档 §3.3-3.4，Word/Excel 共用）。

阶梯的可靠性来自「验收不过绝不覆盖原译文」：
1. 外科修补稿必须通过 diff 受限验收（残留消除 + 改动限窗 + 数字不变）；
2. 修补拒收升级带反馈重译，重译稿复跑分类器验收；
3. cn_date_unit / numbering_prefix 不花 API：前者按既有规则阻断重译，
   后者上游确定性修复已失败、不做模型猜测；
4. 传输失败（不支持 chat、协议外回复）按拒收处理，原译文原样保留。
"""

from __future__ import annotations

import json
import unittest

from core.residual_repair import (
    METHOD_FEEDBACK_RETRANSLATION,
    METHOD_SURGICAL,
    RepairOutcome,
    build_feedback_note,
    parse_repair_reply,
    repair_unit,
    verify_feedback_retranslation,
)

SOURCE = "沿裂缝开V型槽并清理浮灰。"
TARGET_WITH_FRAGMENT = "Ouvrir une rainure en V 型槽 le long de la fissure et nettoyer."
GOOD_SURGICAL = "Ouvrir une rainure en V le long de la fissure et nettoyer."
GOOD_RETRANSLATION = (
    "Ouvrir une rainure en V le long de la fissure et enlever la poussière."
)


def _reply(text: str) -> str:
    return json.dumps({"repaired": text}, ensure_ascii=False)


class _RecordingSend:
    """记录调用并按脚本出牌的假传输层。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[tuple[str, str]] = []

    def __call__(self, system: str, user: str):
        self.calls.append((system, user))
        if not self.replies:
            return None
        return self.replies.pop(0)


class SurgicalLadderTest(unittest.TestCase):
    def test_good_surgical_repair_is_accepted(self):
        surgical = _RecordingSend([_reply(GOOD_SURGICAL)])
        retranslate = _RecordingSend([])
        outcome = repair_unit(
            SOURCE,
            TARGET_WITH_FRAGMENT,
            target_lang="fr",
            surgical_send=surgical,
            retranslate_send=retranslate,
        )
        self.assertTrue(outcome.accepted)
        self.assertEqual(outcome.method, METHOD_SURGICAL)
        self.assertEqual(outcome.text, GOOD_SURGICAL)
        self.assertEqual(len(surgical.calls), 1)
        self.assertEqual(retranslate.calls, [])  # 修补通过就不再花重译请求

    def test_rewrite_rejected_then_escalates_to_retranslation(self):
        # 修补稿整句重写（远处措辞被改）→ diff 验收拒收 → 升级重译
        rewrite = "Une rainure en V doit être ouverte, puis nettoyée."
        surgical = _RecordingSend([_reply(rewrite)])
        retranslate = _RecordingSend([_reply(GOOD_RETRANSLATION)])
        outcome = repair_unit(
            SOURCE,
            TARGET_WITH_FRAGMENT,
            target_lang="fr",
            surgical_send=surgical,
            retranslate_send=retranslate,
        )
        self.assertTrue(outcome.accepted)
        self.assertEqual(outcome.method, METHOD_FEEDBACK_RETRANSLATION)
        self.assertEqual(outcome.text, GOOD_RETRANSLATION)
        self.assertEqual(len(surgical.calls), 1)
        self.assertEqual(len(retranslate.calls), 1)
        # 重译请求里必须带上结构化失败反馈
        self.assertIn("型槽", retranslate.calls[0][0])
        self.assertTrue(any("外科修补拒收" in r for r in outcome.reject_reasons))

    def test_both_stages_fail_keeps_original_text(self):
        surgical = _RecordingSend(["not json at all"])
        retranslate = _RecordingSend([_reply(SOURCE)])  # 重译复读源文 → 拒收
        outcome = repair_unit(
            SOURCE,
            TARGET_WITH_FRAGMENT,
            target_lang="fr",
            surgical_send=surgical,
            retranslate_send=retranslate,
        )
        self.assertFalse(outcome.accepted)
        self.assertEqual(outcome.text, TARGET_WITH_FRAGMENT)  # 原译文原样保留
        self.assertEqual(len(outcome.reject_reasons), 2)


class ZeroApiRoutingTest(unittest.TestCase):
    def test_cn_date_unit_blocks_without_api_calls(self):
        surgical = _RecordingSend([_reply("x")])
        retranslate = _RecordingSend([_reply("x")])
        outcome = repair_unit(
            "工期至2026年8月",
            "Délai jusqu'à 2026年8月",
            target_lang="fr",
            surgical_send=surgical,
            retranslate_send=retranslate,
        )
        self.assertFalse(outcome.accepted)
        self.assertEqual(surgical.calls, [])
        self.assertEqual(retranslate.calls, [])

    def test_unfixed_numbering_prefix_does_not_guess(self):
        surgical = _RecordingSend([_reply("x")])
        retranslate = _RecordingSend([_reply("x")])
        outcome = repair_unit(
            "重要事项",
            "三、Points importants",
            target_lang="fr",
            surgical_send=surgical,
            retranslate_send=retranslate,
        )
        self.assertFalse(outcome.accepted)
        self.assertEqual(surgical.calls, [])
        self.assertEqual(retranslate.calls, [])

    def test_sentence_block_goes_straight_to_retranslation(self):
        surgical = _RecordingSend([_reply("x")])
        retranslate = _RecordingSend([_reply(GOOD_RETRANSLATION)])
        outcome = repair_unit(
            SOURCE,
            "沿裂缝开V型槽并清理浮灰。 (texte non traduit)",
            target_lang="fr",
            surgical_send=surgical,
            retranslate_send=retranslate,
        )
        self.assertTrue(outcome.accepted)
        self.assertEqual(outcome.method, METHOD_FEEDBACK_RETRANSLATION)
        self.assertEqual(surgical.calls, [])  # 成句未译不走外科修补

    def test_clean_text_needs_no_repair(self):
        outcome = repair_unit(
            SOURCE,
            GOOD_SURGICAL,
            target_lang="fr",
            surgical_send=None,
            retranslate_send=None,
        )
        self.assertTrue(outcome.accepted)
        self.assertEqual(outcome.method, "")

    def test_no_transport_available_keeps_original(self):
        outcome = repair_unit(
            SOURCE,
            TARGET_WITH_FRAGMENT,
            target_lang="fr",
            surgical_send=None,
            retranslate_send=None,
        )
        self.assertFalse(outcome.accepted)
        self.assertEqual(outcome.text, TARGET_WITH_FRAGMENT)
        self.assertTrue(outcome.reject_reasons)


class ProtocolHelpersTest(unittest.TestCase):
    def test_parse_repair_reply_accepts_fenced_json(self):
        raw = "```json\n" + _reply(GOOD_SURGICAL) + "\n```"
        self.assertEqual(parse_repair_reply(raw), GOOD_SURGICAL)

    def test_parse_repair_reply_rejects_off_protocol(self):
        self.assertIsNone(parse_repair_reply("Voici la traduction corrigée."))
        self.assertIsNone(parse_repair_reply(json.dumps({"repaired": ""})))
        self.assertIsNone(parse_repair_reply(None))

    def test_feedback_note_names_the_fragments(self):
        note = build_feedback_note(["型槽", "浮灰"])
        self.assertIn("«型槽»", note)
        self.assertIn("«浮灰»", note)

    def test_verify_retranslation_rejects_residual_and_echo(self):
        ok, _ = verify_feedback_retranslation(
            SOURCE, GOOD_RETRANSLATION, target_lang="fr"
        )
        self.assertTrue(ok)
        ok, why = verify_feedback_retranslation(SOURCE, SOURCE, target_lang="fr")
        self.assertFalse(ok)
        ok, why = verify_feedback_retranslation(
            SOURCE, "Ouvrir une rainure 型槽", target_lang="fr"
        )
        self.assertFalse(ok)
        self.assertIn("型槽", why)

    def test_outcome_dataclass_defaults(self):
        outcome = RepairOutcome(accepted=True, text="x")
        self.assertEqual(outcome.method, "")
        self.assertEqual(outcome.reject_reasons, ())


if __name__ == "__main__":
    unittest.main()
