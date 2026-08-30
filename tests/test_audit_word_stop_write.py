"""审计：Word 任务停止时，已拿到的译文要落盘，不再整体丢弃。

背景与 core/word_task_runner.py 里新增的 `_note_stop_without_discarding` 呼应：
Excel（core/task_runner.py，见提交 fa34578）早就是「停止不丢结果」的立场，Word
以前恢复池收尾后直接 `_raise_if_stopped(...)` 把 `api_translations`（含恢复池
已接受的部分）整批扔掉——用户已经付费拿到的译文一个字都不落盘。这里补的是
用户已经拍板要补的那半步：飞行中/已完成的批次照常写文档、照常入记忆库，真正
被停止拦下的只是「还没来得及发出去的请求」；那部分段落在产物里保留原文，等
下次续译。

三个用例对应任务里定的边界：
  * test_stop_after_translation_call_still_writes_translated_and_original_segments
    —— 「停止后产物落盘且含已译段落」
  * test_stop_before_any_translation_writes_nothing
    —— 「零译文时不写产物」（边界 3a：更早阶段一段都没拿到时行为不变）
  * test_stop_message_reports_progress_honestly_not_as_failure
    —— 「停止报告口径」：账记在停止头上，不冒充失败

不改 core/word_document.py（另有代理在改「正文域段落原地替换」）：这里只调用
它导出的 write_bilingual_docx，且测试段落都不含域，不触碰那条改动路径。
"""

from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from docx import Document

from core.api_config_check import ApiConfigCheckResult
from core.model_throughput import EffectiveModelThroughput
from core.task_runner import StoppedMsg
from core.word_document import WordFileItem, WordSegment
from core.word_document import write_bilingual_docx as real_write_bilingual_docx
from core.word_task_runner import WordTaskRunner
from settings import AppSettings
from tests.app_data_isolation import IsolatedAppDataTestCase


def _prepared(path: Path) -> SimpleNamespace:
    """一份免转换、免编号预处理的 `_prepare_word_source_for_translation` 结果。

    字段集合照抄 tests/test_phase5_word_contracts.py 里同名用途的构造——那边已经
    是这类全流程 _run() 测试的既有约定，这里不用另立一套。
    """

    return SimpleNamespace(
        path=path,
        method="编号预处理：Python 兜底",
        temp_paths=(),
        fallback_messages=(),
        labels_seen=0,
        labels_prepended=0,
        conversion_method="not_required",
        conversion_fidelity="not_required",
        numbering_method="python_conservative",
        numbering_fallback_messages=(),
    )


def _stub_recovery_pool(unresolved_sources: list[str] | None = None):
    """一个什么都不接受、什么都不重试的恢复池替身。

    真恢复池（_WordRecoveryPool）挂死路径已经由 de71ab7 系列测试
    （tests/test_audit_word_hang_fix.py）单独盯着；这里只关心「恢复池收尾之后
    word_task_runner 怎么处置停止信号」，用替身把恢复池本身的行为锁定，噪声
    降到最低。默认零产出；传 unresolved_sources 模拟真池在停止时的实际口径——
    cancel_futures 把所有没被接受的候选（不管跑没跑过）一律塞进
    unresolved_sources（见 _WordRecoveryPool._build_outcome）。
    """

    class _RecoveryPool:
        def add_candidate(self, *_args, **_kwargs) -> None:
            return None

        def start(self) -> None:
            return None

        def wait_for_completion(self):
            return SimpleNamespace(
                fixed_sources=[],
                unresolved_sources=list(unresolved_sources or []),
                accepted_translations={},
                recovery_review_results={},
                semantic_review_results={},
                unresolved_validation_results={},
                semantic_check_count=0,
            )

    return _RecoveryPool()


