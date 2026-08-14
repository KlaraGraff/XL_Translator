"""输出文档里"上底色"的取舍——Word 段落和 Excel 单元格同一条规矩。

规则只有一条：底色留给需要人动手的东西——译文没出来（保留了原文、译文里残留中文），
或者原文本身可疑。程序自己判过并放行的（严格重试恢复、语义仲裁认定等义）不上底色，
只在质量报告里留一条记录。

为什么要专门锁住：满篇底色等于没有底色。一份文档里 18 条提示有 13 条是"已自动处理"，
用户挨个点开发现都没事，下一次就整片跳过——真正要看的那几条跟着一起被跳过了。

Word 侧的判定散在 core/word_task_runner.py 的几条路上，下面逐条覆盖。

Excel 侧有四处写入标记，其中三处是写死的 MIXED_MARK_UNRESOLVED（core/task_runner.py
两处"译文与原文相同"的兜底、core/xlsx_patcher.py 的"保留了原文"），本来就在政策
之内；唯一会按内容变化的是 MixedLanguageResult.mark_kind，所以下面只锁这一个。
换句话说这里守的是"哪些情况值得上色"，不是"Excel 一共有几处上色代码"——真要在
xlsx_patcher 里新加一处硬编码的标记，这些用例拦不住。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from core.mixed_language import (
    MIXED_ACTION_EXISTING_BILINGUAL,
    MIXED_ACTION_FOREIGN_NOISE,
    MIXED_ACTION_TRANSLATE,
    MIXED_ACTION_UNCERTAIN,
    MIXED_MARK_FOREIGN_NOISE,
    MIXED_MARK_UNRESOLVED,
    MixedLanguageResult,
)
from core.word_task_runner import (
    _append_residual_cjk_issues,
    _apply_mixed_language_word_results,
)

_RUNNER_SOURCE = Path(__file__).resolve().parent.parent / "core" / "word_task_runner.py"


def _apply(results: dict[str, MixedLanguageResult]):
    translations: dict[str, str] = {}
    issues: list[dict] = []
    marks: dict[str, str] = {}
    _apply_mixed_language_word_results(
        mixed_results=results,
        translations=translations,
        quality_issues=issues,
        segment_locations={},
        review_marks=marks,
    )
    return translations, issues, marks


def _issue_for(issues: list[dict], problem: str) -> dict | None:
    for issue in issues:
        if issue.get("problem") == problem:
            return issue
    return None


class MixedLanguageMarkingTests(unittest.TestCase):
    def test_semantically_accepted_translation_is_reported_but_not_highlighted(self) -> None:
        """仲裁说它跟原文等义，那它就是好译文，用户没有任何事要做。"""
        source = "本项目按合同竣工日期2026年8月9日组织施工。"
        translations, issues, marks = _apply(
            {
                source: MixedLanguageResult(
                    source=source,
                    action=MIXED_ACTION_TRANSLATE,
                    translation="Le projet est exécuté selon la date contractuelle.",
                    accepted_by="semantic",
                )
            }
        )

        self.assertEqual(marks, {})
        self.assertEqual(
            translations[source], "Le projet est exécuté selon la date contractuelle."
        )
        reported = _issue_for(issues, "混合语言译文经语义校验接受")
        self.assertIsNotNone(reported)
        self.assertEqual(reported["severity"], "resolved")

    def test_suspect_source_is_highlighted_and_needs_a_human(self) -> None:
        """原文里混着疑似写错的外文——程序照常翻，但对不对只有拿原件的人能定。"""
        source = "3#一体化车间 CCTEB 短柱移交 sitr 完成。"
        _, issues, marks = _apply(
            {
                source: MixedLanguageResult(
                    source=source,
                    action=MIXED_ACTION_FOREIGN_NOISE,
                    translation="Transfert des poteaux courts terminé.",
                )
            }
        )

        self.assertEqual(marks, {source: MIXED_MARK_FOREIGN_NOISE})
        reported = _issue_for(issues, "原文疑似夹杂错误外文")
        self.assertIsNotNone(reported)
        self.assertEqual(reported["severity"], "needs_review")

    def test_undecided_content_keeps_the_source_and_stays_highlighted(self) -> None:
        source = "现场动火作业 hot work 管控要求。"
        translations, issues, marks = _apply(
            {source: MixedLanguageResult(source=source, action=MIXED_ACTION_UNCERTAIN)}
        )

        self.assertEqual(marks, {source: MIXED_MARK_UNRESOLVED})
        self.assertEqual(translations[source], source)
        reported = _issue_for(issues, "混合语言内容未能确认")
        self.assertEqual(reported["severity"], "needs_review")

    def test_already_bilingual_source_is_left_completely_alone(self) -> None:
        source = "工程概况 Présentation du projet"
        translations, issues, marks = _apply(
            {
                source: MixedLanguageResult(
                    source=source, action=MIXED_ACTION_EXISTING_BILINGUAL
                )
            }
        )

        self.assertEqual(marks, {})
        self.assertEqual(translations, {})
        self.assertEqual(
            _issue_for(issues, "原文疑似已包含目标语言译文")["severity"], "resolved"
        )


class ExcelMarkKindTests(unittest.TestCase):
    """Excel 单元格上不上底色，全看 MixedLanguageResult.mark_kind 返回什么。"""

    def test_semantically_accepted_cell_gets_no_fill(self) -> None:
        """仲裁判定与原文等义，用户没有任何事要做——报告里有记录就够了。"""
        result = MixedLanguageResult(
            source="本工程按合同工期组织施工。",
            action=MIXED_ACTION_TRANSLATE,
            translation="Les travaux suivent le délai contractuel.",
            accepted_by="semantic",
        )

        self.assertIsNone(result.mark_kind)

    def test_plain_translation_gets_no_fill(self) -> None:
        result = MixedLanguageResult(
            source="材料进场计划",
            action=MIXED_ACTION_TRANSLATE,
            translation="Plan d'approvisionnement",
        )

        self.assertIsNone(result.mark_kind)

    def test_already_bilingual_cell_gets_no_fill(self) -> None:
        result = MixedLanguageResult(
            source="工程概况 Présentation du projet",
            action=MIXED_ACTION_EXISTING_BILINGUAL,
        )

        self.assertIsNone(result.mark_kind)

    def test_the_two_cases_a_person_still_has_to_look_at_do_get_filled(self) -> None:
        suspect = MixedLanguageResult(
            source="3#一体化车间 sitr 完成。",
            action=MIXED_ACTION_FOREIGN_NOISE,
            translation="Atelier intégré n°3 terminé.",
        )
        undecided = MixedLanguageResult(
            source="现场动火作业 hot work 管控要求。",
            action=MIXED_ACTION_UNCERTAIN,
        )

        self.assertEqual(suspect.mark_kind, MIXED_MARK_FOREIGN_NOISE)
        self.assertEqual(undecided.mark_kind, MIXED_MARK_UNRESOLVED)


class OnlyTwoMarksExistTests(unittest.TestCase):
    """扫源码，锁住"哪些标记允许被写入"这条规则本身。

    上面几条用例覆盖得到混合语言那条路，`_run` 深处的重试/仲裁两处由
    tests/test_phase5_word_contracts.py 的整跑用例断言。这里再加一道结构上的保险：
    真正危险的不是"有人把删掉的那两行原样贴回来"，而是有人换个写法绕过去——
    传关键字 `mark=`、传一个当场算出来的值（`result.mark_kind` 这类，Excel 那条路
    就是这么写的）、或者写成属性引用。所以这里用白名单：交给 _set_review_mark 的
    标记必须是明面上的那两个常量之一，凡是看不出取值的写法一律算违规——要放宽就得
    先改这条政策，而不是绕过它。
    """

    _ALLOWED_MARKS = {"MIXED_MARK_UNRESOLVED", "MIXED_MARK_FOREIGN_NOISE"}

    def _mark_arguments(self) -> list[tuple[int, ast.expr]]:
        tree = ast.parse(_RUNNER_SOURCE.read_text(encoding="utf-8"))
        found: list[tuple[int, ast.expr]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Name) or node.func.id != "_set_review_mark":
                continue
            argument = node.args[2] if len(node.args) > 2 else None
            for keyword in node.keywords:
                if keyword.arg == "mark":
                    argument = keyword.value
            found.append((node.lineno, argument))
        return found

    def test_the_word_runner_only_ever_paints_the_two_allowed_marks(self) -> None:
        offenders = [
            lineno
            for lineno, argument in self._mark_arguments()
            if not (
                isinstance(argument, ast.Name) and argument.id in self._ALLOWED_MARKS
            )
        ]
        self.assertEqual(
            offenders,
            [],
            (
                f"word_task_runner.py 第 {offenders} 行给输出文档上了底色，但用的标记"
                f"不是 {sorted(self._ALLOWED_MARKS)} 里的任何一个。"
                "底色只留给「译文没出来」和「原文本身可疑」两类。"
            ),
        )

    def test_the_guard_would_notice_a_semantic_mark_sneaking_back(self) -> None:
        """守卫自己也要能被证伪，否则它绿着也说明不了什么。"""
        for snippet in (
            "_set_review_mark(marks, source, MIXED_MARK_SEMANTIC)",
            "_set_review_mark(marks, source, mark=MIXED_MARK_SEMANTIC)",
            "_set_review_mark(marks, source, result.mark_kind)",
            "_set_review_mark(marks, source, mixed_language.MIXED_MARK_SEMANTIC)",
        ):
            with self.subTest(snippet=snippet):
                call = ast.parse(snippet).body[0].value
                argument = call.args[2] if len(call.args) > 2 else None
                for keyword in call.keywords:
                    if keyword.arg == "mark":
                        argument = keyword.value
                allowed = (
                    isinstance(argument, ast.Name)
                    and argument.id in self._ALLOWED_MARKS
                )
                self.assertFalse(allowed)


class ResidualReportWordingTests(unittest.TestCase):
    def test_the_report_says_what_was_left_over_without_guessing_why(self) -> None:
        """早先固定跟一句"多为日期或编号"——可残留的常是章节序号，那句话是替用户瞎猜。"""

        class _Unit:
            kind = "paragraph"
            location = "body.paragraph[23]"
            section_path = "正文"
            source_text = "（一）、交货严重延误"
            target_text = "（一）、Retard important de livraison"
            data = {
                "residual_cjk": ["一"],
                "residual_location": "body.paragraph[23]",
                "residual_text": "（一）、Retard important de livraison",
            }

        issues: list[dict] = []
        _append_residual_cjk_issues(
            issues=issues,
            existing_keys=set(),
            file_name="抢工方案.docx",
            residual_units=[_Unit()],
        )

        self.assertEqual(len(issues), 1)
        status = issues[0]["status"]
        self.assertIn("仅残留：一", status)
        self.assertNotIn("日期", status)
        self.assertNotIn("编号", status)
        self.assertEqual(issues[0]["severity"], "needs_review")

    def test_pre_reported_sources_are_not_counted_twice(self) -> None:
        """写盘前残留巡检报过的段落，写盘后通道必须跳过。

        两个通道的坐标与措辞都不同，位置合并键对不上：不跳过的话同一处残留会
        数成两条待办，复核计数直接翻倍。
        """

        class _Unit:
            kind = "paragraph"
            location = "body.paragraph[7]"
            section_path = "正文"
            source_text = "沿裂缝开V型槽并清理浮灰。"
            target_text = "Ouvrir une rainure en V 型槽 le long de la fissure."
            data = {
                "residual_cjk": ["型槽"],
                "residual_location": "body.paragraph[7]",
                "residual_text": "Ouvrir une rainure en V 型槽 le long de la fissure.",
            }

        # 不给跳过名单：正常报一条
        issues: list[dict] = []
        _append_residual_cjk_issues(
            issues=issues,
            existing_keys=set(),
            file_name="抢工方案.docx",
            residual_units=[_Unit()],
        )
        self.assertEqual(len(issues), 1)

        # 给了跳过名单（写盘前通道已按原文坐标报过）：一条都不再报
        issues = []
        _append_residual_cjk_issues(
            issues=issues,
            existing_keys=set(),
            file_name="抢工方案.docx",
            residual_units=[_Unit()],
            pre_reported_residual_sources={"沿裂缝开V型槽并清理浮灰。"},
        )
        self.assertEqual(issues, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
