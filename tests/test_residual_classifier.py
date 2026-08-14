# -*- coding: utf-8 -*-
"""残留分类器与确定性修复的回归（设计文档 §3.1–§3.5、§五 的机器化版本）。

这套用例的立场：残留中文按「类」处置而不是按「量」放行。每个用例对应一类
真实失败（序号未译、术语尾巴复读、成句未译、标题写法两套），全部取材自
历史交付文档的实测形态——改这里的断言之前先回放语料，别拍脑袋。
"""

from __future__ import annotations

import unittest

from core.residual_classifier import (
    CATEGORY_CN_DATE_UNIT,
    CATEGORY_NUMBERING_PREFIX,
    CATEGORY_QUANTITY_UNIT,
    CATEGORY_SENTENCE_BLOCK,
    CATEGORY_TERM_FRAGMENT,
    CONVENTION_ARABIC_DOT,
    CONVENTION_PAREN_ARABIC,
    CONVENTION_PAREN_ROMAN,
    HEADING_FORM_SECTION_N,
    check_heading_consistency,
    classify_residual_spans,
    detect_sibling_convention,
    deterministic_numbering_fix,
    format_enum_label,
    is_section_heading_source,
    parse_cn_numeral,
    summarize_residuals,
    surgical_repair_ok,
)

# 实测三高发形态的代表句（内容为通用工程句式，不含任何项目/文件标识）
FR_TERM_TAIL = (
    "Mise en œuvre du matériau de scellement : utiliser le mortier sans retrait "
    "à haute résistance pour injection, remplir la rainure en V 型槽, lisser la "
    "surface et assurer une cure pendant au moins 3 jours."
)
FR_NUMBERING_RESIDUAL = (
    "（四）Fissures de classe IV : largeur comprise entre 1,0 et 2,0 mm, "
    "décalage local < 5,0 mm"
)


class ClassifierTest(unittest.TestCase):
    def test_clean_text_has_no_spans(self):
        self.assertEqual(
            classify_residual_spans(
                "Réparation des fissures du sol par injection.", target_lang="fr"
            ),
            [],
        )

    def test_target_zh_ja_exempt(self):
        # 与 _light_residual_chinese_issue 的豁免语言保持一致
        for lang in ("zh", "ja"):
            self.assertEqual(
                classify_residual_spans("地面裂缝修复方案", target_lang=lang), []
            )

    def test_numbering_prefix_paren_cn(self):
        spans = classify_residual_spans(FR_NUMBERING_RESIDUAL, target_lang="fr")
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0].category, CATEGORY_NUMBERING_PREFIX)
        self.assertEqual(spans[0].text, "（四）")

    def test_numbering_prefix_section_merges_multiple_cjk(self):
        # 「第1节」的「第」「节」两个 CJK 片段必须并成一条，而不是两条 term_fragment
        spans = classify_residual_spans(
            "第1节 Dispositions générales du projet", target_lang="fr"
        )
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0].category, CATEGORY_NUMBERING_PREFIX)
        self.assertEqual(spans[0].text, "第1节")

    def test_term_fragment_embedded_in_sentence(self):
        spans = classify_residual_spans(FR_TERM_TAIL, target_lang="fr")
        self.assertEqual([s.category for s in spans], [CATEGORY_TERM_FRAGMENT])
        self.assertEqual(spans[0].text, "型槽")

    def test_sentence_block_when_mostly_untranslated(self):
        spans = classify_residual_spans(
            "施工前应对基层进行全面检查并清理浮灰", target_lang="fr"
        )
        self.assertEqual([s.category for s in spans], [CATEGORY_SENTENCE_BLOCK])

    def test_short_residual_without_target_words_is_not_term_fragment(self):
        # 目标语单词不足 3 个：短残留没有「嵌在成句里」的证据，宁可按成句阻断
        spans = classify_residual_spans("V 型槽", target_lang="fr")
        self.assertEqual([s.category for s in spans], [CATEGORY_SENTENCE_BLOCK])

    def test_cn_date_unit_matches_legacy_semantics(self):
        spans = classify_residual_spans(
            "Le délai de garantie est de 2 年 selon le contrat signé.",
            target_lang="fr",
        )
        self.assertEqual([s.category for s in spans], [CATEGORY_CN_DATE_UNIT])

    def test_quantity_unit_released(self):
        spans = classify_residual_spans(
            "Investissement total : 1,2 万 EUR pour la phase initiale.",
            target_lang="fr",
        )
        self.assertEqual([s.category for s in spans], [CATEGORY_QUANTITY_UNIT])

    def test_summary_policy_mapping(self):
        blocked = summarize_residuals("施工前应对基层进行全面检查", target_lang="fr")
        self.assertTrue(blocked.blocking)
        self.assertFalse(blocked.releasable)

        repairable = summarize_residuals(FR_TERM_TAIL, target_lang="fr")
        self.assertFalse(repairable.blocking)
        self.assertTrue(repairable.repairable)

        clean = summarize_residuals("Texte entièrement traduit.", target_lang="fr")
        self.assertTrue(clean.releasable)
        self.assertEqual(clean.spans, ())


