# -*- coding: utf-8 -*-
"""对抗性评审确认缺陷的回归锁定（共享核心闸门层）。

每个测试类对应一类已被实跑复现过的缺陷，防止回退：
- 外科修补验收：窗口只约束原文侧偏移 → 增长上限 + 有序数字序列；
- 带反馈重译验收：不校验数字 → 源文数字包含校验；
- 分类器：「第X」可选单位字过吞正文、日期单位区间劫持整句；
- 确定性序号修复：截断非序号词语、留悬空顿号；
- 修复阶梯：按单元一票否决 → 按片段路由；
- 调度器：API 失败回退被质量校验二次计数。
"""

from __future__ import annotations

import unittest

from core.engine_dispatcher import TranslationBatchRunStats, _apply_quality_filter
from core.residual_classifier import (
    CATEGORY_CN_DATE_UNIT,
    CATEGORY_NUMBERING_PREFIX,
    CATEGORY_SENTENCE_BLOCK,
    classify_residual_spans,
    deterministic_numbering_fix,
    surgical_repair_ok,
)
from core.residual_pipeline import run_residual_pass
from core.residual_repair import (
    METHOD_FEEDBACK_RETRANSLATION,
    repair_unit,
    verify_feedback_retranslation,
)
from core.tm_hygiene import sanitize_tm_pairs


class SurgicalAcceptanceHardeningTest(unittest.TestCase):
    """外科修补验收器：修补稿侧的改动量必须有上界，数字连顺序都不许动。"""

    def test_in_window_insertion_bomb_is_rejected(self):
        # 残留在句尾时窗口覆盖到字符串末端，原文侧偏移全部「在窗口内」；
        # 没有增长上限的话一次 replace 能塞进任意长度的自造内容
        original = "Remplir la rainure en V avec du mortier 型槽"
        repaired = original[:-2] + (
            "rainure trapézoïdale selon la norme européenne EN 1504 partie 3, "
            "appliquée en deux couches avec cure humide de sept jours"
        )
        ok, why = surgical_repair_ok(
            original, repaired, [(40, 2)], target_lang="fr"
        )
        self.assertFalse(ok)
        self.assertIn("grew too much", why)

    def test_short_string_full_rewrite_is_rejected(self):
        # 整段长度 ≤ 残留末尾+12 时窗口规则失效，增长上限必须兜底
        ok, why = surgical_repair_ok(
            "型槽 en V.",
            "Traitement completement different ici applique.",
            [(0, 2)],
            target_lang="fr",
        )
        self.assertFalse(ok)

    def test_number_swap_within_window_is_rejected(self):
        # 排序后的多重集合比较放过「3 与 5 互换」——必须按出现顺序比较
        ok, why = surgical_repair_ok(
            "型槽 de 3 a 5 m",
            "Rainure de 5 a 3 m",
            [(0, 2)],
            target_lang="fr",
        )
        self.assertFalse(ok)
        self.assertIn("numbers changed", why)

    def test_legitimate_term_replacement_still_passes(self):
        ok, why = surgical_repair_ok(
            "Remplir la 型槽 avec du mortier de 3 mm.",
            "Remplir la rainure en V avec du mortier de 3 mm.",
            [(11, 2)],
            target_lang="fr",
        )
        self.assertTrue(ok, why)


class RetranslationNumberGateTest(unittest.TestCase):
    """带反馈重译验收器：源文里的数字一个都不许丢、不许改。"""

    def test_corrupted_numbers_are_rejected(self):
        ok, why = verify_feedback_retranslation(
            "混凝土养护14天，坍落度180±20mm，共浇筑3层。",
            "Cure du béton pendant 7 jours, affaissement 200 mm, coulage en 5 couches.",
            target_lang="fr",
        )
        self.assertFalse(ok)
        self.assertIn("数字", why)

    def test_extra_numbers_from_enumeration_are_tolerated(self):
        # 「一、」译成「1.」会新增数字 token：包含校验不该拦这种正常展开
        ok, why = verify_feedback_retranslation(
            "一、本工程采用C30混凝土，符合现行规范。",
            "1. Ce projet utilise du béton C30 conformément aux normes en vigueur.",
            target_lang="fr",
        )
        self.assertTrue(ok, why)


