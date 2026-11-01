"""#283 本机复测：「品名」「数量」中日写法相同，译成中文时模型原样还回来不算没翻成。

以前 Word 会把它重翻几轮再标需复核，体检还报「仍有未译原文」；Excel 和 Word 现在用
同一条规则 is_same_han_spelling_in_chinese。
"""
from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from docx import Document

from core.language_preflight import TranslationLanguageResult
from core.task_runner import DoneMsg
from core.translation_filter import is_same_han_spelling_in_chinese
from core.word_document import WordFileItem, WordSegment
from core.word_task_runner import WordTaskRunner
from tests import test_phase5_word_contracts as _contracts
from tests.app_data_isolation import IsolatedAppDataTestCase

HAN = "品名"


class RuleTests(unittest.TestCase):
    def test_han_only_echo_into_chinese_counts_as_already_chinese(self) -> None:
        for source, target in (("品名", "zh"), ("品名", "zh_hant"), ("安全管理 2", "zh")):
            with self.subTest(source=source, target=target):
                self.assertTrue(is_same_han_spelling_in_chinese(source, source, target))

    def test_other_cases_still_go_through_validation(self) -> None:
        cases = [
            ("品名リスト", "品名リスト", "zh"),  # 带假名
            ("PVC管", "PVC管", "zh"),  # 带字母
            ("品名", "品名", "en"),  # 目标不是中文
            ("品名", "货品名称", "zh"),  # 模型给了别的译法
            ("设计说明", "设计说明", "zh_hant"),  # 简体原样还回来＝没转成繁体
        ]
        for source, translation, target in cases:
            with self.subTest(source=source, target=target):
                self.assertFalse(is_same_han_spelling_in_chinese(source, translation, target))


class WordRunnerTests(IsolatedAppDataTestCase):
    def _run(self, *, echo: str, model_answered: bool = True):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "ja.docx"
            Document().save(source)
            prepared = SimpleNamespace(
                path=source, method="编号预处理：Python 兜底", temp_paths=(),
                fallback_messages=(), labels_seen=0, labels_prepended=0,
                conversion_method="not_required", conversion_fidelity="not_required",
                numbering_method="python_conservative", numbering_fallback_messages=(),
            )
            settings = _contracts.WordTaskResultContractTests._settings()
            settings.target_lang = "zh"
            runner = WordTaskRunner(
                [WordFileItem(path=source, name=source.name, size_kb=1.0)],
                settings, source_root=root, source_lang="auto",
            )

            class _PreflightEngine:
                engine_name = "mock"

                def chat(self, system: str, user: str) -> str:
                    return '{"source_langs":["ja"]}'

            def translate(batch, _engine, target_lang, *_args, **kwargs):
                for text in batch:
                    if not model_answered:
                        continue
                    kwargs["source_result_callback"](
                        text,
                        TranslationLanguageResult(
                            source_text=text, translation=echo, source_lang="ja", target_lang=target_lang,
                        ),
                    )
                    kwargs["candidate_callback"](text, echo)
                return {text: echo for text in batch}

            with ExitStack() as stack:
                writer = _contracts.WordTaskResultContractTests._runner_patches(
                    self, stack, root=root, prepared_by_path={source: prepared}
                )
                coverage = stack.enter_context(
                    patch("core.word_task_runner._append_post_write_coverage_issues", return_value=0)
                )
                retries = stack.enter_context(patch("core.word_task_runner._WordRecoveryPool.add_candidate"))
                stack.enter_context(patch("core.word_task_runner.build_engine", return_value=_PreflightEngine()))
                stack.enter_context(patch(
                    "core.word_task_runner.extract_word_segments",
                    return_value=[WordSegment(source=HAN, kind="table_cell", location="表格第 1 格")],
                ))
                stack.enter_context(patch("core.word_task_runner.tm_manager.lookup_batch", return_value={}))
                stack.enter_context(patch("core.word_task_runner.translate_word_texts", side_effect=translate))
                runner._run()
            _contracts.WordTaskResultContractTests._terminal_message(runner, DoneMsg)
            return writer, retries, coverage

    def test_echoed_han_word_is_not_retried_or_reported(self) -> None:
        writer, retries, coverage = self._run(echo=HAN)
        retries.assert_not_called()
        self.assertEqual(coverage.call_args.kwargs["already_target_sources"], {HAN})
        self.assertEqual(writer.call_args.kwargs["translations"].get(HAN, HAN), HAN)

    def test_original_handed_back_without_a_model_answer_still_goes_to_retry(self) -> None:
        # 对照：请求失败时交回的原文不是模型的回答，照常重试、照常可能标复核。
        _writer, retries, coverage = self._run(echo=HAN, model_answered=False)
        retries.assert_called_once()
        self.assertNotIn(HAN, coverage.call_args.kwargs["already_target_sources"])

if __name__ == "__main__":
    unittest.main(verbosity=2)
