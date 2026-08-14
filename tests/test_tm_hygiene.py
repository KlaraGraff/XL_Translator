# -*- coding: utf-8 -*-
"""TM 入库卫生与存量惯例归一的回归（设计文档 §3.7，Word/Excel 共用）。

两条写入前规则 + 一条存量清洗规则：
1. 写入前惯例归一：序号前缀按批次惯例修复、节标题写法按多数派归一后再入库；
2. 写入前分类器复检：带阻断级残留的配对拦下不入库，且必须给出可读原因；
3. tm_cleaner 惯例归一建议：库内节标题离群写法生成确定性建议（0 API），
   沿用「先建议、用户确认后写入」流程，同一条目确定性建议压过模型建议。
"""

from __future__ import annotations

import unittest
from unittest import mock

from core.tm_cleaner import CleanSuggestion, build_convention_suggestions, run_cleaning
from core.tm_hygiene import sanitize_tm_pairs


class SanitizeNormalizationTest(unittest.TestCase):
    def test_numbering_prefix_normalized_by_batch_convention(self):
        pairs = [
            ("（一）表层裂缝", "(I) Fissures superficielles"),
            ("（二）结构裂缝", "(II) Fissures structurelles"),
            ("（三）贯穿裂缝", "(III) Fissures traversantes"),
            ("（四）沉降裂缝", "（四）Fissures de tassement"),
        ]
        result = sanitize_tm_pairs(pairs, target_lang="fr")
        stored = dict(result.pairs)
        self.assertEqual(stored["（四）沉降裂缝"], "(IV) Fissures de tassement")
        self.assertEqual(result.normalized, ("（四）沉降裂缝",))
        self.assertEqual(result.rejected, ())

    def test_heading_outlier_normalized_to_majority_form(self):
        # 库里实存过「Section N」与「Nième section」两套写法：多数派为准
        pairs = [
            ("第一节 总则", "Section 1 Dispositions générales"),
            ("第二节 施工准备", "Section 2 Préparation du chantier"),
            ("第三节 质量控制", "Troisième section — Contrôle de la qualité"),
        ]
        result = sanitize_tm_pairs(pairs, target_lang="fr")
        stored = dict(result.pairs)
        self.assertEqual(
            stored["第三节 质量控制"], "Section 3 Contrôle de la qualité"
        )
        self.assertIn("第三节 质量控制", result.normalized)

    def test_blocking_residual_is_rejected_with_reason(self):
        pairs = [
            ("养护要求", "Exigences de cure"),
            (
                "沿裂缝开V型槽并清理浮灰。",
                "Ouvrir une rainure en V 型槽 le long de la fissure.",
            ),
        ]
        result = sanitize_tm_pairs(pairs, target_lang="fr")
        self.assertEqual(dict(result.pairs), {"养护要求": "Exigences de cure"})
        self.assertEqual(len(result.rejected), 1)
        source, reason = result.rejected[0]
        self.assertEqual(source, "沿裂缝开V型槽并清理浮灰。")
        self.assertIn("«型槽»", reason)

    def test_quantity_unit_only_is_still_stored(self):
        # 万/亿按设计放行：拦下会把大量正常财务词条挡在库外
        pairs = [("投资约 1.2 万欧元", "Investissement d'environ 1,2 万 euros")]
        result = sanitize_tm_pairs(pairs, target_lang="fr")
        self.assertEqual(len(result.pairs), 1)
        self.assertEqual(result.rejected, ())

    def test_exempt_target_lang_passes_through(self):
        pairs = [("（四）沉降裂缝", "（四）沈下亀裂")]
        result = sanitize_tm_pairs(pairs, target_lang="ja")
        self.assertEqual(result.pairs, tuple(pairs))
        self.assertEqual(result.normalized, ())
        self.assertEqual(result.rejected, ())


def _entry(entry_id: int, source: str, target: str) -> dict:
    return {
        "id": entry_id,
        "source_text": source,
        "target_text": target,
        "lang_pair": "zh-fr",
        "version": f"v{entry_id}",
    }