class ClassifierOverSwallowTest(unittest.TestCase):
    """「第X」前缀与日期单位不许吞掉真实残留。"""

    def test_di_san_fang_is_not_a_numbering_prefix(self):
        spans = classify_residual_spans(
            "第三方检测机构 doit disposer des qualifications requises.",
            target_lang="fr",
        )
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0].text, "第三方检测机构")
        self.assertEqual(spans[0].category, CATEGORY_SENTENCE_BLOCK)

    def test_bare_di_x_before_separator_is_still_numbering(self):
        spans = classify_residual_spans("第三 Contrôle de la qualité", target_lang="fr")
        self.assertEqual(spans[0].category, CATEGORY_NUMBERING_PREFIX)

    def test_date_unit_only_claims_fully_contained_fragments(self):
        spans = classify_residual_spans(
            "Le délai est de 30日历天内完成全部工作", target_lang="fr"
        )
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0].category, CATEGORY_SENTENCE_BLOCK)
        self.assertEqual(spans[0].text, "日历天内完成全部工作")

    def test_pure_date_unit_residual_keeps_its_category(self):
        spans = classify_residual_spans("Achevé en 2026年", target_lang="fr")
        self.assertEqual(spans[0].category, CATEGORY_CN_DATE_UNIT)


class DeterministicFixNoTruncationTest(unittest.TestCase):
    """确定性修复绝不把非序号词语砍掉半截，也不留悬空标点。"""

    def test_source_anchor_does_not_chop_di_san_fang(self):
        result = run_residual_pass(
            [("5.5.3 第三方检测要求", "第三方检测 exigences de contrôle par un tiers")],
            target_lang="fr",
        )
        self.assertEqual(result.fixes, {})
        self.assertEqual(len(result.needs_review), 1)
        self.assertIn("第三方检测", result.needs_review[0].spans)

    def test_section_branch_does_not_chop_jie_dian(self):
        # 「第五节点」是「节点」的词语，不是「第五节 + 点」
        result = run_residual_pass(
            [("第五节点验收要求", "第五节点 exigences de réception")],
            target_lang="fr",
        )
        self.assertEqual(result.fixes, {})
        fix = deterministic_numbering_fix(
            "第五节点 exigences de réception", convention="paren_arabic",
            target_lang="fr", source_text="第五节点验收要求",
        )
        self.assertIsNone(fix)

    def test_dangling_enum_separator_is_consumed(self):
        result = run_residual_pass(
            [("5.5.3 焊接工艺要求", "第一、Exigences de soudage")],
            target_lang="fr",
        )
        self.assertEqual(
            result.fixes.get("5.5.3 焊接工艺要求"), "5.5.3 Exigences de soudage"
        )

    def test_section_heading_fix_still_works(self):
        fix = deterministic_numbering_fix(
            "第三节 Contrôle de la qualité", convention="paren_arabic",
            target_lang="fr", source_text="第三节 质量控制",
        )
        self.assertEqual(fix, "Section 3 Contrôle de la qualité")

    def test_bu_fen_unit_word_is_matched_whole(self):
        # 「第二部分」整体是序号（部分 为双字单位词），后随正文汉字时则不是
        spans = classify_residual_spans("第二部分 Dispositions", target_lang="fr")
        self.assertEqual(spans[0].category, CATEGORY_NUMBERING_PREFIX)
        self.assertEqual(spans[0].text, "第二部分")
        spans = classify_residual_spans(
            "第二部分安排如下 comme indiqué ci-dessous", target_lang="fr"
        )
        self.assertEqual(spans[0].category, CATEGORY_SENTENCE_BLOCK)


