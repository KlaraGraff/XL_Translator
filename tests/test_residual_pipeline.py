# -*- coding: utf-8 -*-
"""残留修复流水线共享入口的回归（设计文档 §3，Word/Excel 共用）。

要守住的行为边界：
1. 序号残留能按同级惯例 / 源编号锚点确定性修复，修复进 fixes 供上层回写；
2. 「译文=原文」的未翻译条目不进残留通道（另有未翻译告警，不重复报警）；
3. 仅数量单位残留放行留痕，不升级成 needs_review；
4. 目标语言豁免（中→日）与非中文源文直接跳过。
"""

from __future__ import annotations

import unittest

from core.residual_classifier import (
    CATEGORY_NUMBERING_PREFIX,
    CATEGORY_QUANTITY_UNIT,
    CATEGORY_TERM_FRAGMENT,
    CONVENTION_PAREN_ROMAN,
)
from core.residual_pipeline import run_residual_pass


class NumberingFixTest(unittest.TestCase):
    def test_paren_family_fixed_by_sibling_convention(self):
        pairs = [
            ("（一）表层裂缝", "(I) Fissures superficielles"),
            ("（二）结构裂缝", "(II) Fissures structurelles"),
            ("（三）贯穿裂缝", "(III) Fissures traversantes"),
            ("（四）沉降裂缝", "（四）Fissures de tassement"),
        ]
        result = run_residual_pass(pairs, target_lang="fr")
        self.assertEqual(result.convention, CONVENTION_PAREN_ROMAN)
        self.assertEqual(
            result.fixes, {"（四）沉降裂缝": "(IV) Fissures de tassement"}
        )
        self.assertEqual(len(result.auto_fixed), 1)
        self.assertEqual(
            result.auto_fixed[0].categories, (CATEGORY_NUMBERING_PREFIX,)
        )
        # 前缀修掉后正文干净，不再进 needs_review
        self.assertEqual(result.needs_review, [])

    def test_source_anchored_fix_beats_convention(self):
        # 模型把源编号 5.5.3 改写成了「三、」——照抄源编号，不做惯例推断
        pairs = [
            ("5.5.3 施工安全措施", "三、Mesures de sécurité du chantier"),
        ]
        result = run_residual_pass(pairs, target_lang="fr")
        self.assertEqual(len(result.fixes), 1)
        fixed = result.fixes["5.5.3 施工安全措施"]
        self.assertTrue(fixed.startswith("5.5.3"), fixed)
        self.assertIn("Mesures de sécurité", fixed)

    def test_unfixable_numbering_goes_to_needs_review(self):
        # 裸「三、」无源锚点、无同级惯例可投票 → 不猜，交人工
        pairs = [("重要事项", "三、Points importants")]
        result = run_residual_pass(pairs, target_lang="fr")
        self.assertEqual(result.fixes, {})
        self.assertEqual(len(result.needs_review), 1)
        self.assertIn(
            CATEGORY_NUMBERING_PREFIX, result.needs_review[0].categories
        )


class ChannelSeparationTest(unittest.TestCase):
    def test_untranslated_pairs_are_skipped(self):
        # 译文=原文是「未翻译」，由 api_unavailable / 质量回退通道负责
        pairs = [
            ("施工方案总说明", "施工方案总说明"),
            ("养护要求", "Exigences de cure"),
        ]
        result = run_residual_pass(pairs, target_lang="fr")
        self.assertEqual(result.checked_count, 1)
        self.assertTrue(result.clean)

    def test_non_cjk_source_and_empty_target_skipped(self):
        pairs = [
            ("A-1", "A-1 zone"),
            ("2026-08", ""),
        ]
        result = run_residual_pass(pairs, target_lang="fr")
        self.assertEqual(result.checked_count, 0)
        self.assertTrue(result.clean)

    def test_exempt_target_lang_returns_empty(self):
        pairs = [("（四）沉降裂缝", "（四）沈下亀裂")]
        result = run_residual_pass(pairs, target_lang="ja")
        self.assertEqual(result.checked_count, 0)
        self.assertTrue(result.clean)


class SeverityRoutingTest(unittest.TestCase):
    def test_quantity_unit_only_is_released_with_note(self):
        pairs = [("投资约 1.2 万欧元", "Investissement d'environ 1,2 万 euros")]
        result = run_residual_pass(pairs, target_lang="fr")
        self.assertEqual(len(result.released_notes), 1)
        self.assertEqual(
            result.released_notes[0].categories, (CATEGORY_QUANTITY_UNIT,)
        )
        self.assertEqual(result.needs_review, [])

    def test_term_fragment_needs_review(self):
        pairs = [
            (
                "沿裂缝开V型槽并清理浮灰。",
                "Ouvrir une rainure en V 型槽 le long de la fissure.",
            )
        ]
        result = run_residual_pass(pairs, target_lang="fr")
        self.assertEqual(len(result.needs_review), 1)
        self.assertIn(
            CATEGORY_TERM_FRAGMENT, result.needs_review[0].categories
        )
        self.assertEqual(result.fixes, {})


if __name__ == "__main__":
    unittest.main()
