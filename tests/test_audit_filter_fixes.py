"""2026-08-29 全仓审查 · A1-filter 集群的回归钉子。

覆盖三条：

- 高-1 日译中/韩译中：日文汉字落在 `一-龥` 码区里，旧判据「含汉字 → 已经是
  中文 → 跳过」把 `工事契約書` 这类纯汉字标题整条踢出流水线——不抽取、不
  翻译、也不进报告。
- 高-2 中译日：`_residual_cn_date_unit_issue` 没按 residual_classifier 的目标语
  豁免表放过 ja，`本工事は2026年8月9日に完了する` 被判 fail，engine_dispatcher
  随即把正确译文重置回中文原文。
- 中-11 数字 token 只认 ASCII 数字与「逗号换点」，`1500天 → 1 500 jours`
  （法语标准千分位写法）被判 missing_number；同一套 token 逻辑在
  residual_classifier / residual_repair 上把修复稿一并打回。

第二轮（对抗审查开出的整改）：

- A1-1 普通空格既是千分位也是普通分隔符，`尺寸 100 200` 被并成一个数
  100200，译文换个分隔符（100/200）就永远配不上 → 又一类假失败。
- A1-3 超过 28 位有效数字的 token 在 decimal 默认精度下解析失败，键落空，
  配对器把空键当成「必然丢了」，一字未改的译文照样被判 missing_number。
- 片假名中点 `・` 与长音符 `ー` 不该单独充当「这不是中文」的证据。

这些用例钉的是「行为」：某个语言对下该不该翻、某段译文该不该放行，不钉
实现（正则长相、内部函数名）。
"""

from __future__ import annotations

import unittest

from core.residual_classifier import (
    canonical_number_keys,
    extract_number_tokens,
    missing_number_tokens,
    surgical_repair_ok,
)
from core.residual_repair import verify_feedback_retranslation
from core.translation_filter import (
    VALIDATION_PROFILE_STRICT,
    VALIDATION_PROFILE_WORD_RECOVERY,
    _check_semantic_numbers_intact,
    is_translation_redundant,
    should_translate,
    validate_translation,
)


class JapaneseSourceToChineseTests(unittest.TestCase):
    """高-1：源语言是日文时，汉字是待译原文而不是「已经是中文」。"""

    KANJI_ONLY = ("工事契約書", "施工計画", "安全管理体制", "工期")
    WITH_KANA = ("工事の契約", "おしらせ", "コンクリート打設")

    def test_kanji_only_headings_are_translated(self) -> None:
        for text in self.KANJI_ONLY:
            with self.subTest(text=text):
                self.assertTrue(
                    should_translate(text, target_lang="zh", source_lang="ja"),
                    msg="日译中时纯汉字标题必须进翻译流水线",
                )

    def test_kanji_only_headings_are_translated_for_korean_source(self) -> None:
        # 韩文汉字同样落在 一-龥 码区，判据按语言对走，两边一致。
        for text in self.KANJI_ONLY:
            with self.subTest(text=text):
                self.assertTrue(
                    should_translate(text, target_lang="zh", source_lang="ko")
                )

    def test_kana_and_hangul_are_translated_regardless_of_declared_source(self) -> None:
        # 假名/谚文是无歧义证据：出现即不是中文，源语言选错也不该漏译。
        for text in self.WITH_KANA + ("계약서", "건설공사"):
            for source_lang in ("ja", "ko", "auto", "zh", "en"):
                with self.subTest(text=text, source_lang=source_lang):
                    self.assertTrue(
                        should_translate(
                            text, target_lang="zh", source_lang=source_lang
                        )
                    )

    def test_region_tagged_source_codes_are_recognised(self) -> None:
        for source_lang in ("ja-JP", "JA", " ja "):
            with self.subTest(source_lang=source_lang):
                self.assertTrue(
                    should_translate(
                        "工事契約書", target_lang="zh", source_lang=source_lang
                    )
                )

    def test_numbers_and_model_codes_stay_skipped_for_japanese_source(self) -> None:
        # 放行只针对「源语言的文字」，纯数字/符号与型号代码照旧不送翻译。
        for text in ("123", "±5%", "A3B12", "DN100", "  "):
            with self.subTest(text=text):
                self.assertFalse(
                    should_translate(text, target_lang="zh", source_lang="ja")
                )