class CnNumeralTest(unittest.TestCase):
    def test_basic_and_compound(self):
        self.assertEqual(parse_cn_numeral("四"), 4)
        self.assertEqual(parse_cn_numeral("十"), 10)
        self.assertEqual(parse_cn_numeral("十二"), 12)
        self.assertEqual(parse_cn_numeral("二十"), 20)
        self.assertEqual(parse_cn_numeral("九十九"), 99)
        self.assertEqual(parse_cn_numeral("３"), 3)
        self.assertEqual(parse_cn_numeral("15"), 15)

    def test_rejects_garbage(self):
        for bad in ("", "零", "百", "十十", "一二"):
            self.assertIsNone(parse_cn_numeral(bad), bad)


class ConventionTest(unittest.TestCase):
    def test_votes_only_among_same_family_sources(self):
        # 正文步骤编号 1. 2. 3. 不许污染（X）族的惯例投票——PoC 实测踩过的坑
        pairs = [
            ("（一）表层裂缝", "(I) Fissures superficielles"),
            ("（二）结构裂缝", "(II) Fissures structurelles"),
            ("（三）贯穿裂缝", "(III) Fissures traversantes"),
            ("1. 清理基层", "1. Nettoyer le support"),
            ("2. 涂刷界面剂", "2. Appliquer l'agent d'interface"),
        ]
        self.assertEqual(detect_sibling_convention(pairs), CONVENTION_PAREN_ROMAN)

    def test_default_when_no_evidence(self):
        self.assertEqual(detect_sibling_convention([]), CONVENTION_PAREN_ARABIC)

    def test_deterministic_fix_aligns_with_convention(self):
        fixed = deterministic_numbering_fix(
            FR_NUMBERING_RESIDUAL, convention=CONVENTION_PAREN_ROMAN
        )
        self.assertIsNotNone(fixed)
        self.assertTrue(fixed.startswith("(IV) "))
        self.assertNotIn("（四）", fixed)
        # 正文一个字不许动（前缀替换 + 补一个分隔空格是仅有的改动）
        self.assertEqual(fixed[len("(IV) "):], FR_NUMBERING_RESIDUAL[len("（四）"):])

        self.assertEqual(
            deterministic_numbering_fix(
                "（二）Travaux préparatoires", convention=CONVENTION_PAREN_ARABIC
            ),
            "(2) Travaux préparatoires",
        )
        self.assertEqual(
            deterministic_numbering_fix(
                "（五）Contrôle qualité", convention=CONVENTION_ARABIC_DOT
            ),
            "5. Contrôle qualité",
        )

    def test_source_anchored_fix_restores_source_numbering(self):
        # 实测形态：源段「5.5.3 …」被译成「三、…」——编号被模型改掉了。
        # 按同级惯例翻译「三」是错的，唯一正确的修复是抄回源编号。
        self.assertEqual(
            deterministic_numbering_fix(
                "三、Mesures de sécurité de construction",
                convention=CONVENTION_PAREN_ARABIC,
                source_text="5.5.3 施工安全措施",
            ),
            "5.5.3 Mesures de sécurité de construction",
        )
        # 源锚点优先于惯例投票（源编号是最可信的证据）
        self.assertEqual(
            deterministic_numbering_fix(
                "（二）Préparation du chantier",
                convention=CONVENTION_PAREN_ROMAN,
                source_text="3.1施工准备",
            ),
            "3.1 Préparation du chantier",
        )
        # 单级数字带显式分隔符也算编号
        self.assertEqual(
            deterministic_numbering_fix(
                "一、Nettoyer le support",
                convention=CONVENTION_PAREN_ARABIC,
                source_text="7、清理基层",
            ),
            "7. Nettoyer le support",
        )

    def test_bare_dun_prefix_without_source_anchor_returns_none(self):
        # 裸「X、」残留：既无源编号可抄、也没有顿号族的同级译文证据——不猜
        self.assertIsNone(
            deterministic_numbering_fix(
                "三、Mesures de sécurité", convention=CONVENTION_PAREN_ARABIC
            )
        )

    def test_year_and_measure_sources_are_not_anchors(self):
        for source in ("2026年度计划", "3.5米宽通道两侧", "7 台设备清单"):
            self.assertIsNone(
                deterministic_numbering_fix(
                    "三、Texte traduit",
                    convention=CONVENTION_PAREN_ARABIC,
                    source_text=source,
                ),
                source,
            )

    def test_section_heading_fix_needs_known_language(self):
        fixed = deterministic_numbering_fix(
            "第1节 Dispositions générales",
            convention=CONVENTION_PAREN_ARABIC,
            target_lang="fr",
        )
        self.assertEqual(fixed, "Section 1 Dispositions générales")
        # 未收录语言：不猜，返回 None 走人工复核
        self.assertIsNone(
            deterministic_numbering_fix(
                "第1节 Общие положения",
                convention=CONVENTION_PAREN_ARABIC,
                target_lang="ru",
            )
        )

    def test_format_enum_label_bounds(self):
        self.assertEqual(format_enum_label(4, CONVENTION_PAREN_ROMAN), "(IV)")
        self.assertIsNone(format_enum_label(21, CONVENTION_PAREN_ROMAN))
        self.assertIsNone(format_enum_label(0, CONVENTION_PAREN_ARABIC))


