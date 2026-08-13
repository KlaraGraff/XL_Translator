"""「数字 + 中文日期/数量单位」残留校验规则。

对应问题：中文施工方案翻译成法文后，法文译文里经常夹带中式日期写法，例如
"la date d'achèvement est fixée au 2026年8月9日"。根因是旧提示词把「编号必须
原样保留」和「日期必须原样保留」捆在一起，模型照做后把中文日期单位一起抄了
过去。本测试覆盖两部分：

1. core/translation_filter.py 新增的 residual_cn_date_unit 规则校验——正例
   （该抓的中文日期/数量单位残留）与反例（编号、国际单位、中文目标语言，绝不
   能误伤）。
2. config.py DOMAIN_PRESETS 与 core/mixed_language.py 的提示词措辞不再把
   「编号原样保留」和「日期原样保留」捆在一起表述。
"""

from __future__ import annotations

import unittest

from config import DOMAIN_PRESETS
from core.translation_filter import (
    VALIDATION_PROFILE_STRICT,
    VALIDATION_PROFILE_WORD_RECOVERY,
    validate_translation,
)


class ResidualChineseDateUnitPositiveCasesTests(unittest.TestCase):
    """该抓的 7 条真实例句（最后一条含两个子例句，共 8 个断言）。"""

    def _assert_fails_with_cn_date_unit(self, translated: str) -> None:
        result = validate_translation(
            "施工方案中的中文占位原文",
            translated,
            target_lang="fr",
            source_lang="zh",
        )
        self.assertTrue(result.is_fail, msg=f"应判为未通过：{translated!r}")
        codes = [issue.code for issue in result.issues]
        self.assertIn("residual_cn_date_unit", codes)

    def test_completion_date(self) -> None:
        self._assert_fails_with_cn_date_unit(
            "la date d'achèvement est fixée au 2026年8月9日"
        )

    def test_date_range_with_full_cn_dates(self) -> None:
        self._assert_fails_with_cn_date_unit(
            "reportée du 2026年8月9日 au 2027年1月25日"
        )

    def test_year_month_range_no_spaces(self) -> None:
        self._assert_fails_with_cn_date_unit(
            "De 2025年11月至2026年2月, précipitations importantes prévues"
        )

    def test_year_month_with_spaces(self) -> None:
        self._assert_fails_with_cn_date_unit(
            "À partir de 2025 年 11 月, le site sera réorganisé"
        )

    def test_month_day_with_spaces(self) -> None:
        self._assert_fails_with_cn_date_unit(
            "L'objectif d'achèvement au 10 月 31 日"
        )

    def test_month_range_dash(self) -> None:
        self._assert_fails_with_cn_date_unit(
            "7—9 月 correspond au pic annuel"
        )

    def test_age_limits_zhousui(self) -> None:
        self._assert_fails_with_cn_date_unit(
            "limites d'âge 18周岁 / 55周岁 / 50周岁"
        )

    def test_amount_yuan(self) -> None:
        self._assert_fails_with_cn_date_unit("20000元/人")


class ResidualChineseDateUnitNegativeCasesTests(unittest.TestCase):
    """绝不能误伤：编号数字、国际单位、中文目标语言。"""

    def _assert_passes(self, translated: str, *, target_lang: str = "fr") -> None:
        result = validate_translation(
            "占位原文，不含实际中文日期",
            translated,
            target_lang=target_lang,
            source_lang="zh",
        )
        codes = [issue.code for issue in result.issues]
        self.assertNotIn(
            "residual_cn_date_unit",
            codes,
            msg=f"不应被 residual_cn_date_unit 误伤：{translated!r}",
        )

    def test_project_code_btr_anode(self) -> None:
        self._assert_passes("Réf. BTR-ANODE-CCTEB-032")

    def test_project_code_btr_dzhfj(self) -> None:
        self._assert_passes("Réf. BTR-DZHFJ-ZB1-008")

    def test_axis_number_hash(self) -> None:
        self._assert_passes("Axe 1#")

    def test_axis_number_fullwidth_hash(self) -> None:
        self._assert_passes("Axe 2＃")

    def test_temperature_unit(self) -> None:
        self._assert_passes("La température atteint 38℃")

    def test_percentage_unit(self) -> None:
        self._assert_passes("Taux d'humidité de 30 %")

    def test_days_unit_169(self) -> None:
        self._assert_passes("Durée totale de 169 jours")

    def test_days_unit_86(self) -> None:
        self._assert_passes("Durée totale de 86 jours")

    def test_square_meter_unit(self) -> None:
        self._assert_passes("Surface de 120 m²")

    def test_combined_codes_and_units_sentence(self) -> None:
        # 真实场景里编号、国际单位常常混在同一句译文里，仍不应误伤。
        self._assert_passes(
            "L'axe 1# (Réf. BTR-ANODE-CCTEB-032) mesure 120 m² à 38℃ et 30 %."
        )

    def test_target_lang_zh_never_triggers_even_with_cn_dates(self) -> None:
        # 目标语言本身是中文时，规则完全不生效——译文里出现「2026年8月9日」
        # 是正常现象，不能被这条规则拦截。
        self._assert_passes("竣工日期为 2026年8月9日", target_lang="zh")

    def test_source_only_cn_dates_are_not_checked(self) -> None:
        # 规则只查译文，不查原文：原文里全是中文日期，只要译文本身干净就该通过。
        result = validate_translation(
            "竣工日期定于2026年8月9日，年龄限制为18周岁",
            "La date d'achèvement est fixée au 9 août 2026, limite d'âge 18 ans.",
            target_lang="fr",
            source_lang="zh",
        )
        codes = [issue.code for issue in result.issues]
        self.assertNotIn("residual_cn_date_unit", codes)