class ChineseTargetRegressionTests(unittest.TestCase):
    """高-1 的反面：其它语言对上「中文已经是目标语言」的跳过必须原样保留。"""

    def test_chinese_cell_is_still_skipped_for_non_han_sources(self) -> None:
        for source_lang in ("en", "fr", "ru", "th", "auto", "zh", ""):
            with self.subTest(source_lang=source_lang):
                self.assertFalse(
                    should_translate(
                        "工程概况", target_lang="zh", source_lang=source_lang
                    ),
                    msg="外译中时已有中文的单元格不该被重复翻译",
                )

    def test_foreign_text_to_chinese_is_unchanged(self) -> None:
        for text in ("Договор", "สัญญาก่อสร้างอาคาร", "Straße", "Conditions générales"):
            with self.subTest(text=text):
                self.assertTrue(
                    should_translate(text, target_lang="zh", source_lang="auto")
                )

    def test_chinese_to_other_targets_is_unchanged(self) -> None:
        for target_lang in ("en", "fr", "ja", "ko", "de"):
            with self.subTest(target_lang=target_lang):
                self.assertTrue(
                    should_translate(
                        "工程概况", target_lang=target_lang, source_lang="zh"
                    )
                )
                self.assertFalse(
                    should_translate("DN", target_lang=target_lang, source_lang="zh")
                )


class ChineseToJapaneseDateUnitTests(unittest.TestCase):
    """高-2：日文本来就写「2026年8月9日」，这条残留规则对 ja 必须整条豁免。"""

    JA_SENTENCES = (
        "本工事は2026年8月9日に完了する",
        "工期は30日間、年齢制限は18周岁とする",
        "契約金額は20000元とする",
    )

    def test_japanese_translations_with_cn_date_units_pass(self) -> None:
        for translated in self.JA_SENTENCES:
            with self.subTest(translated=translated):
                result = validate_translation(
                    "本工程于2026年8月9日完工，工期30日，年龄限制18周岁，金额20000元",
                    translated,
                    target_lang="ja",
                    source_lang="zh",
                )
                codes = [issue.code for issue in result.issues]
                self.assertNotIn("residual_cn_date_unit", codes)

    def test_dispatcher_gate_does_not_reset_japanese_translation(self) -> None:
        # engine_dispatcher 用 is_translation_redundant 决定是否把译文重置回原文。
        self.assertFalse(
            is_translation_redundant(
                "本工程于2026年8月9日完工",
                "本工事は2026年8月9日に完了する",
                target_lang="ja",
                source_lang="zh",
            )
        )

    def test_word_recovery_profile_does_not_fail_japanese(self) -> None:
        result = validate_translation(
            "本工程于2026年8月9日完工",
            "本工事は2026年8月9日に完了する",
            target_lang="ja",
            source_lang="zh",
            profile=VALIDATION_PROFILE_WORD_RECOVERY,
        )
        self.assertFalse(result.is_fail)

    def test_region_tagged_japanese_target_is_exempt_too(self) -> None:
        result = validate_translation(
            "本工程于2026年8月9日完工",
            "本工事は2026年8月9日に完了する",
            target_lang="ja-JP",
            source_lang="zh",
        )
        self.assertFalse(result.is_fail)

    def test_french_target_still_fails(self) -> None:
        # 豁免只给 zh / ja；法语译文里夹中文日期依旧要打回重译。
        result = validate_translation(
            "竣工日期为2026年8月9日",
            "La date d'achèvement est le 2026年8月9日.",
            target_lang="fr",
            source_lang="zh",
            profile=VALIDATION_PROFILE_STRICT,
        )
        self.assertTrue(result.is_fail)
        self.assertIn(
            "residual_cn_date_unit", [issue.code for issue in result.issues]
        )