class SurgicalValidatorTest(unittest.TestCase):
    """好修补放行、坏修补拒收，六个用例与 PoC 完全一致（设计文档 §3.3）。"""

    def setUp(self):
        self.original = FR_TERM_TAIL
        start = self.original.index("型槽")
        self.spans = [(start, 2)]

    def _check(self, repaired):
        return surgical_repair_ok(
            self.original, repaired, self.spans, target_lang="fr"
        )

    def test_good_repair_delete_residual(self):
        ok, why = self._check(self.original.replace(" 型槽", ""))
        self.assertTrue(ok, why)

    def test_good_repair_rewrite_at_spot(self):
        ok, why = self._check(
            self.original.replace("rainure en V 型槽", "rainure en V")
        )
        self.assertTrue(ok, why)

    def test_bad_repair_touches_distant_wording(self):
        ok, why = self._check(
            self.original.replace(" 型槽", "").replace("Mise en œuvre", "Application")
        )
        self.assertFalse(ok)
        self.assertIn("outside allowed window", why)

    def test_bad_repair_changes_number(self):
        ok, why = self._check(
            self.original.replace(" 型槽", "").replace("3 jours", "5 jours")
        )
        self.assertFalse(ok)

    def test_bad_repair_full_rewrite(self):
        ok, _why = self._check(
            "Remplir la rainure en V avec le mortier et curer 3 jours."
        )
        self.assertFalse(ok)

    def test_bad_repair_residual_still_present(self):
        ok, why = self._check(self.original)
        self.assertFalse(ok)
        self.assertIn("型槽", why)


class HeadingConsistencyTest(unittest.TestCase):
    """两套节标题写法的文档级聚类：逐段全对、全篇才看得出的失败形态。"""

    OBSERVATIONS = [
        ("第一节 总则", "Section 1 Dispositions générales", "p10"),
        ("第二节 工程概况", "Deuxième section — Aperçu des travaux", "p20"),
        ("第三节 施工方案", "Section 3 Plan d'exécution", "p30"),
        ("第四节 质量保证", "Quatrième section — Assurance qualité", "p40"),
        ("第五节 安全措施", "Section 5 Mesures de sécurité", "p50"),
        ("第六节 环境保护", "Section 6 Protection de l'environnement", "p60"),
        ("第七节 应急预案", "Septième section — Plan d'urgence", "p70"),
    ]

    def test_source_heading_detection(self):
        self.assertTrue(is_section_heading_source("第三节 施工方案"))
        self.assertTrue(is_section_heading_source("第１２章 附则"))
        self.assertFalse(is_section_heading_source("（三）施工方案"))
        self.assertFalse(is_section_heading_source("3.1 施工准备"))

    def test_majority_and_outliers(self):
        result = check_heading_consistency(self.OBSERVATIONS, target_lang="fr")
        self.assertEqual(result.majority_form, HEADING_FORM_SECTION_N)
        self.assertEqual(
            sorted(item.unit_key for item in result.outliers), ["p20", "p40", "p70"]
        )
        self.assertEqual(
            result.fixes["p20"], "Section 2 Aperçu des travaux"
        )
        self.assertEqual(
            result.fixes["p40"], "Section 4 Assurance qualité"
        )
        self.assertEqual(
            result.fixes["p70"], "Section 7 Plan d'urgence"
        )

    def test_uniform_document_has_no_outliers(self):
        uniform = [
            (src, f"Section {i + 1} X", key)
            for i, (src, _tgt, key) in enumerate(self.OBSERVATIONS)
        ]
        result = check_heading_consistency(uniform, target_lang="fr")
        self.assertEqual(result.outliers, ())
        self.assertEqual(result.fixes, {})

    def test_majority_ordinal_word_reports_without_rewrite(self):
        # 多数派是序数词写法时需要词形知识，只报告不代改（设计定的保守边界）
        observations = [
            ("第一节 总则", "Première section — Dispositions générales", "p1"),
            ("第二节 概况", "Deuxième section — Aperçu", "p2"),
            ("第三节 方案", "Section 3 Plan", "p3"),
        ]
        result = check_heading_consistency(observations, target_lang="fr")
        self.assertEqual(len(result.outliers), 1)
        self.assertEqual(result.fixes, {})


if __name__ == "__main__":
    unittest.main()
