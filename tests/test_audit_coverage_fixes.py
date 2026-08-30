"""审计 2026-08-29 覆盖率三条（高-4 / 中-12 / 中-13）的回归。

三条的共同根因是同一处：补译判定里「这段文字是哪门语言」只有 en/fr 一对标记词，
其他语言一律没话说，而「没话说」被当成了「确定不是」。后果分三种：

* 高-4：英译中里 ``Société Générale Contract`` 因为带 é 被判成「法语，所以不是英语」，
  既不算源文也不算译文 → 静默 ignored，不翻译、也不进报告。
* 中-12：中译日里已经翻好的日文格（``工事は…に完了する``）被当成中文原文，
  整表再补一条重复译文。
* 中-13：英译德这种同字母语言对，两行都是原文时第二行照样满足「有 3 个字母以上的
  自然语言词」，必然被当成译文，整格判 covered → 引擎不支持仲裁时彻底漏译。

判错的代价不对称（见 core/coverage_arbitration 开头）：多翻一条看得见、删得掉，
漏译一条没人知道。所以这些用例钉的是「拿不准时要落到 source_only」，不是「必须判对」。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from core.excel_coverage import build_excel_coverage_plan
from core.translation_coverage import (
    COVERAGE_COVERED,
    COVERAGE_IGNORED,
    COVERAGE_SOURCE_ONLY,
    _is_same_alphabet_pair,
    _looks_translated_despite_cjk,
    _script_evidence,
    contains_kana,
    looks_like_source_text,
    looks_like_target_text,
    split_existing_bilingual_text,
)


class _WorkbookCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)

    def _plan(self, cells: dict[str, str], *, source_lang: str, target_lang: str) -> dict[str, str]:
        """建一份单分表 xlsx，走完整补译扫描，返回 {坐标: 覆盖状态}。"""
        path = self.root / f"{source_lang}_{target_lang}.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Sheet1"
        for coordinate, value in cells.items():
            sheet[coordinate] = value
        workbook.save(str(path))
        plan = build_excel_coverage_plan(
            path,
            target_lang=target_lang,
            source_lang=source_lang,
        )
        return {
            str(unit.data.get("coordinate")): unit.status
            for unit in plan.units
            if unit.data.get("coordinate")
        }


class ForeignFlavouredSourceTests(_WorkbookCase):
    """高-4：证据不足时不许静默跳过。"""

    def test_french_flavoured_english_cells_are_still_translated(self) -> None:
        statuses = self._plan(
            {
                "A1": "Société Générale Contract",
                "A2": "Café Manager",
                "A3": "Payment schedule",
            },
            source_lang="en",
            target_lang="zh",
        )
        for coordinate in ("A1", "A2", "A3"):
            self.assertEqual(statuses[coordinate], COVERAGE_SOURCE_ONLY, coordinate)

    def test_diacritics_alone_do_not_disqualify_source_text(self) -> None:
        for text in ("Société Générale Contract", "Café Manager"):
            self.assertTrue(
                looks_like_source_text(text, source_lang="en", target_lang="zh"),
                text,
            )
            self.assertFalse(
                looks_like_target_text(text, source_lang="en", target_lang="zh"),
                text,
            )

    def test_latin_text_is_not_a_translation_into_a_non_latin_target(self) -> None:
        """英译俄：一整格英文不能因为「有自然语言词」就算成俄文译文。"""
        for target in ("ru", "ko", "ar", "el", "th"):
            self.assertFalse(
                looks_like_target_text(
                    "The contract shall be signed by the parties",
                    source_lang="en",
                    target_lang=target,
                ),
                target,
            )

    def test_translated_target_line_is_still_recognized(self) -> None:
        """反向护栏：真的目标语言译文不能因为放宽而失守。"""
        self.assertFalse(
            looks_like_source_text(
                "Le contrat est signé par les parties",
                source_lang="en",
                target_lang="fr",
            )
        )
        self.assertEqual(
            split_existing_bilingual_text(
                "The contract is signed by the parties\n"
                "Le contrat est signé par les parties",
                source_lang="en",
                target_lang="fr",
            ),
            (
                "The contract is signed by the parties",
                "Le contrat est signé par les parties",
            ),
        )

    def test_unknown_target_language_never_gets_negative_evidence(self) -> None:
        """自定义目标语言：词表里没有它，就只能「没话说」，不能反推「所以不是它」。

        否则中译某自定义语言时，法文译文会因为「更像法语」被判成非译文，整格重翻，
        格里多出一条重复译文。
        """
        self.assertTrue(
            looks_like_target_text(
                "Le contrat est signé par les parties",
                source_lang="zh",
                target_lang="x-custom-darija",
            )
        )
        self.assertEqual(
            split_existing_bilingual_text(
                "合同由双方签署\nLe contrat est signé par les parties",
                source_lang="zh",
                target_lang="x-custom-darija",
            ),
            ("合同由双方签署", "Le contrat est signé par les parties"),
        )

    def test_english_inside_a_chinese_to_french_document_is_not_french(self) -> None:
        """中译法文档里夹的英文段不是法文译文——这条老行为不能被放宽带塌。"""
        self.assertFalse(
            looks_like_target_text(
                "The contract is signed by the parties",
                source_lang="zh",
                target_lang="fr",
            )
        )


class ChineseToJapaneseTests(_WorkbookCase):
    """中-12：已有日文译文的格子不许再补一条。"""

    def test_japanese_translation_is_not_mistaken_for_chinese_source(self) -> None:
        for text in ("工事は2026年8月9日に完了する", "契約書の内容について", "コンクリート打設"):
            self.assertTrue(
                looks_like_target_text(text, source_lang="zh", target_lang="ja"),
                text,
            )
            self.assertFalse(
                looks_like_source_text(text, source_lang="zh", target_lang="ja"),
                text,
            )

    def test_chinese_source_stays_source_under_a_japanese_target(self) -> None:
        for text in ("抢工方案", "混凝土浇筑", "本工程按合同竣工日期执行"):
            self.assertTrue(
                looks_like_source_text(text, source_lang="zh", target_lang="ja"),
                text,
            )
            self.assertFalse(
                looks_like_target_text(text, source_lang="zh", target_lang="ja"),
                text,
            )

    def test_bilingual_cell_with_japanese_translation_counts_as_covered(self) -> None:
        statuses = self._plan(
            {
                "A1": "施工进度计划\n工事は2026年8月9日に完了する",
                "A2": "混凝土浇筑",
            },
            source_lang="zh",
            target_lang="ja",
        )
        self.assertEqual(statuses["A1"], COVERAGE_COVERED)
        self.assertEqual(statuses["A2"], COVERAGE_SOURCE_ONLY)

    def test_standalone_japanese_cell_is_skipped_not_retranslated(self) -> None:
        statuses = self._plan(
            {"A1": "工事は2026年8月9日に完了する"},
            source_lang="zh",
            target_lang="ja",
        )
        self.assertEqual(statuses["A1"], COVERAGE_IGNORED)


class SameAlphabetPairTests(_WorkbookCase):
    """中-13：同字母语言对，双行都是原文时不许判 covered。"""

    def test_two_english_lines_do_not_pair_up_as_english_to_german(self) -> None:
        self.assertIsNone(
            split_existing_bilingual_text(
                "Payment Terms\nNet 30 days after the invoice date",
                source_lang="en",
                target_lang="de",
            )
        )
        statuses = self._plan(
            {"A1": "Payment Terms\nNet 30 days after the invoice date"},
            source_lang="en",
            target_lang="de",
        )
        self.assertEqual(statuses["A1"], COVERAGE_SOURCE_ONLY)

    def test_genuine_german_translation_still_pairs_up(self) -> None:
        self.assertEqual(
            split_existing_bilingual_text(
                "Payment Terms\nZahlungsbedingungen für die Lieferung",
                source_lang="en",
                target_lang="de",
            ),
            ("Payment Terms", "Zahlungsbedingungen für die Lieferung"),
        )
        statuses = self._plan(
            {"A1": "Payment Terms\nZahlungsbedingungen für die Lieferung"},
            source_lang="en",
            target_lang="de",
        )
        self.assertEqual(statuses["A1"], COVERAGE_COVERED)

    def test_german_line_is_not_english_target_text(self) -> None:
        """反向：德译英时德文行不能算成英文译文。"""
        self.assertFalse(
            looks_like_target_text(
                "Die Lieferung wird durch den Auftragnehmer ausgeführt",
                source_lang="de",
                target_lang="en",
            )
        )
        self.assertIsNone(
            split_existing_bilingual_text(
                "Die Lieferung erfolgt frei Baustelle\n"
                "Die Lieferung wird durch den Auftragnehmer ausgeführt",
                source_lang="de",
                target_lang="en",
            )
        )

    def test_cross_script_pair_is_untouched_by_the_same_alphabet_guard(self) -> None:
        """中译法：译文里残留几个中文序号仍是一条译文，配对不能被这条新规则打掉。"""
        self.assertEqual(
            split_existing_bilingual_text(
                "（二）钢筋绑扎\n（二）Ligature des armatures",
                source_lang="zh",
                target_lang="fr",
            ),
            ("（二）钢筋绑扎", "（二）Ligature des armatures"),
        )


class MixedScriptChinesePairTests(_WorkbookCase):
    """整改 MF1：中文原文里带材料牌号（HRB400、C30、M20）时，配对不许被打掉。

    第一轮为 中-13 加的「同文字体系闸门」拿 _script_signature 做集合比较——中文行只要
    出现一个拉丁字符，两半签名就都是 {cjk, latin}，闸门开火；而它据以否定的证据
    `_script_evidence(后半, 'zh')` 其实只说明「后半有汉字」，不说明后半仍是中文原文。
    施工表里材料牌号到处都是，这一刀会把已经翻好的双语格重新打回补译清单。
    """

    def test_material_grade_in_chinese_source_does_not_break_the_pair(self) -> None:
        self.assertEqual(
            split_existing_bilingual_text(
                "（二）钢筋绑扎 HRB400\n（二）Ligature des armatures HRB400",
                source_lang="zh",
                target_lang="fr",
            ),
            ("（二）钢筋绑扎 HRB400", "（二）Ligature des armatures HRB400"),
        )

    def test_material_grade_cell_stays_covered(self) -> None:
        statuses = self._plan(
            {"A1": "（二）钢筋绑扎 HRB400\n（二）Ligature des armatures HRB400"},
            source_lang="zh",
            target_lang="fr",
        )
        self.assertEqual(statuses["A1"], COVERAGE_COVERED)


class SameAlphabetGuardTests(_WorkbookCase):
    """整改 MF1 的另一半：闸门改窄之后仍要真的开火，不能改成一段死代码。

    审查点名第一轮那道闸门「无测试覆盖，整块删掉全集群仍绿」。这里钉的是它唯一的
    用武之地：同字母语言对里证据很弱的那一格——后半只有一个英语专属功能词、一个德语
    专属词都没有。这种 1 比 0 按 MF2 的余量规则只配得出「说不上来」，
    looks_like_target_text 于是放行（有 3 个字母以上的自然语言词），全靠这道闸门
    拦住。删掉闸门，本类第一条就会转红。
    """

    def test_weak_english_evidence_still_blocks_an_english_to_german_pair(self) -> None:
        self.assertIsNone(
            split_existing_bilingual_text(
                "Delivery Schedule\nDelivery has begun",
                source_lang="en",
                target_lang="de",
            )
        )
        statuses = self._plan(
            {"A1": "Delivery Schedule\nDelivery has begun"},
            source_lang="en",
            target_lang="de",
        )
        self.assertEqual(statuses["A1"], COVERAGE_SOURCE_ONLY)

    def test_guard_only_applies_to_same_alphabet_pairs(self) -> None:
        """跨文字体系的语言对不许进这道闸——那种组合看字符就分得开。"""
        self.assertFalse(_is_same_alphabet_pair("zh", "fr"))
        self.assertFalse(_is_same_alphabet_pair("en", "ru"))
        self.assertFalse(_is_same_alphabet_pair("en", "ko"))
        self.assertFalse(_is_same_alphabet_pair("ja", "zh"))
        self.assertFalse(_is_same_alphabet_pair("en", "en"))
        self.assertTrue(_is_same_alphabet_pair("en", "de"))
        self.assertTrue(_is_same_alphabet_pair("en-GB", "nl"))

    def test_guard_does_not_fire_on_chinese_source_with_latin_fragments(self) -> None:
        """MF1 的病灶复现点：中文原文带材料牌号时闸门必须完全不参与判定。"""
        for text in (
            "（二）钢筋绑扎 HRB400\n（二）Ligature des armatures HRB400",
            "混凝土 C30 浇筑\nCoulage du béton C30",
            "砌体 M20 砂浆\nMortier de maçonnerie M20",
        ):
            self.assertIsNotNone(
                split_existing_bilingual_text(text, source_lang="zh", target_lang="fr"),
                text,
            )


class WeakMarkerEvidenceTests(_WorkbookCase):
    """整改 MF2：1 比 0 不足以定案。

    标记词表从 2 门扩到 7 门之后，door / over / met / van / son / el 这些荷/西专属词
    把大量普通英文短标签判成了「确定不是英文」——而这些短标签里往往一个 en 专属功能词
    都没有。后果是 zh→en（本工具最主要的语言对）已经翻好的双语格重新落回补译清单，
    格里多出第二条同样的译文。弱证据只该产生「说不上来」。
    """

    def test_short_english_labels_are_still_english(self) -> None:
        for text in (
            "Fire Door",
            "Door Type",
            "Steel Door",
            "Carried over",
            "Hand over",
            "Site met",
            "Van transport",
            "Son of contractor",
            "El Paso office",
        ):
            self.assertTrue(
                looks_like_target_text(text, source_lang="zh", target_lang="en"),
                text,
            )

    def test_translated_chinese_to_english_cell_is_not_retranslated(self) -> None:
        statuses = self._plan(
            {"A1": "防火门\nFire Door", "A2": "钢制门"},
            source_lang="zh",
            target_lang="en",
        )
        self.assertEqual(statuses["A1"], COVERAGE_COVERED)
        self.assertEqual(statuses["A2"], COVERAGE_SOURCE_ONLY)

    def test_decisive_evidence_still_produces_a_negative(self) -> None:
        """护栏：证据足够时仍要判否，不能被这条余量放塌。"""
        self.assertFalse(
            looks_like_target_text(
                "Net 30 days after the invoice date",
                source_lang="en",
                target_lang="de",
            )
        )
        self.assertFalse(
            looks_like_target_text(
                "Die Lieferung wird durch den Auftragnehmer ausgeführt",
                source_lang="de",
                target_lang="en",
            )
        )


class KanaPunctuationTests(_WorkbookCase):
    """整改 MF3：「・」「ー」是标点，不是假名。

    片假名区间 U+30A0–U+30FF 里混着 ・(U+30FB) 与 ー(U+30FC)，中文排版里都会出现
    （外国人名分隔、从日文资料复制过来的长音号）。把它们算成假名，中译日时纯中文格
    就成了「日文译文」——既非源文也非译文，静默 ignored，正是 高-4 那个病复发。
    """

    def test_katakana_punctuation_is_not_kana(self) -> None:
        for text in ("阿尔法・贝塔工程", "钢筋工程ー第一部分", "甲方・乙方"):
            self.assertFalse(contains_kana(text), text)

    def test_chinese_with_katakana_punctuation_stays_source(self) -> None:
        for text in ("阿尔法・贝塔工程", "钢筋工程ー第一部分"):
            self.assertTrue(
                looks_like_source_text(text, source_lang="zh", target_lang="ja"),
                text,
            )
            self.assertFalse(
                looks_like_target_text(text, source_lang="zh", target_lang="ja"),
                text,
            )
        statuses = self._plan(
            {"A1": "阿尔法・贝塔工程"},
            source_lang="zh",
            target_lang="ja",
        )
        self.assertEqual(statuses["A1"], COVERAGE_SOURCE_ONLY)

    def test_real_kana_is_still_kana(self) -> None:
        for text in ("工事は2026年8月9日に完了する", "コンクリート打設", "ｺﾝｸﾘｰﾄ"):
            self.assertTrue(contains_kana(text), text)


class JapaneseSourceCoverageTests(_WorkbookCase):
    """整改（跨集群，来自 A1-filter 审查高-1）：日译中的日文格要认得出是源文。

    core/translation_filter.should_translate 已经按语言对判定（假名/谚文，或源语言
    落在日/韩时的汉字），但补译判定在源语言不是中文时遇 CJK 就一刀 return False，
    根本走不到那个漏斗——ja→zh 整份文档全判 ignored：不译、也不进报告。
    """

    def test_japanese_cells_are_source_text_under_a_chinese_target(self) -> None:
        for text in ("工事契約書", "コンクリート打設", "工事は2026年8月9日に完了する"):
            self.assertTrue(
                looks_like_source_text(text, source_lang="ja", target_lang="zh"),
                text,
            )

    def test_japanese_workbook_is_scanned_not_ignored(self) -> None:
        statuses = self._plan(
            {"A1": "工事契約書", "A2": "コンクリート打設"},
            source_lang="ja",
            target_lang="zh",
        )
        self.assertEqual(statuses["A1"], COVERAGE_SOURCE_ONLY)
        self.assertEqual(statuses["A2"], COVERAGE_SOURCE_ONLY)

    def test_japanese_language_tag_variants_are_recognized(self) -> None:
        for tag in ("ja", "ja-JP", "JA", " ja "):
            self.assertTrue(
                looks_like_source_text("工事契約書", source_lang=tag, target_lang="zh"),
                tag,
            )

    def test_bilingual_japanese_cell_counts_as_covered(self) -> None:
        statuses = self._plan(
            {"A1": "コンクリート打設\n混凝土浇筑"},
            source_lang="ja",
            target_lang="zh",
        )
        self.assertEqual(statuses["A1"], COVERAGE_COVERED)

    def test_chinese_under_an_english_source_is_still_not_source_text(self) -> None:
        """护栏：英译中里的中文是译文，不是源文——这条老行为不能被放宽带塌。"""
        for text in ("合同由双方签署", "混凝土浇筑"):
            self.assertFalse(
                looks_like_source_text(text, source_lang="en", target_lang="zh"),
                text,
            )


class KoreanPureHanjaCoverageTests(_WorkbookCase):
    """整改（2026-08-30）：ko 纯汉字漏译。

    _script_evidence 对 "ja" 有专属三分支，"ko" 没有，落进通用逻辑：纯汉字、
    无谚文，直接 return False（「确定不是韩文」）。后果是韩译中里「工事契約書」
    「株式会社」这类纯汉字词被 looks_like_target_text 误判成已译中文，
    补译、覆盖率两条线一起漏收。修法是把 ja 的三分支参数化给 ja/ko 共用：
    含本国专属文字（假名/谚文）→ True；无任何 CJK 汉字→ False；纯汉字、
    无本国专属文字，且 rival 落在汉字圈歧义集合 {"", "zh", "ja", "ko"}→ None，
    交下游 should_translate 兜底裁决，否则才是 True。
    """

    def test_pure_hanja_script_evidence_abstains_like_japanese_does(self) -> None:
        # ko 纯汉字，对手是汉字圈内的语言（含空白）：跟 ja 此前对纯汉字的处理
        # 一样弃权，不再像改动前那样直接 False（「确定不是韩文」）。
        for rival in ("", "zh", "ja", "ko"):
            with self.subTest(rival=rival):
                self.assertIsNone(_script_evidence("工事契約書", "ko", rival))

    def test_pure_hanja_script_evidence_confirms_against_non_cjk_rival(self) -> None:
        # 对手压根不是汉字圈的语言（比如 fr）：纯汉字才能确证是 ko。
        self.assertEqual(_script_evidence("工事契約書", "ko", "fr"), True)

    def test_japanese_pure_kanji_now_abstains_against_korean_rival(self) -> None:
        # 有意的行为变化：ja 纯汉字此前只对 {"", "zh", "ja"} 弃权，rival="ko" 时
        # 会误判 True（「早就断定是日文」）。现在 ko 也算进汉字圈歧义集合，
        # ja 遇 rival="ko" 同样从 True 变 None——更保守，交下游裁决，不是回归。
        self.assertIsNone(_script_evidence("工事契約書", "ja", "ko"))

    def test_japanese_pure_kanji_behaviour_otherwise_unchanged(self) -> None:
        # ja 原有分支的其余行为一律不动：假名命中直接 True；无 CJK 直接 False；
        # 纯汉字对非汉字圈对手（如 fr）依旧确证 True。
        self.assertTrue(_script_evidence("おしらせ", "ja", ""))
        self.assertFalse(_script_evidence("Straße", "ja", ""))
        self.assertEqual(_script_evidence("工事契約書", "ja", "fr"), True)

    def test_korean_pure_hanja_is_recognized_as_source_text(self) -> None:
        for text in ("工事契約書", "株式会社"):
            with self.subTest(text=text):
                self.assertTrue(
                    looks_like_source_text(text, source_lang="ko", target_lang="zh"),
                    text,
                )

    def test_korean_pure_hanja_workbook_is_scanned_not_ignored(self) -> None:
        statuses = self._plan(
            {"A1": "工事契約書", "A2": "株式会社"},
            source_lang="ko",
            target_lang="zh",
        )
        self.assertEqual(statuses["A1"], COVERAGE_SOURCE_ONLY)
        self.assertEqual(statuses["A2"], COVERAGE_SOURCE_ONLY)

    def test_pure_hangul_behaviour_is_not_regressed(self) -> None:
        # 纯谚文：走的是 own_script_re 命中即 True 那一步，跟改动前一样，不经过
        # 汉字圈歧义那段新逻辑。
        self.assertTrue(_script_evidence("계약서", "ko", ""))
        self.assertTrue(
            looks_like_source_text("계약서", source_lang="ko", target_lang="zh")
        )

    def test_hangul_mixed_with_hanja_behaviour_is_not_regressed(self) -> None:
        # 谚文夹汉字：只要谚文命中就直接 True，不看汉字，跟改动前一样。
        text = "工事契約書는 계약서입니다"
        self.assertTrue(_script_evidence(text, "ko", ""))
        self.assertTrue(
            looks_like_source_text(text, source_lang="ko", target_lang="zh")
        )

    def test_pure_hanzi_under_korean_source_matches_japanese_precedent(self) -> None:
        """护栏：纯汉字、rival=zh 弃权后落到下游兜底，ko 要跟 ja 此前的既有口径一致。

        "施工合同" 这种纯汉字词，_script_evidence 对 zh 对手弃权（返回 None），
        拿不准时兜底判 True（见模块开头「拿不准时要落到 source_only」）——这不是
        本次改动引入的新宽松，ja 在同样场景下改动前后都是这个结果，这里钉住的是
        ko 跟 ja 对齐，不是「纯汉字总能穿透」。
        """
        self.assertEqual(
            looks_like_source_text("施工合同", source_lang="ko", target_lang="zh"),
            looks_like_source_text("施工合同", source_lang="ja", target_lang="zh"),
        )


class KoreanTargetAbstainGuardTests(_WorkbookCase):
    """互审整改（2026-08-30）：ko 弃权不得从「判源文」漏进「判译文」。

    _script_evidence 给 ko 补的「纯汉字弃权」（None）是为「这是不是韩文源文」
    留的余地；_looks_translated_despite_cjk 末行的 `is not False` 却会把 None
    当成正面证据——中译韩里「拉丁为主＋零星汉字、无谚文」的段落会被判成
    「已翻好的韩文」，造成新的静默漏译（word/excel 覆盖率共用这条判据）。
    修法：ko 在这里直接看谚文，与参数化前「无谚文即决定性 False」口径一致，
    弃权只留在源文判定一侧。
    """

    _LATIN_WITH_HAN = "Payment Schedule for Contract 附件 A and Appendix Notes"

    def test_incidental_cjk_without_hangul_is_not_korean_target_text(self) -> None:
        # 修复前：_language_evidence 对「有汉字、无谚文」的 ko 弃权（None），
        # `is not False` 把弃权放行成 True；修复后一个谚文字符都没有必须是 False。
        self.assertFalse(
            _looks_translated_despite_cjk(
                self._LATIN_WITH_HAN, source_lang="zh", target_lang="ko"
            )
        )
        self.assertFalse(
            looks_like_target_text(
                self._LATIN_WITH_HAN, source_lang="zh", target_lang="ko"
            )
        )

    def test_incidental_cjk_cell_stays_source_only_under_korean_target(self) -> None:
        statuses = self._plan(
            {"A1": self._LATIN_WITH_HAN},
            source_lang="zh",
            target_lang="ko",
        )
        self.assertEqual(statuses["A1"], COVERAGE_SOURCE_ONLY)

    def test_hangul_evidence_still_counts_as_korean_target_text(self) -> None:
        # 正例护栏：真带谚文的译文照常放行，证明上一条不是一刀切 False。
        text = "계약 대금 지급 일정표 Payment Schedule for the Contract 附件 A and Notes"
        self.assertTrue(
            looks_like_target_text(text, source_lang="zh", target_lang="ko")
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