class NumberWritingSystemTests(unittest.TestCase):
    """中-11：同一个数值的各国写法必须算「数字没丢」。"""

    EQUIVALENT_1500 = (
        ("fr", "délai total de 1 500 jours"),          # 普通空格千分位
        ("fr", "délai total de 1 500 jours"),     # 不间断空格
        ("fr", "délai total de 1 500 jours"),     # 窄不间断空格
        ("en", "total duration of 1,500 days"),        # 英式逗号千分位
        ("de", "Gesamtdauer von 1.500 Tagen"),         # 德式点千分位
        ("de", "Gesamtdauer von 1'500 Tagen"),         # 瑞士撇号千分位
        ("ar", "١٥٠٠ يوم"),                            # 阿拉伯-印度数字
        ("ja", "１５００日間"),                          # 全角数字
    )

    def test_grouped_and_non_ascii_numbers_are_recognised(self) -> None:
        for target_lang, translated in self.EQUIVALENT_1500:
            with self.subTest(target_lang=target_lang, translated=translated):
                result = validate_translation(
                    "总工期1500天",
                    translated,
                    target_lang=target_lang,
                    source_lang="zh",
                )
                self.assertNotIn(
                    "missing_number", [issue.code for issue in result.issues]
                )

    def test_decimal_comma_and_trailing_zeros_match(self) -> None:
        result = validate_translation(
            "总价1234567.89元，坍落度180mm",
            "Montant de 1 234 567,89 EUR, affaissement de 180,00 mm",
            target_lang="fr",
            source_lang="zh",
        )
        self.assertNotIn("missing_number", [issue.code for issue in result.issues])

    def test_a_genuinely_dropped_number_still_fails(self) -> None:
        result = validate_translation(
            "总工期1500天，共3层",
            "délai total de 500 jours",
            target_lang="fr",
            source_lang="zh",
        )
        self.assertTrue(result.is_fail)
        self.assertIn("missing_number", [issue.code for issue in result.issues])

    def test_repeated_numbers_are_counted_one_for_one(self) -> None:
        # 原文两个 3、译文只剩一个 → 仍算丢（配对是一对一消耗，不是集合包含）。
        result = validate_translation(
            "3层3跨",
            "3 niveaux et deux travées",
            target_lang="fr",
            source_lang="zh",
        )
        self.assertIn("missing_number", [issue.code for issue in result.issues])

    def test_wan_scale_still_resolves_under_word_recovery(self) -> None:
        result = validate_translation(
            "合同总价3万元",
            "Montant total du contrat : 30 000 yuans",
            target_lang="fr",
            source_lang="zh",
            profile=VALIDATION_PROFILE_WORD_RECOVERY,
        )
        self.assertFalse(result.is_fail)

    def test_canonical_keys_keep_both_readings_of_an_ambiguous_token(self) -> None:
        # 1,500 在英文里是 1500、在法文里是 1.5：两种读法都保留，比对时任一命中即可。
        self.assertEqual(canonical_number_keys("1,500"), frozenset({"1500", "1.5"}))
        self.assertEqual(canonical_number_keys("1 500"), frozenset({"1500"}))
        self.assertEqual(canonical_number_keys("3.5"), frozenset({"3.5"}))
        self.assertEqual(canonical_number_keys("１５００"), frozenset({"1500"}))

    def test_group_separator_needs_exactly_three_digits(self) -> None:
        # "1 5000" 是两个数，不是一个千分位写法。
        self.assertEqual(extract_number_tokens("1 5000"), ["1", "5000"])
        self.assertEqual(extract_number_tokens("de 3 a 5 m"), ["3", "5"])