class RepairLadderRoutingTest(unittest.TestCase):
    """修复阶梯按片段路由：0 API 短路只针对纯序号/纯日期单元。"""

    def test_mixed_numbering_and_sentence_reaches_retranslation(self):
        calls = []

        def retranslate(system, user):
            calls.append(user)
            return (
                '{"repaired": "1. Ce projet utilise du béton C30 '
                'conformément aux normes en vigueur."}'
            )

        outcome = repair_unit(
            "一、本工程采用C30混凝土，符合现行规范。",
            "一、本工程采用C30混凝土 conformément aux normes en vigueur.",
            target_lang="fr",
            surgical_send=None,
            retranslate_send=retranslate,
        )
        self.assertTrue(outcome.accepted)
        self.assertEqual(outcome.method, METHOD_FEEDBACK_RETRANSLATION)
        self.assertEqual(len(calls), 1)

    def test_pure_numbering_prefix_still_zero_api(self):
        def must_not_send(system, user):
            raise AssertionError("纯序号残留不许发请求")

        outcome = repair_unit(
            "（三）质量控制",
            "第三 Contrôle de la qualité",
            target_lang="fr",
            surgical_send=must_not_send,
            retranslate_send=must_not_send,
        )
        self.assertFalse(outcome.accepted)
        self.assertIn("序号残留缺乏确定性修复依据，不做模型猜测", outcome.reject_reasons)

    def test_rejected_retranslation_keeps_original_text(self):
        outcome = repair_unit(
            "混凝土养护14天，坍落度180±20mm，共浇筑3层。",
            "Cure du béton pendant 14 jours, affaissement 180±20 mm, coulage en 3 层.",
            target_lang="fr",
            surgical_send=lambda s, u: None,
            retranslate_send=lambda s, u: (
                '{"repaired": "Cure du béton pendant 7 jours, affaissement '
                '200 mm, coulage en 5 couches."}'
            ),
        )
        self.assertFalse(outcome.accepted)
        self.assertEqual(
            outcome.text,
            "Cure du béton pendant 14 jours, affaissement 180±20 mm, coulage en 3 层.",
        )
        self.assertTrue(any("数字" in reason for reason in outcome.reject_reasons))


class QualityFilterDoubleCountTest(unittest.TestCase):
    """API 失败回退的条目不许被质量校验二次计数、二次上报。"""

    def test_untranslated_fallback_is_not_counted_again(self):
        stats = TranslationBatchRunStats()
        stats.record_untranslated(["施工方案总说明"], "HTTP 503")
        results = {"施工方案总说明": "施工方案总说明"}
        _apply_quality_filter(results, "fr", stats=stats)
        self.assertEqual(stats.untranslated_count, 1)
        self.assertEqual(stats.quality_reset_count, 0)
        self.assertEqual(stats.quality_reset_items, [])

    def test_engine_echo_is_still_reset_and_counted(self):
        stats = TranslationBatchRunStats()
        results = {"施工方案总说明": "施工方案总说明"}
        _apply_quality_filter(results, "fr", stats=stats)
        self.assertEqual(stats.quality_reset_count, 1)
        self.assertEqual(stats.quality_reset_items, ["施工方案总说明"])


class HygieneNormalizedCountTest(unittest.TestCase):
    """归一计数只算真正入库的条目：修好前缀但仍被拦下的不算「已归一」。"""

    def test_fixed_but_rejected_pair_is_only_counted_as_rejected(self):
        pairs = [
            ("（一）表层裂缝", "(1) Fissures superficielles"),
            ("（二）结构裂缝", "(2) Fissures structurelles"),
            (
                "（四）沉降裂缝并清理浮灰后进行下一道工序施工",
                "（四）Fissures de tassement 并清理浮灰后进行下一道工序",
            ),
        ]
        result = sanitize_tm_pairs(pairs, target_lang="fr")
        rejected_sources = {source for source, _reason in result.rejected}
        self.assertIn("（四）沉降裂缝并清理浮灰后进行下一道工序施工", rejected_sources)
        self.assertNotIn(
            "（四）沉降裂缝并清理浮灰后进行下一道工序施工", result.normalized
        )


if __name__ == "__main__":
    unittest.main()