class ResidualChineseDateUnitTriggersRetryTests(unittest.TestCase):
    """确认新规则真的会触发既有的单段严格重试通路（VALIDATION_PROFILE_STRICT
    的 is_fail 是 Word 单段严格重试判定的依据），且 word_recovery 的软通过
    不会把这条问题悄悄放行。"""

    def test_strict_profile_fails(self) -> None:
        result = validate_translation(
            "竣工日期为2026年8月9日",
            "La date d'achèvement est le 2026年8月9日.",
            target_lang="fr",
            source_lang="zh",
            profile=VALIDATION_PROFILE_STRICT,
        )
        self.assertTrue(result.is_fail)

    def test_word_recovery_profile_does_not_soft_pass(self) -> None:
        result = validate_translation(
            "竣工日期为2026年8月9日",
            "La date d'achèvement est le 2026年8月9日.",
            target_lang="fr",
            source_lang="zh",
            profile=VALIDATION_PROFILE_WORD_RECOVERY,
        )
        # residual_cn_date_unit 是硬失败码，word_recovery 不得把它软化成
        # soft_pass_review——否则残留的中文日期会被当作「已恢复」直接写回文档。
        self.assertTrue(result.is_fail)


class PromptWordingSplitsNumberingFromDateUnitsTests(unittest.TestCase):
    """提示词措辞：编号「原样保留」与日期「换成目标语言书写习惯」必须分开表述，
    不能再用同一句「编号、日期...必须原样保留」把二者捆在一起。"""

    def _assert_split_wording(self, text: str) -> None:
        self.assertNotIn("编号、日期", text)
        self.assertNotIn("编号、文号、日期", text)
        # 日期应明确要求换成目标语言书写习惯，而不是原样保留。
        self.assertIn("目标语言的书写习惯", text)

    def test_sync_engineering_preset_base(self) -> None:
        self._assert_split_wording(DOMAIN_PRESETS["同步工程场景"]["_base"])

    def test_sync_engineering_preset_en(self) -> None:
        self._assert_split_wording(DOMAIN_PRESETS["同步工程场景"]["en"])

    def test_sync_engineering_preset_fr_uses_idiomatic_french(self) -> None:
        fr_text = DOMAIN_PRESETS["同步工程场景"]["fr"]
        self.assertNotIn("编号", fr_text)
        self.assertIn("selon l’usage du français", fr_text)
        self.assertIn("9 août 2026", fr_text)

    def test_document_management_preset_base(self) -> None:
        self._assert_split_wording(DOMAIN_PRESETS["资料管理场景"]["_base"])

    def test_document_management_preset_en(self) -> None:
        self._assert_split_wording(DOMAIN_PRESETS["资料管理场景"]["en"])

    def test_document_management_preset_fr_uses_idiomatic_french(self) -> None:
        fr_text = DOMAIN_PRESETS["资料管理场景"]["fr"]
        self.assertNotIn("dates, versions", fr_text)
        self.assertIn("selon l’usage du français", fr_text)

    def test_admin_daily_preset_base_keeps_identifiers_literal(self) -> None:
        text = DOMAIN_PRESETS["行政生活化场景"]["_base"]
        self.assertIn("原样保留", text)
        self.assertIn("目标语言的书写习惯", text)

    def test_admin_daily_preset_fr_uses_idiomatic_french(self) -> None:
        fr_text = DOMAIN_PRESETS["行政生活化场景"]["fr"]
        self.assertIn("selon l’usage du français", fr_text)


class MixedLanguagePromptWordingTests(unittest.TestCase):
    def test_mixed_language_prompt_splits_numbering_from_dates(self) -> None:
        from core.mixed_language import _build_mixed_translation_prompt

        prompt = _build_mixed_translation_prompt(
            target_lang="fr",
            source_lang="zh",
            base_prompt="",
            retry_hint=False,
        )
        self.assertNotIn("数字、日期、单位、编号", prompt)
        self.assertIn("目标语言的书写习惯", prompt)


if __name__ == "__main__":
    unittest.main()