class RepairLadderNumberGateTests(unittest.TestCase):
    """中-11 同源：修复阶梯的两道数字闸门用同一套归一，别把修复稿打回。"""

    def test_missing_number_tokens_matches_across_writings(self) -> None:
        self.assertEqual(missing_number_tokens("工期1500天", "1 500 jours"), [])
        self.assertEqual(missing_number_tokens("工期1500天", "500 jours"), ["1500"])

    def test_feedback_retranslation_accepts_local_thousands_separator(self) -> None:
        ok, why = verify_feedback_retranslation(
            "总工期1500天，混凝土养护14天。",
            "Le délai total est de 1 500 jours, avec 14 jours de cure du béton.",
            target_lang="fr",
        )
        self.assertTrue(ok, why)

    def test_feedback_retranslation_still_rejects_changed_numbers(self) -> None:
        ok, why = verify_feedback_retranslation(
            "总工期1500天。",
            "Le délai total est de 1 200 jours.",
            target_lang="fr",
        )
        self.assertFalse(ok)
        self.assertIn("1500", why)

    def test_surgical_repair_accepts_reformatted_thousands(self) -> None:
        ok, why = surgical_repair_ok(
            "Remplir la 型槽 sur 1 500 mm.",
            "Remplir la rainure en V sur 1 500 mm.",
            [(11, 2)],
            target_lang="fr",
        )
        self.assertTrue(ok, why)

    def test_surgical_repair_still_rejects_number_swap(self) -> None:
        ok, why = surgical_repair_ok(
            "型槽 de 3 a 5 m",
            "Rainure de 5 a 3 m",
            [(0, 2)],
            target_lang="fr",
        )
        self.assertFalse(ok)
        self.assertIn("numbers changed", why)


class AdjacentNumbersSeparatedBySpaceTests(unittest.TestCase):
    """A1-1：空格既是千分位也是普通分隔符，挨着写的两个数不能被并成一个。

    中-11 的修法把普通空格列进千分位分隔符，于是 `尺寸 100 200` 在源文侧
    变成一个数 100200；译文只要换了分隔符（100/200、100, 200）就配不上，
    正确译文被判 missing_number 后由 engine_dispatcher 重置回原文——正是
    中-11 要消灭的那类假失败换了个形状。
    """

    def test_adjacent_numbers_survive_a_separator_change(self) -> None:
        for translated in (
            "Dimensions 100/200",
            "Dimensions: 100, 200",
            "Size 100 x 200 mm",
            "Dimensions 100-200",
        ):
            with self.subTest(translated=translated):
                result = validate_translation(
                    "尺寸 100 200",
                    translated,
                    target_lang="en",
                    source_lang="zh",
                )
                self.assertNotIn(
                    "missing_number", [issue.code for issue in result.issues]
                )

    def test_three_adjacent_numbers_survive(self) -> None:
        result = validate_translation(
            "规格：50 100 150",
            "Spec: 50, 100, 150",
            target_lang="en",
            source_lang="zh",
        )
        self.assertNotIn("missing_number", [issue.code for issue in result.issues])

    def test_space_grouped_thousands_still_match(self) -> None:
        # 反向守卫：中-11 修好的千分位配对不能被这轮改回去。
        self.assertEqual(missing_number_tokens("工期1500天", "1 500 jours"), [])
        self.assertEqual(
            missing_number_tokens("总价1234567元", "1 234 567 EUR"), []
        )

    def test_dropped_adjacent_number_still_fails(self) -> None:
        result = validate_translation(
            "尺寸 100 200",
            "Dimension 100",
            target_lang="en",
            source_lang="zh",
        )
        self.assertTrue(result.is_fail)
        self.assertIn("missing_number", [issue.code for issue in result.issues])

    def test_split_source_matches_space_grouped_target(self) -> None:
        # 反方向同样要通：源文写「100、200」，译文写成「100 200」。
        self.assertEqual(
            missing_number_tokens("尺寸 100、200", "Dimensions 100 200"), []
        )

    def test_feedback_retranslation_accepts_adjacent_numbers(self) -> None:
        ok, why = verify_feedback_retranslation(
            "型槽尺寸 100 200 毫米。",
            "Rainure de 100/200 mm.",
            target_lang="fr",
        )
        self.assertTrue(ok, why)

    def test_feedback_retranslation_still_rejects_dropped_adjacent_number(self) -> None:
        ok, why = verify_feedback_retranslation(
            "型槽尺寸 100 200 毫米。",
            "Rainure de 100 mm.",
            target_lang="fr",
        )
        self.assertFalse(ok)

    def test_surgical_repair_accepts_reformatted_adjacent_numbers(self) -> None:
        ok, why = surgical_repair_ok(
            "型槽 100 200 mm",
            "Rainure en V 100/200 mm",
            [(0, 2)],
            target_lang="fr",
        )
        self.assertTrue(ok, why)

    def test_surgical_repair_still_rejects_number_swap_across_spaces(self) -> None:
        ok, why = surgical_repair_ok(
            "型槽 100 200 mm",
            "Rainure en V 200/100 mm",
            [(0, 2)],
            target_lang="fr",
        )
        self.assertFalse(ok)
        self.assertIn("numbers changed", why)

    def test_word_recovery_rescue_gate_uses_the_same_reading(self) -> None:
        # word_recovery 的语义数字兜底闸门与 _missing_number_tokens 必须同判据，
        # 否则「严格校验与分类器各有一套规则」会重新长出来（高-2 的根因形状）。
        self.assertTrue(
            _check_semantic_numbers_intact("尺寸 100 200 毫米", "Dimensions 100/200 mm")
        )
        self.assertTrue(
            _check_semantic_numbers_intact("总工期1500天", "délai de 1 500 jours")
        )
        self.assertFalse(
            _check_semantic_numbers_intact("尺寸 100 200 毫米", "Dimensions 100 mm")
        )

    def test_wan_scale_applies_only_to_the_adjacent_segment(self) -> None:
        # 「100 200万」读作 100 与 200万，不是 100万 与 200万。
        self.assertTrue(
            _check_semantic_numbers_intact("产量 100 200万吨", "Output 100 / 2 000 000 t")
        )
        self.assertFalse(
            _check_semantic_numbers_intact("产量 100 200万吨", "Output 100 / 200 t")
        )


