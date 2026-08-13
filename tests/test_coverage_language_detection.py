"""覆盖率判定里的语言识别回归。

这三条都来自一次真实任务：一份纯中文施工方案翻成法文，输出双语 docx 之后，
质量报告报了 38 条「需人工复核」，其中 23 条是误报。误报的机制逐条实测坐实：

1. 1330 字的法文译文段里夹了 15 个中文字（全是日期），整段被判成中文原文，
   跟它配对的原文段一起被报成「输出文档仍存在未译源文」——两条误报，实际上
   是一段翻完整的译文。
2. 单元格切分假设「第一行是原文、其余都是译文」，原文本身有两行的格
   （变配电室／专项）就把第二行原文算进译文侧，报成「译文中残留少量中文」。
   13 个格，无一例外。
3. looks_like_target_text("N°") 恒为 False（没有字母、太短），8 个
   「序号 / N°」双语表头格被报成「未译源文」。
"""

from __future__ import annotations

import unittest

from core.translation_coverage import (
    looks_like_source_text,
    looks_like_target_text,
    split_existing_bilingual_text,
)

# 真实输出文档 body.paragraph[153] 的开头，中文日期原样留在法文句子里。
FRENCH_WITH_CHINESE_DATES = (
    "Notre société a respectivement adressé, le 2025年12月8日, la lettre "
    "BTR-ANODE-CCTEB-032 intitulée « Questions relatives à l’impact des essais "
    "des fondations sur pieux réalisés par le maître d’ouvrage et des potelets "
    "courts encastrés fournis par le maître d’ouvrage sur la durée totale des "
    "travaux », puis le 2026年1月5日 la lettre BTR-ANODE-CCTEB-057, et enfin le "
    "2026年4月2日 la lettre BTR-ANODE-CCTEB-108 concernant le rejet et le "
    "recours relatifs à la réclamation sur les potelets courts."
)


def _split(text: str):
    return split_existing_bilingual_text(text, source_lang="zh", target_lang="fr")


def _is_target(text: str) -> bool:
    return looks_like_target_text(text, source_lang="zh", target_lang="fr")


def _is_source(text: str) -> bool:
    return looks_like_source_text(text, source_lang="zh", target_lang="fr")


class IncidentalChineseTests(unittest.TestCase):
    def test_long_translation_carrying_a_few_chinese_dates_stays_target_text(self) -> None:
        self.assertTrue(_is_target(FRENCH_WITH_CHINESE_DATES))
        self.assertFalse(_is_source(FRENCH_WITH_CHINESE_DATES))

    def test_short_chinese_heading_is_still_source_text(self) -> None:
        """按比例判定不能把纯中文短标题放过去——4 个字 100% 中文。"""
        for heading in ("抢工方案", "施工范围说明", "二次结构"):
            self.assertTrue(_is_source(heading), heading)
            self.assertFalse(_is_target(heading), heading)

    def test_mostly_chinese_paragraph_is_not_treated_as_translation(self) -> None:
        mixed = "本工程按合同竣工日期 2026年8月9日 执行，受甲供材料延误影响需顺延 169 天。"
        self.assertTrue(_is_source(mixed))
        self.assertFalse(_is_target(mixed))


class ShortTargetTokenTests(unittest.TestCase):
    def test_short_reference_and_unit_tokens_count_as_translation(self) -> None:
        for token in ("N°", "No.", "Réf.", "m²", "kg", "%", "III", "2025"):
            self.assertTrue(_is_target(token), token)

    def test_chinese_short_token_is_not_a_translation(self) -> None:
        for token in ("序号", "编号", "工程"):
            self.assertFalse(_is_target(token), token)


class BilingualSplitTests(unittest.TestCase):
    def test_serial_number_header_cell_pairs_up(self) -> None:
        self.assertEqual(_split("序号\nN°"), ("序号", "N°"))

    def test_multi_line_source_keeps_every_source_line(self) -> None:
        text = "变配电室\n专项\nLocal de transformation et de distribution électrique spécial"
        self.assertEqual(
            _split(text),
            ("变配电室\n专项", "Local de transformation et de distribution électrique spécial"),
        )

    def test_multi_line_source_and_multi_line_target_split_at_the_boundary(self) -> None:
        """从末尾往前扫，不能贪心到把译文行吞进原文侧。"""
        self.assertEqual(
            _split("污染\n破坏\nContamination\nDétérioration"),
            ("污染\n破坏", "Contamination\nDétérioration"),
        )
        self.assertEqual(
            _split("施工范围\n本工程范围包括\nPérimètre des travaux\nLe périmètre comprend"),
            ("施工范围\n本工程范围包括", "Périmètre des travaux\nLe périmètre comprend"),
        )

    def test_single_line_pair_still_splits(self) -> None:
        self.assertEqual(
            _split("抢工方案\nPlan d'accélération des travaux"),
            ("抢工方案", "Plan d'accélération des travaux"),
        )

    def test_chinese_only_cell_has_no_split(self) -> None:
        self.assertIsNone(_split("全部中文\n没有译文"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
