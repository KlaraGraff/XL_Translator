"""审计批次 2「停止可见性小包」回归测试（CODE_AUDIT_2026-08-29 追加批次，见
docs/AUDIT_FIX_PLAN_2026-08-29.md 第 70-71 行）。

covers:
  ① PDF 逐页状态要把「已停止」和「失败」分开（ui/src/views/workspace.ts）
  ② 停止收尾横幅要报出「还剩多少没跑到」（ui/src/views/workspace.ts）
  ③ 单页重新生成失败要说清「旧产物已保留」（core/pdf_image_translation.py）
  ④ Excel 停止时没来得及自动修复的残留中文，要归因到「停止」，不能算成
    「修复失败」（core/task_runner.py）

①②③ 目前没有可用的前端/该函数专属集成夹具（①②没有 JS 测试运行时；③的第二个
raise 分支——重新装配失败——需要模拟 pikepdf 内部装配失败，成本明显高于「这句
话还在不在源码里」这件事本身值得的投入），走源码钉死（``_read_ui``/``_function_body``
沿用 tests/test_audit_excel_fixes.py 的 B2 集群先例）。③的第一个 raise 分支
（``_fatal_model_error``）有现成的集成夹具，见 tests/test_audit_pdf_fixes.py 的
``AuditPdfHighAndMediumTests``。④驱动真实的 ``TaskRunner._run()``，写法
沿用 tests/test_audit_task_runner_fixes.py。
"""

from __future__ import annotations

import re
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from core.file_scanner import FileItem
from core.residual_pipeline import ResidualPassResult, ResidualUnitReport
from core.residual_repair import RepairLadderResult
from core.task_runner import TaskRunner
from tests.test_audit_excel_fixes import _function_body, _read_ui
from tests.test_audit_task_runner_fixes import _PreflightEngine, _base_patches, _settings, _stopped_message


class PdfPageStopStatusWordingTests(unittest.TestCase):
    """子项①：逐页表格里「已停止」不能跟「失败」「建议复核」共用同一个桶。"""

    def test_review_result_chip_and_note_brand_a_stopped_pending_page_separately(self) -> None:
        source = _read_ui("views/workspace.ts")

        chip = _function_body(source, "reviewResultChip")
        self.assertIn("taskStopped", chip, "reviewResultChip 丢了 taskStopped 信号")
        self.assertIn('"已停止"', chip)
        # 还在跑的时候，待处理页仍然要是「待处理」（mute）——不能因为加了新分支
        # 就把两种情形都合并成一个词。
        self.assertIn('"待处理"', chip)

        note = _function_body(source, "reviewNote")
        self.assertIn("taskStopped", note)
        self.assertIn("不是生成失败", note, "停止未跑到的页不能被读成生成失败")

    def test_group_summary_buckets_stopped_pages_apart_from_failed_and_flagged(self) -> None:
        source = _read_ui("views/workspace.ts")
        summary = _function_body(source, "buildGroupSummaryNodes")
        self.assertIn("taskStopped", summary)
        self.assertIn("stoppedSet", summary, "分组小结没有单独的「已停止」桶")
        # 这里是模板字符串拼出来的（`${stoppedSet.size} 已停止`），不是字面量，
        # 所以不带引号地找这个词。
        self.assertIn("已停止", summary)

    def test_pdf_review_card_only_treats_the_genuine_stopped_state_as_taskStopped(self) -> None:
        # 只认「已停止」这一个终态——这是范围裁剪，不是成因差异：error 终态下同样
        # 会留下 pending 页（_discard_stopped_pages 挂在 finally 上，对所有出口
        # 生效），但那不在审计批次 2 第①条的范围里，另有待办。
        source = _read_ui("views/workspace.ts")
        card = _function_body(source, "buildPdfReviewCard")
        self.assertIn('local.task.state === "stopped"', card)