class WordStopWritesPartialTranslationTests(IsolatedAppDataTestCase):
    @staticmethod
    def _settings() -> AppSettings:
        return AppSettings(source_lang="zh", target_lang="en")

    @staticmethod
    def _terminal_message(runner: WordTaskRunner, message_type):
        matches = [
            message
            for message in list(runner._queue.queue)
            if isinstance(message, message_type)
        ]
        if len(matches) != 1:
            raise AssertionError(
                f"expected exactly one {message_type.__name__}, got {len(matches)}"
            )
        return matches[0]

    def _runner_patches(
        self,
        stack: ExitStack,
        *,
        root: Path,
        prepared_by_path: dict[Path, object],
        real_write: bool = False,
    ) -> MagicMock:
        """与 test_phase5_word_contracts.py 的同名方法同一套脚手架。

        唯一的差别是 `real_write`：默认仍然把 write_bilingual_docx 换成一个只记
        调用参数、不真正碰磁盘的桩（跟既有全流程测试一致）；真正要检查落盘内容
        的用例传 real_write=True，让它经桩转发给 core.word_document 的真实实现——
        这样断言的是「word_task_runner 有没有带着正确的 translations 走到写盘
        这一步」，不是重新验证 write_bilingual_docx 自己的段落替换逻辑（那是
        core/word_document.py 的测试范围，且该文件另有代理在改）。
        """

        writer = stack.enter_context(
            patch(
                "core.word_task_runner.write_bilingual_docx",
                side_effect=(
                    real_write_bilingual_docx
                    if real_write
                    else (lambda **kwargs: root / "out" / kwargs["output_name"])
                ),
            )
        )
        stack.enter_context(
            patch("core.word_task_runner.TaskLogger", return_value=MagicMock(task_id="stop-write-audit"))
        )
        stack.enter_context(
            patch(
                "core.word_task_runner.check_translation_api_config",
                return_value=ApiConfigCheckResult(ok=True),
            )
        )
        stack.enter_context(
            patch("core.word_task_runner.build_engine", return_value=SimpleNamespace(engine_name="stop-audit/mock"))
        )
        stack.enter_context(patch("core.word_task_runner.get_system_prompt", return_value="system"))
        stack.enter_context(patch("core.word_task_runner.resolve_effective_model_config", return_value=object()))
        stack.enter_context(
            patch(
                "core.word_task_runner.get_model_throughput",
                return_value=EffectiveModelThroughput(
                    profile_key="stop-audit", batch_size=10, concurrency=1
                ),
            )
        )
        stack.enter_context(
            patch("core.word_task_runner.build_word_output_dir", return_value=root / "out")
        )
        stack.enter_context(
            patch("core.word_task_runner._append_post_write_coverage_issues", return_value=0)
        )
        stack.enter_context(
            patch(
                "core.word_task_runner._write_word_quality_report",
                return_value=root / "out" / "word_translation_report.md",
            )
        )
        stack.enter_context(patch("core.word_task_runner.tm_manager.insert_batch", return_value=0))

        def prepare(path: Path, **_kwargs):
            prepared = prepared_by_path[path]
            if isinstance(prepared, BaseException):
                raise prepared
            return prepared

        stack.enter_context(
            patch(
                "core.word_task_runner._prepare_word_source_for_translation",
                side_effect=prepare,
            )
        )
        return writer

    def test_stop_after_translation_call_still_writes_translated_and_original_segments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.docx"
            document = Document()
            document.add_paragraph("已完成翻译的段落。")
            document.add_paragraph("翻译中途被打断的段落。")
            document.save(source)
            translated_text = "This paragraph finished translating before the stop landed."

            runner = WordTaskRunner(
                [WordFileItem(path=source, name=source.name, size_kb=1.0)],
                self._settings(),
                source_root=root,
            )

            def stop_mid_batch(texts, *_args, **kwargs):
                # 用户点停止的时机落在「这一批翻译请求已经发出去」之后：按
                # fa34578/de71ab7 的立场，飞行中的这批结果照常拿回来——所以这里
                # 先 stop() 再照常返回译文，而不是让整批作废。返回值里只给「已完
                # 成翻译的段落。」一条，模拟「翻译中途被打断的段落。」那批还没来
                # 得及发出去，这才是停止真正拦下的部分。
                runner.stop()
                kwargs["drained_callback"]()
                return {"已完成翻译的段落。": translated_text}

            with ExitStack() as stack:
                self._runner_patches(
                    stack,
                    root=root,
                    prepared_by_path={source: _prepared(source)},
                    real_write=True,
                )
                stack.enter_context(
                    patch(
                        "core.word_task_runner.extract_word_segments",
                        return_value=[
                            WordSegment("已完成翻译的段落。", "paragraph", "正文第 1 段"),
                            WordSegment("翻译中途被打断的段落。", "paragraph", "正文第 2 段"),
                        ],
                    )
                )
                stack.enter_context(
                    patch(
                        "core.word_task_runner.tm_manager.lookup_batch",
                        return_value={
                            "已完成翻译的段落。": None,
                            "翻译中途被打断的段落。": None,
                        },
                    )
                )
                stack.enter_context(
                    patch(
                        "core.word_task_runner.translate_word_texts",
                        side_effect=stop_mid_batch,
                    )
                )
                stack.enter_context(
                    patch(
                        "core.word_task_runner._WordRecoveryPool",
                        return_value=_stub_recovery_pool(),
                    )
                )
                runner._run()

            # 停止之后应当收到 StoppedMsg 而不是 DoneMsg——任务确实提前收尾了，
            # 但产物要照常在里面。
            stopped = self._terminal_message(runner, StoppedMsg)
            self.assertEqual([item["status"] for item in stopped.files], ["succeeded"])

            output_path = Path(stopped.files[0]["output"])
            self.assertTrue(output_path.exists(), "停止后应当仍然写出双语文档")
            written = Document(output_path)
            paragraphs = [paragraph.text for paragraph in written.paragraphs]
            self.assertIn(translated_text, paragraphs)
            # 没翻到的段落保持原文，而不是被吞掉或留空——这是
            # core/word_document.py 里 `_resolve_translation` 对「找不到译文」
            # 的既有兜底：global_translations 缺这一条，写盘时自动落回原文。
            self.assertIn("翻译中途被打断的段落。", paragraphs)
            self.assertIn("已译 1/2 段", stopped.message)
            self.assertIn("已写入 1 个文件", stopped.message)

    def test_stop_before_any_translation_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.docx"
            document = Document()
            document.add_paragraph("完全没来得及翻译的段落。")
            document.save(source)

            runner = WordTaskRunner(
                [WordFileItem(path=source, name=source.name, size_kb=1.0)],
                self._settings(),
                source_root=root,
            )

            def stop_during_tm_lookup(texts, _pair):
                # 停止落在阶段 2 刚进门、翻译请求一个都还没发出去的时候——这是
                # 「一段译文都没拿到」的真实场景（边界要求 3a），要跟
                # tests/test_phase5_word_contracts.py 里
                # test_stop_before_word_scanning_records_each_file_as_unstarted
                # （那个测的是「_run() 都还没开始」这个更早的路径）区分开：这里
                # 阶段 1 抽取正常跑完，global_unique_texts 不是空的，落到的是我
                # 新加的阶段 3 入口闸——global_translations 算出来是空字典，照旧
                # 整批放弃、不写空产物。
                runner.stop()
                return {text: None for text in texts}

            with ExitStack() as stack:
                writer = self._runner_patches(
                    stack,
                    root=root,
                    prepared_by_path={source: _prepared(source)},
                )
                stack.enter_context(
                    patch(
                        "core.word_task_runner.extract_word_segments",
                        return_value=[
                            WordSegment("完全没来得及翻译的段落。", "paragraph", "正文第 1 段")
                        ],
                    )
                )
                stack.enter_context(
                    patch(
                        "core.word_task_runner.tm_manager.lookup_batch",
                        side_effect=stop_during_tm_lookup,
                    )
                )
                runner._run()

            writer.assert_not_called()
            stopped = self._terminal_message(runner, StoppedMsg)
            self.assertIn("任务已停止，未获得可写入的 Word 翻译结果。", stopped.message)
            self.assertEqual(stopped.files[0]["status"], "unstarted")

    def test_stop_message_reports_progress_honestly_not_as_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.docx"
            document = Document()
            document.add_paragraph("已完成翻译的段落。")
            document.add_paragraph("翻译中途被打断的段落。")
            document.save(source)
            translated_text = "This paragraph finished translating before the stop landed."

            runner = WordTaskRunner(
                [WordFileItem(path=source, name=source.name, size_kb=1.0)],
                self._settings(),
                source_root=root,
            )

            def stop_mid_batch(texts, *_args, **kwargs):
                runner.stop()
                kwargs["drained_callback"]()
                return {"已完成翻译的段落。": translated_text}

            with ExitStack() as stack:
                self._runner_patches(
                    stack,
                    root=root,
                    prepared_by_path={source: _prepared(source)},
                    real_write=True,
                )
                stack.enter_context(
                    patch(
                        "core.word_task_runner.extract_word_segments",
                        return_value=[
                            WordSegment("已完成翻译的段落。", "paragraph", "正文第 1 段"),
                            WordSegment("翻译中途被打断的段落。", "paragraph", "正文第 2 段"),
                        ],
                    )
                )
                stack.enter_context(
                    patch(
                        "core.word_task_runner.tm_manager.lookup_batch",
                        return_value={
                            "已完成翻译的段落。": None,
                            "翻译中途被打断的段落。": None,
                        },
                    )
                )
                stack.enter_context(
                    patch(
                        "core.word_task_runner.translate_word_texts",
                        side_effect=stop_mid_batch,
                    )
                )
                stack.enter_context(
                    patch(
                        "core.word_task_runner._WordRecoveryPool",
                        return_value=_stub_recovery_pool(),
                    )
                )
                runner._run()

            stopped = self._terminal_message(runner, StoppedMsg)
            expected_message = (
                "任务已停止，停止前已获得的译文将照常写入文档并存入记忆库，"
                "未完成部分保留原文。"
                "进度：已译 1/2 段，1 段保留原文待续译，已写入 1 个文件。"
            )
            self.assertEqual(stopped.message, expected_message)
            # 账记在「停止」头上：不许把这次提前收尾包装成失败或错误。
            self.assertNotIn("失败", stopped.message)
            self.assertNotIn("错误", stopped.message)
            self.assertEqual(stopped.files[0]["status"], "succeeded")
            self.assertFalse(stopped.files[0]["error"])

            warn_logs = [
                message.message
                for message in list(runner._queue.queue)
                if getattr(message, "level", "") == "WARN"
            ]
            self.assertIn(expected_message, warn_logs)

    def test_stop_with_pool_unresolved_does_not_fake_translations(self) -> None:
        """停止把恢复池撤下后，未决候选不许被「原文当译文」占位。

        互审抓到的阻塞缺陷：停止改为不抛异常后，`api_translations[source] = source`
        这两行旧代码首次变得可达——被停止撤下的候选（一轮重试都没真正跑完）被
        占位成「已译」，进度报「已译 2/2」、还挨「重试 N 轮仍失败，需人工复核」
        的假帽子。修复后：它们缺席词典 → 写盘保留原文、进度计入「保留原文待
        续译」、不出任何 needs_review 标记。
        """
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.docx"
            document = Document()
            document.add_paragraph("已完成翻译的段落。")
            document.add_paragraph("翻译中途被打断的段落。")
            document.save(source)
            translated_text = "This paragraph finished translating before the stop landed."

            runner = WordTaskRunner(
                [WordFileItem(path=source, name=source.name, size_kb=1.0)],
                self._settings(),
                source_root=root,
            )

            def stop_mid_batch(texts, *_args, **kwargs):
                runner.stop()
                kwargs["drained_callback"]()
                return {"已完成翻译的段落。": translated_text}

            with ExitStack() as stack:
                self._runner_patches(
                    stack,
                    root=root,
                    prepared_by_path={source: _prepared(source)},
                    real_write=True,
                )
                stack.enter_context(
                    patch(
                        "core.word_task_runner.extract_word_segments",
                        return_value=[
                            WordSegment("已完成翻译的段落。", "paragraph", "正文第 1 段"),
                            WordSegment("翻译中途被打断的段落。", "paragraph", "正文第 2 段"),
                        ],
                    )
                )
                stack.enter_context(
                    patch(
                        "core.word_task_runner.tm_manager.lookup_batch",
                        return_value={
                            "已完成翻译的段落。": None,
                            "翻译中途被打断的段落。": None,
                        },
                    )
                )
                stack.enter_context(
                    patch(
                        "core.word_task_runner.translate_word_texts",
                        side_effect=stop_mid_batch,
                    )
                )
                stack.enter_context(
                    patch(
                        "core.word_task_runner._WordRecoveryPool",
                        return_value=_stub_recovery_pool(
                            unresolved_sources=["翻译中途被打断的段落。"]
                        ),
                    )
                )
                runner._run()

            stopped = self._terminal_message(runner, StoppedMsg)
            # 进度不许撒谎：真译文只有 1 条，占位不算「已译」。
            self.assertIn("已译 1/2 段", stopped.message)
            self.assertIn("1 段保留原文待续译", stopped.message)
            # 没翻到的段落在产物里保留原文。
            output_path = Path(stopped.files[0]["output"])
            written = Document(output_path)
            paragraphs = [paragraph.text for paragraph in written.paragraphs]
            self.assertIn(translated_text, paragraphs)
            self.assertIn("翻译中途被打断的段落。", paragraphs)
            # 不许出「重试 N 轮仍失败」的假帽子：这些段落是被停止撤下的，
            # 不是重试穷尽。
            review_items = stopped.files[0]["review_items"]
            for item in review_items:
                self.assertNotIn("重试后仍未获得有效译文", str(item))
            warn_logs = [
                message.message
                for message in list(runner._queue.queue)
                if getattr(message, "level", "") == "WARN"
            ]
            for message in warn_logs:
                self.assertNotIn("保留原文，需复核", message)

    def test_stop_with_pool_unresolved_and_zero_real_translations_writes_nothing(self) -> None:
        """全部候选都被停止撤下、一条真译文都没有时，空产物闸必须仍然生效。

        缺陷发作时的最坏形态：占位把 global_translations 填满，阶段 3 的
        「零译文不写盘」闸被绕过，写出一份全是原文的「成功」产物。修复后
        占位消失，闸照常拦下，不写空产物。
        """
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.docx"
            document = Document()
            document.add_paragraph("完全没来得及翻译的段落。")
            document.save(source)

            runner = WordTaskRunner(
                [WordFileItem(path=source, name=source.name, size_kb=1.0)],
                self._settings(),
                source_root=root,
            )

            def stop_and_return_nothing(texts, *_args, **kwargs):
                runner.stop()
                kwargs["drained_callback"]()
                return {}

            with ExitStack() as stack:
                writer = self._runner_patches(
                    stack,
                    root=root,
                    prepared_by_path={source: _prepared(source)},
                )
                stack.enter_context(
                    patch(
                        "core.word_task_runner.extract_word_segments",
                        return_value=[
                            WordSegment("完全没来得及翻译的段落。", "paragraph", "正文第 1 段")
                        ],
                    )
                )
                stack.enter_context(
                    patch(
                        "core.word_task_runner.tm_manager.lookup_batch",
                        return_value={"完全没来得及翻译的段落。": None},
                    )
                )
                stack.enter_context(
                    patch(
                        "core.word_task_runner.translate_word_texts",
                        side_effect=stop_and_return_nothing,
                    )
                )
                stack.enter_context(
                    patch(
                        "core.word_task_runner._WordRecoveryPool",
                        return_value=_stub_recovery_pool(
                            unresolved_sources=["完全没来得及翻译的段落。"]
                        ),
                    )
                )
                runner._run()

            writer.assert_not_called()
            stopped = self._terminal_message(runner, StoppedMsg)
            self.assertIn("任务已停止，未获得可写入的 Word 翻译结果。", stopped.message)
            self.assertEqual(stopped.files[0]["status"], "unstarted")


if __name__ == "__main__":
    unittest.main()
