"""审计批次 2 第④条：补译计划的 ignored 格子曾是静默黑洞。

core/translation_coverage.py 的语言证据判定会把某些真该翻的格子判成
COVERAGE_IGNORED（公式格、看起来已经是译文的格、不符合候选规则的格……），但补译
计划日志过去只报 covered / source_only / ambiguous 三类计数，从不提 ignored——用户
翻完对着原表发现漏译，日志里连"哪一格被跳过、为什么"都查不到。

这里测三层：
  1. core.translation_coverage.group_ignored_units —— 共享层的纯分组函数。
  2. core.translation_coverage.format_ignored_coverage_report —— 共享层的排版函数：
     样例上限、量词参数（Excel 用「格」、Word 用「处」）、全量明细单独返回。
  3. core.task_runner.TaskRunner._log_ignored_coverage_units —— Excel 任务日志出口：
     分组摘要进任务日志（INFO），全量明细只进 loguru 调试输出（stderr，本仓库没配
     文件 sink）——任务面板前端不按 level 过滤，发进任务日志的"DEBUG"行照样全量
     刷在用户面前。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from core import task_runner
from core.translation_coverage import (
    COVERAGE_AMBIGUOUS,
    COVERAGE_COVERED,
    COVERAGE_IGNORED,
    COVERAGE_SOURCE_ONLY,
    CoverageUnit,
    coverage_summary,
    format_ignored_coverage_report,
    group_ignored_units,
)

REASON_RULE = "单元格不符合补译候选规则。"
REASON_FORMULA = "公式单元格：公式显示值回填已关闭，不覆盖以保留公式。"


def _unit(status: str, location: str, *, reason: str = "") -> CoverageUnit:
    return CoverageUnit(
        source_text="占位原文",
        status=status,
        location=location,
        reason=reason,
        kind="cell",
    )


class GroupIgnoredUnitsTests(unittest.TestCase):
    """共享层：按 reason 分组，只收 ignored，顺序按首次出现。"""

    def test_only_ignored_units_are_grouped(self) -> None:
        units = [
            _unit(COVERAGE_SOURCE_ONLY, "Sheet1!A1"),
            _unit(COVERAGE_COVERED, "Sheet1!A2"),
            _unit(COVERAGE_AMBIGUOUS, "Sheet1!A3"),
            _unit(COVERAGE_IGNORED, "Sheet1!A4", reason=REASON_RULE),
        ]
        groups = group_ignored_units(units)
        self.assertEqual(groups, [(REASON_RULE, ["Sheet1!A4"])])

    def test_group_order_follows_first_appearance_and_preserves_location_order(
        self,
    ) -> None:
        units = [
            _unit(COVERAGE_IGNORED, "Sheet1!A1", reason=REASON_RULE),
            _unit(COVERAGE_IGNORED, "Sheet1!B1", reason=REASON_FORMULA),
            _unit(COVERAGE_IGNORED, "Sheet1!A2", reason=REASON_RULE),
            _unit(COVERAGE_IGNORED, "Sheet1!B2", reason=REASON_FORMULA),
        ]
        groups = group_ignored_units(units)
        self.assertEqual(
            groups,
            [
                (REASON_RULE, ["Sheet1!A1", "Sheet1!A2"]),
                (REASON_FORMULA, ["Sheet1!B1", "Sheet1!B2"]),
            ],
        )

    def test_no_ignored_units_returns_empty_list(self) -> None:
        units = [
            _unit(COVERAGE_SOURCE_ONLY, "Sheet1!A1"),
            _unit(COVERAGE_COVERED, "Sheet1!A2"),
        ]
        self.assertEqual(group_ignored_units(units), [])


class FormatIgnoredCoverageReportTests(unittest.TestCase):
    """共享层排版：样例上限、量词参数、全量明细单独返回。"""

    def test_sample_cap_and_full_detail_split(self) -> None:
        # 7 个同理由：摘要只点前 5 个、收"等 2 格"；全量明细里 7 个都在。
        units = [
            _unit(COVERAGE_IGNORED, f"Sheet1!A{i}", reason=REASON_RULE)
            for i in range(1, 8)
        ] + [
            _unit(COVERAGE_IGNORED, "Sheet1!B1", reason=REASON_FORMULA),
            _unit(COVERAGE_IGNORED, "Sheet1!B2", reason=REASON_FORMULA),
        ]
        lines, full_detail = format_ignored_coverage_report("进度表.xlsx", units)

        self.assertIn("9 格未补译", lines[0])
        rule_line = next(line for line in lines if REASON_RULE in line)
        self.assertIn("7 格", rule_line)
        for i in range(1, 6):
            self.assertIn(f"Sheet1!A{i}", rule_line)
        self.assertNotIn("Sheet1!A6", rule_line)
        self.assertNotIn("Sheet1!A7", rule_line)
        self.assertIn("等 2 格", rule_line)

        formula_line = next(line for line in lines if REASON_FORMULA in line)
        self.assertIn("2 格", formula_line)
        self.assertIn("Sheet1!B1", formula_line)
        self.assertIn("Sheet1!B2", formula_line)
        self.assertNotIn("等", formula_line.split(REASON_FORMULA)[-1])

        # 全量明细：被样例截掉的第 6、7 个位置只在这里出现。
        self.assertIn("Sheet1!A6", full_detail)
        self.assertIn("Sheet1!A7", full_detail)
        self.assertIn("进度表.xlsx", full_detail)

    def test_unit_noun_parameter_for_word_side(self) -> None:
        """Word 侧同一个格式化器，量词换成「处」——两个调用方只差这一个参数。"""
        units = [
            _unit(COVERAGE_IGNORED, f"段落 {i}", reason="段落不符合补译候选规则。")
            for i in range(1, 8)
        ]
        lines, _ = format_ignored_coverage_report(
            "报告.docx", units, unit_noun="处"
        )
        self.assertIn("7 处未补译", lines[0])
        self.assertIn("等 2 处", lines[1])
        self.assertNotIn("格", "".join(lines))

    def test_zero_ignored_returns_empty(self) -> None:
        units = [_unit(COVERAGE_SOURCE_ONLY, "Sheet1!A1")]
        self.assertEqual(format_ignored_coverage_report("表.xlsx", units), ([], ""))


class LogIgnoredCoverageUnitsTests(unittest.TestCase):
    """Excel 任务日志出口：摘要进任务日志（INFO），全量只进 loguru 调试输出。"""

    def _runner(self, logs: list[tuple[str, str]]) -> task_runner.TaskRunner:
        runner = task_runner.TaskRunner.__new__(task_runner.TaskRunner)
        runner._log = lambda level, message: logs.append((level, message))
        return runner

    def _plan(self, units: list[CoverageUnit]) -> SimpleNamespace:
        return SimpleNamespace(units=units, summary=coverage_summary(units))

    def test_summary_goes_to_task_log_and_full_detail_to_loguru_debug_only(self) -> None:
        logs: list[tuple[str, str]] = []
        units = [
            _unit(COVERAGE_IGNORED, f"Sheet1!A{i}", reason=REASON_RULE)
            for i in range(1, 8)
        ] + [_unit(COVERAGE_SOURCE_ONLY, "Sheet1!C1")]
        plan = self._plan(units)

        with mock.patch.object(task_runner.logger, "debug") as debug_mock:
            self._runner(logs)._log_excel_coverage_plan("进度表.xlsx", plan)

        info_lines = [message for level, message in logs if level == "INFO"]
        self.assertTrue(
            any("7 格未补译" in line for line in info_lines), info_lines
        )
        # 任务日志里不许有 DEBUG 行：任务面板不按 level 过滤，DEBUG 行会照 INFO
        # 一样全量刷在用户面前，起不到"降权重"的作用。
        self.assertFalse(any(level == "DEBUG" for level, _ in logs))
        # 全量明细走 loguru 调试输出：被样例截掉的位置只在这里出现。
        debug_mock.assert_called_once()
        full_detail = debug_mock.call_args.args[0]
        self.assertIn("Sheet1!A6", full_detail)
        self.assertIn("Sheet1!A7", full_detail)

    def test_zero_ignored_units_emits_nothing_for_that_section(self) -> None:
        logs: list[tuple[str, str]] = []
        units = [
            _unit(COVERAGE_SOURCE_ONLY, "Sheet1!A1"),
            _unit(COVERAGE_COVERED, "Sheet1!A2"),
            _unit(COVERAGE_AMBIGUOUS, "Sheet1!A3"),
        ]
        plan = self._plan(units)

        with mock.patch.object(task_runner.logger, "debug") as debug_mock:
            self._runner(logs)._log_excel_coverage_plan("进度表.xlsx", plan)

        # 没有 ignored 格子：不出汇总段、不落文件明细——仓库立场"无关提示不挂"。
        debug_mock.assert_not_called()
        self.assertFalse(any("未补译（默认跳过）" in message for _, message in logs))

    def test_zero_ignored_still_keeps_the_existing_totals_line(self) -> None:
        """回归：加 ignored 汇总不能动到原有"待补/已覆盖/不确定跳过"这一行。"""
        logs: list[tuple[str, str]] = []
        units = [_unit(COVERAGE_SOURCE_ONLY, "Sheet1!A1")]
        plan = self._plan(units)

        self._runner(logs)._log_excel_coverage_plan("进度表.xlsx", plan)

        self.assertTrue(
            any("待补 1" in message and "已覆盖 0" in message for _, message in logs)
        )


class WordLogIgnoredCoverageUnitsTests(unittest.TestCase):
    """Word 任务日志出口：与 Excel 同一立场，量词换「处」。

    互审指出 Word 侧接线原先只有内联代码没有钉子：抽成
    WordTaskRunner._log_ignored_coverage_units 后在这里直测。
    """

    def _runner(self, logs: list[tuple[str, str]]):
        from core import word_task_runner

        runner = word_task_runner.WordTaskRunner.__new__(
            word_task_runner.WordTaskRunner
        )
        runner._log = lambda level, message: logs.append((level, message))
        return runner

    def test_word_summary_uses_chu_noun_and_detail_goes_to_loguru_debug(self) -> None:
        from core import word_task_runner

        logs: list[tuple[str, str]] = []
        units = [
            _unit(COVERAGE_IGNORED, f"正文第 {i} 段", reason=REASON_RULE)
            for i in range(1, 8)
        ]

        with mock.patch.object(word_task_runner.logger, "debug") as debug_mock:
            self._runner(logs)._log_ignored_coverage_units("合同.docx", units)

        info_lines = [message for level, message in logs if level == "INFO"]
        self.assertTrue(
            any("7 处未补译" in line for line in info_lines), info_lines
        )
        self.assertTrue(any("等 2 处" in line for line in info_lines), info_lines)
        self.assertFalse(any(level == "DEBUG" for level, _ in logs))
        debug_mock.assert_called_once()
        full_detail = debug_mock.call_args.args[0]
        self.assertIn("正文第 6 段", full_detail)
        self.assertIn("正文第 7 段", full_detail)

    def test_word_zero_ignored_emits_nothing(self) -> None:
        from core import word_task_runner

        logs: list[tuple[str, str]] = []
        units = [_unit(COVERAGE_SOURCE_ONLY, "正文第 1 段")]

        with mock.patch.object(word_task_runner.logger, "debug") as debug_mock:
            self._runner(logs)._log_ignored_coverage_units("合同.docx", units)

        debug_mock.assert_not_called()
        self.assertEqual(logs, [])



if __name__ == "__main__":
    unittest.main(verbosity=2)