class LongDigitRunTests(unittest.TestCase):
    """A1-3：29 位以上的数字 token 不许解析成空键。

    decimal 默认上下文只有 28 位有效数字，normalize/quantize 会抛
    InvalidOperation；空键在配对器里等价于「必然配不上」，于是一字未改的
    长流水号被判 missing_number，正确译文被重置回原文。
    """

    LONG_DIGITS = "123456789012345678901234567890"

    def test_long_account_number_kept_verbatim_is_not_missing(self) -> None:
        result = validate_translation(
            f"账号 {self.LONG_DIGITS} 元",
            f"Account {self.LONG_DIGITS} EUR",
            target_lang="en",
            source_lang="zh",
        )
        self.assertNotIn("missing_number", [issue.code for issue in result.issues])

    def test_canonical_keys_are_never_empty(self) -> None:
        for token in ("1" * 29, "9" * 40, LongDigitRunTests.LONG_DIGITS):
            with self.subTest(token=token):
                self.assertTrue(canonical_number_keys(token))

    def test_long_number_actually_changed_still_fails(self) -> None:
        result = validate_translation(
            f"账号 {self.LONG_DIGITS} 元",
            f"Account {self.LONG_DIGITS[:-1]}9 EUR",
            target_lang="en",
            source_lang="zh",
        )
        self.assertTrue(result.is_fail)
        self.assertIn("missing_number", [issue.code for issue in result.issues])

    def test_repair_gate_accepts_long_number_kept_verbatim(self) -> None:
        self.assertEqual(
            missing_number_tokens(
                f"账号{self.LONG_DIGITS}的付款", f"paiement {self.LONG_DIGITS}"
            ),
            [],
        )


class KanaEvidenceRangeTests(unittest.TestCase):
    """审查的次要观察：中点 ・ 与长音符 ー 不是「这段不是中文」的证据。"""

    def test_middle_dot_and_prolonged_mark_do_not_reopen_chinese_cells(self) -> None:
        for text in ("皮埃尔・卡尔丹", "圣罗兰・巴黎工程部", "长音符ー的用法"):
            with self.subTest(text=text):
                self.assertFalse(
                    should_translate(text, target_lang="zh", source_lang="auto"),
                    msg="外译中时已有中文的单元格不该被重复送翻",
                )

    def test_real_kana_and_hangul_are_still_evidence(self) -> None:
        for text in ("コンクリート打設", "サーバー室", "おしらせ", "계약서"):
            with self.subTest(text=text):
                self.assertTrue(
                    should_translate(text, target_lang="zh", source_lang="auto")
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