class StopBannerUnfinishedCountTests(unittest.TestCase):
    """子项②：横幅要报出「还剩多少没跑到」，PDF 按页、Excel/Word 按文件。"""

    def test_finish_task_reads_unstarted_counts_only_when_task_state_is_stopped(self) -> None:
        # 互审抓过一次假钉子：这里原先断言的字面量（字段名、「没跑到」）在旁边的
        # 注释里全都出现过，把实现代码整块删掉、只留注释，四条断言照样通过。所以
        # 断言一律用「注释里不会出现的代码形态」——含反引号模板串的 push 调用、
        # 含 num(record(...)) 的字段读取。
        source = _read_ui("views/workspace.ts")
        body = _function_body(source, "finishTask")
        self.assertIn(
            "const unfinishedPages = num(record(result.kpi).unstarted_page_count)", body
        )
        self.assertIn(
            "const unfinishedFiles = num(record(result.kpi).unstarted_file_count)", body
        )
        self.assertIn("clauses.push(`还有 ${unfinishedPages} 页没跑到", body)
        self.assertIn("clauses.push(`还有 ${unfinishedFiles} 个文件没跑到", body)

    def test_fail_banner_keeps_clauses_when_nothing_was_generated(self) -> None:
        # 互审 F2：停止落在阶段 2 之前时 generated 为 0、tone 必为 fail，旧代码在
        # 这个分支把 clauses 整个扔掉——「还有 N 页没跑到」恰恰在 clauses 里，最
        # 需要报数的那一次反而一个字不显示。钉住修后的无条件拼接形态。
        source = _read_ui("views/workspace.ts")
        body = _function_body(source, "finishTask")
        self.assertIn('subtitle: [detail, ...clauses].join(" · ")', body)
        self.assertNotIn(
            'generated > 0 ? [detail, ...clauses].join(" · ") : detail', body
        )


class PdfRegenerateFailureKeepsOldArtifactWordingTests(unittest.TestCase):
    """子项③：单页重新生成失败，两处 PdfPageActionError 都要说「旧产物已保留」。"""

    def test_execute_page_rerun_error_messages_pin_the_reassurance_clause(self) -> None:
        # 源码钉死：确保两处异常消息——致命模型错误、重新装配失败——都不会在
        # 未来的改动里把这句安抚文案又漏掉（071e130 之前就是「有原因文本时不带
        # 这句话」的老毛病，这里既钉住有文案分支也钉住默认兜底分支）。
        repo = Path(__file__).resolve().parents[1]
        source = (repo / "core" / "pdf_image_translation.py").read_text(encoding="utf-8")
        match = re.search(r"    def _execute_page_rerun\(.*?\n    def ", source, re.S)
        assert match, "在源码里找不到 _execute_page_rerun"
        body = match.group(0)
        self.assertIn(
            "已保留上一版译文页和输出文件，本次改动未生效。",
            body,
            "致命模型错误分支丢了旧产物保留的安抚文案",
        )
        self.assertIn(
            "已保留上一版产物，输出文件未改动。",
            body,
            "重新装配失败分支丢了旧产物保留的安抚文案",
        )