_HEADING_ENTRIES = [
    _entry(1, "第一节 总则", "Section 1 Dispositions générales"),
    _entry(2, "第二节 施工准备", "Section 2 Préparation du chantier"),
    _entry(3, "第三节 施工工艺", "Section 3 Procédés de construction"),
    _entry(4, "第四节 质量控制", "Section 4 Contrôle de la qualité"),
    _entry(5, "第五节 安全文明施工", "Cinquième section — Sécurité du chantier"),
    _entry(6, "第六节 环境保护", "Sixième section — Protection de l'environnement"),
    _entry(7, "第七节 应急预案", "Septième section — Plan d'urgence"),
]


class ConventionSuggestionTest(unittest.TestCase):
    def test_outliers_get_prefix_only_suggestions(self):
        suggestions = build_convention_suggestions("zh-fr", entries=_HEADING_ENTRIES)
        by_id = {s.entry_id: s for s in suggestions}
        # 多数派 Section N（4:3），三条序数词写法各得一条仅改前缀的建议
        self.assertEqual(sorted(by_id), [5, 6, 7])
        self.assertEqual(
            by_id[5].new_target, "Section 5 Sécurité du chantier"
        )
        self.assertEqual(by_id[5].old_target, "Cinquième section — Sécurité du chantier")
        self.assertEqual(by_id[5].lang_pair, "zh-fr")
        self.assertEqual(by_id[5].expected_version, "v5")

    def test_ordinal_majority_yields_no_deterministic_rewrite(self):
        # 归一到序数词写法需要词形变化知识：只报告，不生成建议
        entries = [
            _entry(1, "第一节 总则", "Première section — Dispositions générales"),
            _entry(2, "第二节 施工准备", "Deuxième section — Préparation"),
            _entry(3, "第三节 工艺", "Section 3 Procédés"),
        ]
        suggestions = build_convention_suggestions("zh-fr", entries=entries)
        self.assertEqual(suggestions, [])

    def test_single_heading_entry_is_left_alone(self):
        suggestions = build_convention_suggestions(
            "zh-fr", entries=[_HEADING_ENTRIES[0]]
        )
        self.assertEqual(suggestions, [])


class RunCleaningMergeTest(unittest.TestCase):
    def test_convention_suggestion_wins_over_model_suggestion(self):
        # 同一条目：确定性惯例建议为准，模型建议让位；其余模型建议保留
        model_only = CleanSuggestion(
            entry_id=1,
            source_text="第一节 总则",
            old_target="Section 1 Dispositions générales",
            new_target="Section 1 Dispositions générales (rev)",
        )
        conflicting = CleanSuggestion(
            entry_id=5,
            source_text="第五节 安全文明施工",
            old_target="Cinquième section — Sécurité du chantier",
            new_target="Une réécriture complète du modèle",
        )
        with (
            mock.patch(
                "core.tm_cleaner.tm_manager.get_all_entries_for_cleaning",
                return_value=list(_HEADING_ENTRIES),
            ),
            mock.patch(
                "core.tm_cleaner._run_cleaning_threaded",
                return_value=[model_only, conflicting],
            ),
            mock.patch("core.tm_cleaner.is_local_engine_name", return_value=False),
            # 惯例建议会即时入库（读 pending 去重 + 写建议表）；测试不碰真实
            # 用户库，两个入库口都替换掉
            mock.patch(
                "core.tm_cleaner.tm_manager.list_cleaning_suggestions",
                return_value=[],
            ),
            mock.patch(
                "core.tm_cleaner.tm_manager.persist_cleaning_suggestions",
                return_value=3,
            ) as persist_mock,
        ):
            fake_engine = mock.Mock()
            fake_engine.engine_name = "FakeCloudEngine"
            merged = run_cleaning("zh-fr", engine=fake_engine)
        by_id = {s.entry_id: s for s in merged}
        self.assertEqual(sorted(by_id), [1, 5, 6, 7])
        self.assertEqual(by_id[5].new_target, "Section 5 Sécurité du chantier")
        self.assertEqual(by_id[1].new_target, "Section 1 Dispositions générales (rev)")
        # 惯例建议应当在模型批次之前就已入库（entry 5/6/7 三条）
        persist_mock.assert_called_once()
        persisted_ids = sorted(
            item["entry_id"] for item in persist_mock.call_args.args[0]
        )
        self.assertEqual(persisted_ids, [5, 6, 7])


if __name__ == "__main__":
    unittest.main()
