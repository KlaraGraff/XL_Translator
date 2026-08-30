"""Regression tests for the CODE_AUDIT_2026-08-29 items assigned to task_runner.py.

covers:
  中-2  停止时已付费拿到的译文既不写文件也不入 TM
  中-3  Excel 复核仲裁标记被无条件清空
  中-8  .xls 转换临时文件在中断路径永久残留
  低-text_source_scopes  用预检前数据构建，重建后新词条保守不入 TM

All cases drive the real ``TaskRunner._run()`` with the translation engine,
Excel automation and TM store mocked out — the same harness style used by
``tests/test_phase4_excel_contracts.py`` — so each test exercises the actual
control flow the bug lived in, not a hand-rolled stand-in for it.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.api_config_check import ApiConfigCheckResult
from core.excel_coverage import ExcelCoveragePlan
from core.file_scanner import FileItem
from core.language_preflight import TranslationLanguageResult
from core.model_throughput import EffectiveModelThroughput
from core.translation_coverage import COVERAGE_SOURCE_ONLY, CoverageUnit
from core.task_runner import StoppedMsg, TaskRunner
from settings import AppSettings, EngineSettings


class _PreflightEngine:
    """Minimal chat engine standing in for the language-preflight round-trip."""

    engine_name = "audit-task-runner/mock"

    def __init__(self, result_for_sample: dict[str, str] | None = None) -> None:
        self._result_for_sample = dict(result_for_sample or {})

    def chat(self, system: str, user: str) -> str:
        samples = json.loads(user)["samples"]
        first = str(samples[0]) if samples else ""
        return self._result_for_sample.get(first, '{"source_langs":["ja"]}')


def _settings(*, source_lang: str = "zh", target_lang: str = "en") -> AppSettings:
    return AppSettings(
        engine=EngineSettings(
            mode="cloud",
            cloud_provider="custom_openai",
            cloud_model="audit-test-model",
            cloud_base_url="https://example.invalid/v1",
            concurrency=1,
            batch_size=10,
        ),
        source_lang=source_lang,
        target_lang=target_lang,
    )


def _base_patches(stack: ExitStack, *, root: Path, engine) -> None:
    """Wire up the plumbing every case needs so only translation/TM differ."""
    stack.enter_context(
        patch("core.task_runner.TaskLogger", return_value=MagicMock(task_id="audit-task-runner"))
    )
    stack.enter_context(
        patch("core.task_runner.check_translation_api_config", return_value=ApiConfigCheckResult(ok=True))
    )
    stack.enter_context(patch("core.task_runner.build_engine", return_value=engine))
    stack.enter_context(patch("core.task_runner.get_system_prompt", return_value="system"))
    stack.enter_context(patch("core.task_runner.resolve_effective_model_config", return_value=object()))
    stack.enter_context(
        patch(
            "core.task_runner.get_model_throughput",
            return_value=EffectiveModelThroughput(profile_key="audit", batch_size=10, concurrency=1),
        )
    )
    stack.enter_context(
        patch("core.task_runner.bilingual_writer.build_output_dir", return_value=root / "out")
    )


def _stopped_message(runner: TaskRunner) -> StoppedMsg:
    messages = [m for m in list(runner._queue.queue) if isinstance(m, StoppedMsg)]
    if len(messages) != 1:
        raise AssertionError(f"expected exactly one StoppedMsg, got {len(messages)}")
    return messages[0]


class StopDuringApiTranslationKeepsTmWrite(unittest.TestCase):
    """中-2: the run stops mid phase-2, after the API call was already paid for."""

    def test_translation_paid_for_before_stop_still_lands_in_tm_but_file_is_not_written(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.xlsx"
            source.write_bytes(b"placeholder")
            engine = _PreflightEngine()

            runner = TaskRunner(
                [FileItem(path=source, name="source", size_kb=1.0)],
                _settings(source_lang="zh", target_lang="en"),
                source_root=root,
            )

            insert_calls: list[tuple] = []

            def fake_insert_batch(pairs, lang_pair, max_len, engine_name, **kwargs):
                insert_calls.append((list(pairs), lang_pair))
                return len(pairs)

            def fake_translate_texts(misses, *args, **kwargs):
                # The API call has already happened (and been paid for) by the
                # time the caller sees this return value — a stop requested
                # right now must not be allowed to erase it. Use a genuine
                # (non-residual-Chinese) translation so the TM hygiene pass
                # doesn't reject it for an unrelated reason.
                runner.stop()
                return {text: "Project Name" for text in misses}

            with ExitStack() as stack:
                _base_patches(stack, root=root, engine=engine)
                stack.enter_context(
                    patch.object(TaskRunner, "_collect_texts", side_effect=lambda *a, **k: (["项目名称"], 1))
                )
                stack.enter_context(patch("core.task_runner.tm_manager.lookup_batch", return_value={}))
                stack.enter_context(
                    patch("core.task_runner.tm_manager.insert_batch", side_effect=fake_insert_batch)
                )
                stack.enter_context(
                    patch("core.task_runner.translate_texts", side_effect=fake_translate_texts)
                )
                writer = stack.enter_context(
                    patch("core.task_runner.bilingual_writer.write_bilingual_file")
                )

                runner._run()

            # The paid-for translation must have reached the TM store...
            self.assertEqual(len(insert_calls), 1)
            pairs, lang_pair = insert_calls[0]
            self.assertIn(("项目名称", "Project Name"), pairs)
            self.assertEqual(lang_pair, "zh-en")

            # ...but stopping still cancels file generation (phase 3 never runs) —
            # that is the part of the run stopping is actually allowed to cancel.
            writer.assert_not_called()
            stopped = _stopped_message(runner)
            self.assertIn("source_path", stopped.files[0] if stopped.files else {})


class ExcelReviewMarksSurvivePhaseTwo(unittest.TestCase):
    """中-3: arbitration marks written in phase 1 must reach the phase-3 writer."""

    def test_phase_one_review_marks_are_not_wiped_before_the_file_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.xlsx"
            source.write_bytes(b"placeholder")
            engine = _PreflightEngine()

            plan = ExcelCoveragePlan(
                path=source,
                units=[
                    CoverageUnit(
                        source_text="疑似原文异常",
                        status=COVERAGE_SOURCE_ONLY,
                        location="Sheet1!A1",
                        reason="test fixture",
                    )
                ],
                sheet_count=1,
            )

            def fake_arbitrate(self, coverage_plan, *, review_marks, **kwargs):
                # Simulate what phase 1's real arbitration does: it writes a
                # mark into the *same* dict the runner will keep using.
                review_marks["疑似原文异常"] = "foreign_noise"

            runner = TaskRunner(
                [FileItem(path=source, name="source", size_kb=1.0)],
                _settings(source_lang="zh", target_lang="en"),
                source_root=root,
                untranslated_only=True,
            )

            with ExitStack() as stack:
                _base_patches(stack, root=root, engine=engine)
                stack.enter_context(
                    patch("core.task_runner.build_excel_coverage_plan", return_value=plan)
                )
                stack.enter_context(
                    patch.object(TaskRunner, "_arbitrate_excel_coverage_pairs", fake_arbitrate)
                )
                stack.enter_context(patch("core.task_runner.tm_manager.lookup_batch", return_value={}))
                stack.enter_context(patch("core.task_runner.tm_manager.insert_batch", return_value=0))
                stack.enter_context(
                    patch("core.task_runner.translate_texts", return_value={"疑似原文异常": "Suspicious source"})
                )
                writer = stack.enter_context(
                    patch(
                        "core.task_runner.write_untranslated_excel_file",
                        return_value=root / "out" / "source.xlsx",
                    )
                )

                runner._run()

            self.assertEqual(writer.call_count, 1)
            review_marks = writer.call_args.kwargs["review_marks"]
            self.assertEqual(review_marks.get("疑似原文异常"), "foreign_noise")


class XlsConversionTempFileCleanup(unittest.TestCase):
    """中-8: the converted .xlsx temp file must not survive a stopped run."""

    def test_temp_file_from_xls_conversion_is_removed_when_the_run_stops_before_phase_three(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.xls"
            source.write_bytes(b"placeholder xls")
            temp_dir = root / "xl_translator_temp"
            temp_dir.mkdir()
            converted = temp_dir / "source.xlsx"
            converted.write_bytes(b"placeholder xlsx")
            engine = _PreflightEngine()

            runner = TaskRunner(
                [FileItem(path=source, name="source", size_kb=1.0, format="xls")],
                _settings(source_lang="zh", target_lang="en"),
                source_root=root,
                allow_xls_fallback=True,
            )

            def fake_collect(*_args, **_kwargs):
                # By the time text collection runs the .xls→.xlsx temp file
                # already exists on disk; stop right here, before phase 2/3.
                runner.stop()
                return (["项目名称"], 1)

            with ExitStack() as stack:
                _base_patches(stack, root=root, engine=engine)
                # 兼容转换现在先探 LibreOffice：桩成「没装」，让这个用例继续走它原本
                # 要测的 xlrd 兜底路径，不被 LO 分支截胡。
                stack.enter_context(
                    patch("core.word_converter._find_soffice", return_value=None)
                )
                stack.enter_context(
                    patch("core.xls_converter.convert_with_fallback", return_value=converted)
                )
                stack.enter_context(patch.object(TaskRunner, "_collect_texts", side_effect=fake_collect))
                stack.enter_context(patch("core.task_runner.tm_manager.lookup_batch", return_value={}))
                stack.enter_context(patch("core.task_runner.tm_manager.insert_batch", return_value=0))
                stack.enter_context(patch("core.task_runner.translate_texts", return_value={}))
                stack.enter_context(patch("core.task_runner.bilingual_writer.write_bilingual_file"))

                runner._run()

            _stopped_message(runner)  # confirms the run actually stopped here
            self.assertFalse(converted.exists(), "转换临时件在停止路径上应当被清理，而不是永久残留")

    def test_temp_file_is_removed_for_an_already_failed_file_skipped_before_phase_three(self) -> None:
        # 这个用例要真的走到阶段 3 的 `if already_failed: continue` 分支，
        # 而不是阶段 1 自己的读失败清理点（那个分支早就有清理、不是 中-8
        # 修的缺口）。要做到这一点，得让阶段 1 的取词成功（.xls 转换临时件
        # 落地且不被阶段 1 删掉），失败要推迟到「自动识别源语言后重算补译
        # 计划」那一步——即 auto_source_lang + untranslated_only 时，阶段 1
        # 只是先拿一遍全量候选样本（走 _collect_texts，不走
        # build_excel_coverage_plan），真正的失败点在预检之后的
        # _rebuild_coverage_plans_after_preflight 里调用 build_excel_coverage_plan。
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.xls"
            source.write_bytes(b"placeholder xls")
            temp_dir = root / "xl_translator_temp"
            temp_dir.mkdir()
            converted = temp_dir / "source.xlsx"
            converted.write_bytes(b"placeholder xlsx")
            engine = _PreflightEngine()

            runner = TaskRunner(
                [FileItem(path=source, name="source", size_kb=1.0, format="xls")],
                _settings(source_lang="auto", target_lang="en"),
                source_root=root,
                untranslated_only=True,
                allow_xls_fallback=True,
            )

            with ExitStack() as stack:
                _base_patches(stack, root=root, engine=engine)
                # 兼容转换现在先探 LibreOffice：桩成「没装」，让这个用例继续走它原本
                # 要测的 xlrd 兜底路径，不被 LO 分支截胡。
                stack.enter_context(
                    patch("core.word_converter._find_soffice", return_value=None)
                )
                stack.enter_context(
                    patch("core.xls_converter.convert_with_fallback", return_value=converted)
                )
                # 阶段 1（auto_source_lang 分支）取全量候选样本成功——这一步
                # 不能失败，否则又会掉回阶段 1 自己的清理点，测不到 中-8。
                stack.enter_context(
                    patch.object(TaskRunner, "_collect_texts", side_effect=lambda *a, **k: (["项目名称"], 1))
                )
                # 语言预检之后重算补译计划时才失败——这是 中-8 真正要保护的
                # 那条路径：文件在这里被标 failed，阶段 3 直接 continue 过去。
                stack.enter_context(
                    patch(
                        "core.task_runner.build_excel_coverage_plan",
                        side_effect=ValueError("模拟重算补译计划失败"),
                    )
                )
                stack.enter_context(patch("core.task_runner.tm_manager.lookup_batch", return_value={}))
                stack.enter_context(patch("core.task_runner.tm_manager.insert_batch", return_value=0))
                stack.enter_context(patch("core.task_runner.translate_texts", return_value={}))
                stack.enter_context(patch("core.task_runner.bilingual_writer.write_bilingual_file"))

                runner._run()

            self.assertFalse(
                converted.exists(),
                "already_failed 文件的转换临时件也应当被清理，而不是永久残留",
            )


class TextSourceScopesCoverNewPostPreflightEntries(unittest.TestCase):
    """低-text_source_scopes: rebuild can surface source text the pre-preflight
    scan never saw; TM insertion must not silently refuse it for that reason.
    """

    def test_a_source_text_only_discovered_after_the_coverage_rebuild_still_gets_tm_eligibility(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.xlsx"
            source.write_bytes(b"placeholder")
            # The pre-preflight full-text sample never contains "新词条"（比如它
            # 是重算补译计划、拿到真实源语言之后才被认出的原文）。
            engine = _PreflightEngine({"旧文本": '{"source_langs":["ja"]}'})

            rebuilt_plan = ExcelCoveragePlan(
                path=source,
                units=[
                    CoverageUnit(
                        source_text="新词条",
                        status=COVERAGE_SOURCE_ONLY,
                        location="Sheet1!B2",
                        reason="test fixture: only visible once source_lang is known",
                    )
                ],
                sheet_count=1,
            )

            runner = TaskRunner(
                [FileItem(path=source, name="source", size_kb=1.0)],
                _settings(source_lang="auto", target_lang="zh"),
                source_root=root,
                untranslated_only=True,
            )

            insert_calls: list[list[dict]] = []

            def fake_insert_auto_entries(entries, target_lang, max_len, engine_name, **kwargs):
                insert_calls.append(list(entries))
                return len(entries)

            with ExitStack() as stack:
                _base_patches(stack, root=root, engine=engine)
                stack.enter_context(
                    patch.object(TaskRunner, "_collect_texts", side_effect=lambda *a, **k: (["旧文本"], 1))
                )
                stack.enter_context(
                    patch("core.task_runner.build_excel_coverage_plan", return_value=rebuilt_plan)
                )
                stack.enter_context(
                    patch.object(TaskRunner, "_arbitrate_excel_coverage_pairs", lambda *a, **k: None)
                )
                stack.enter_context(patch("core.task_runner.tm_manager.lookup_batch", return_value={}))
                stack.enter_context(
                    patch(
                        "core.task_runner.tm_manager.insert_auto_entries",
                        side_effect=fake_insert_auto_entries,
                    )
                )
                stack.enter_context(
                    patch(
                        "core.task_runner.translate_texts_with_sources",
                        return_value={
                            "新词条": TranslationLanguageResult(
                                "新词条",
                                "New term",
                                source_lang="ja",
                                target_lang="zh",
                                tm_eligible=True,
                            ),
                        },
                    )
                )
                stack.enter_context(
                    patch(
                        "core.task_runner.write_untranslated_excel_file",
                        return_value=root / "out" / "source.xlsx",
                    )
                )

                runner._run()

            self.assertEqual(len(insert_calls), 1)
            by_source = {entry["source_text"]: entry for entry in insert_calls[0]}
            self.assertIn("新词条", by_source)
            self.assertTrue(
                by_source["新词条"]["tm_eligible"],
                "补译重算后才出现的新词条不该因为不在预检前的候选范围内就被拒绝入库",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