class ExcelStopNotRepairedWordingTests(unittest.TestCase):
    """子项④：停止时没来得及自动修复的残留中文，要单独归因到「停止」。"""

    def test_stop_mid_repair_ladder_marks_the_quality_issue_as_caused_by_stop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.xlsx"
            source.write_bytes(b"placeholder")
            engine = _PreflightEngine()

            unit = ResidualUnitReport(
                source_text="项目名称",
                target_text="Project 名称",
                categories=("mixed",),
                spans=("名称",),
            )
            residual_stub = ResidualPassResult(
                convention="",
                checked_count=1,
                fixes={},
                auto_fixed=[],
                needs_review=[unit],
                released_notes=[],
            )

            runner = TaskRunner(
                [FileItem(path=source, name="source", size_kb=1.0)],
                _settings(source_lang="zh", target_lang="en"),
                source_root=root,
            )

            def fake_run_repair_ladder(units, **kwargs):
                # 修复阶梯正在跑的时候用户按了停止：should_stop 变真，剩余单元原样
                # 退回 remaining，并逐条计入 stopped_count——这是
                # core/residual_repair.py 自己文档写明的行为，不是这条用例编出来的
                # 分支。归因判据就是 stopped_count（互审 F7：事后探停止标志会在
                # 「停止落在最后一个单元请求期间」时把真拒收误标成停止导致）。
                runner.stop()
                return RepairLadderResult(
                    remaining=list(units), stopped_count=len(list(units))
                )

            with ExitStack() as stack:
                _base_patches(stack, root=root, engine=engine)
                stack.enter_context(
                    patch.object(
                        TaskRunner, "_collect_texts", side_effect=lambda *a, **k: (["项目名称"], 1)
                    )
                )
                stack.enter_context(patch("core.task_runner.tm_manager.lookup_batch", return_value={}))
                stack.enter_context(patch("core.task_runner.tm_manager.insert_batch", return_value=0))
                stack.enter_context(
                    patch(
                        "core.task_runner.translate_texts",
                        return_value={"项目名称": "Project 名称"},
                    )
                )
                stack.enter_context(
                    patch("core.task_runner.run_residual_pass", return_value=residual_stub)
                )
                stack.enter_context(
                    patch("core.task_runner.run_repair_ladder", side_effect=fake_run_repair_ladder)
                )
                stack.enter_context(patch("core.task_runner.bilingual_writer.write_bilingual_file"))

                runner._run()

            stopped = _stopped_message(runner)
            issues = [item for item in stopped.issues if item.get("type") == "residual_source_language"]
            self.assertEqual(
                len(issues), 1, "停止路径下 quality_issues 没有传进 StoppedMsg.issues"
            )
            issue = issues[0]
            self.assertTrue(issue["caused_by_stop"])
            self.assertIn("任务停止时还没来得及自动修复", issue["message"])
            self.assertIn("不是修复失败", issue["message"])
            # 互审 F6：停止标志置位后阶段 3 之前就被拦下，输出文件根本没写出来——
            # 这条 issue 不许承诺「已在输出文件中标记」，也不许说「接着处理」
            # （本次没有可续的产物，重跑是从头再来、靠记忆库复用）。
            self.assertNotIn("已在输出文件中标记", issue["message"])
            self.assertNotIn("接着处理", issue["message"])
            self.assertIn("请按下方源文清单逐条核对。", issue["message"])
            # 佐证「文件确实没写出来」：契约里该文件是 unstarted。
            self.assertEqual(stopped.files[0]["status"], "unstarted")

    def test_over_cap_only_remaining_is_not_mislabeled_as_stopped(self) -> None:
        """没有真的停止时，不能因为凑巧读到某个残留状态就误标成停止导致。"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.xlsx"
            source.write_bytes(b"placeholder")
            engine = _PreflightEngine()

            unit = ResidualUnitReport(
                source_text="项目名称",
                target_text="Project 名称",
                categories=("mixed",),
                spans=("名称",),
            )
            residual_stub = ResidualPassResult(
                convention="",
                checked_count=1,
                fixes={},
                auto_fixed=[],
                needs_review=[unit],
                released_notes=[],
            )

            runner = TaskRunner(
                [FileItem(path=source, name="source", size_kb=1.0)],
                _settings(source_lang="zh", target_lang="en"),
                source_root=root,
            )

            def fake_run_repair_ladder(units, **kwargs):
                # 没有调用 runner.stop()：这批 remaining 是真的超限没修，不是停止。
                return RepairLadderResult(remaining=list(units), over_cap_count=1)

            with ExitStack() as stack:
                _base_patches(stack, root=root, engine=engine)
                stack.enter_context(
                    patch.object(
                        TaskRunner, "_collect_texts", side_effect=lambda *a, **k: (["项目名称"], 1)
                    )
                )
                stack.enter_context(patch("core.task_runner.tm_manager.lookup_batch", return_value={}))
                stack.enter_context(patch("core.task_runner.tm_manager.insert_batch", return_value=0))
                stack.enter_context(
                    patch(
                        "core.task_runner.translate_texts",
                        return_value={"项目名称": "Project 名称"},
                    )
                )
                stack.enter_context(
                    patch("core.task_runner.run_residual_pass", return_value=residual_stub)
                )
                stack.enter_context(
                    patch("core.task_runner.run_repair_ladder", side_effect=fake_run_repair_ladder)
                )
                stack.enter_context(patch("core.task_runner.bilingual_writer.write_bilingual_file"))

                runner._run()

            from core.task_runner import DoneMsg

            done = [m for m in list(runner._queue.queue) if isinstance(m, DoneMsg)]
            self.assertEqual(len(done), 1)
            issues = [item for item in done[0].issues if item.get("type") == "residual_source_language"]
            self.assertEqual(len(issues), 1)
            issue = issues[0]
            self.assertFalse(issue["caused_by_stop"])
            self.assertNotIn("任务停止时还没来得及自动修复", issue["message"])
            self.assertIn("超出单次自动修复", issue["message"])


if __name__ == "__main__":
    unittest.main()
