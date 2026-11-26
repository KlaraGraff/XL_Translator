"""Word 这条路上模型原样还回来的段落，交给模型复查「原样保留对不对」（反馈 #28）。

讲义里「xyz,」「(as −a′s) bs + …」「xiy + c」这类公式、变量，模型原样还回来是对的；
以前 Word 把它直接当成没翻成功，重译三轮后保留原文、涂需复核底色。Excel、PPT、CAD、
PDF 原位替换早就把原样还回的交给复查，Word 现在也一样：复查认可的采用，不认可的照旧。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from docx import Document

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.translation_review import ReviewVerdict, build_review_prompt  # noqa: E402
from core.word_recovery_pool import (  # noqa: E402
    _evaluate_word_translation,
    _semantic_candidate_is_eligible,
    _SemanticArbitrationResult,
    _WordRecoveryPool,
)
from core.word_document import WordFileItem, WordSegment  # noqa: E402
from core.word_task_runner import WordTaskRunner  # noqa: E402
from tests import test_phase5_word_contracts as _contracts  # noqa: E402
from tests.app_data_isolation import IsolatedAppDataTestCase  # noqa: E402
from tests.test_audit_word_hang_fix import _ChatStubEngine, _retry_settings, _run_pool_with_timeout  # noqa: E402


def _echo_translate(texts, *args, **kwargs):
    return {text: text for text in texts}


def _pool() -> _WordRecoveryPool:
    return _WordRecoveryPool(
        engine=_ChatStubEngine(),
        target_lang="zh",
        retry_prompt="retry",
        retry_batch_settings=_retry_settings(),
        retry_attempts=2,
        source_lang="en",
        api_scheduler=None,
        concurrency=2,
        should_stop=lambda: False,
        enable_semantic=True,
    )


class EchoReviewTests(unittest.TestCase):
    def test_echo_is_eligible_for_review(self) -> None:
        for text in ("xyz,", "xiy + c", "Pump"):
            with self.subTest(text=text):
                validation = _evaluate_word_translation(
                    text, text, source_lang="en", target_lang="zh", allow_recovery=True
                ).validation
                self.assertEqual({issue.code for issue in validation.issues}, {"same_as_source"})
                self.assertTrue(
                    _semantic_candidate_is_eligible(
                        text, text, target_lang="zh", source_lang="en", validation=validation
                    )
                )

    def test_echo_with_only_punctuation_changed_is_eligible_for_review(self) -> None:
        # 本机复验 #332：「xyz,」「yxz,」「yzx,」模型还回「xyz，」这类（标点换成全角、或去掉逗号），
        # 没有一个汉字，被当成硬失败、不交复查，重译三轮后保留原文需复核。
        for source, candidate in (("xyz,", "xyz，"), ("yxz,", "yxz"), ("Pump", "Pump。")):
            with self.subTest(candidate=candidate):
                validation = _evaluate_word_translation(
                    source, candidate, source_lang="en", target_lang="zh", allow_recovery=True
                ).validation
                self.assertEqual({issue.code for issue in validation.issues}, {"missing_target_chinese"})
                self.assertTrue(
                    _semantic_candidate_is_eligible(
                        source, candidate, target_lang="zh", source_lang="en", validation=validation
                    )
                )

    def test_review_accepts_formula_kept_as_is(self) -> None:
        source = "(as −a′s) bs + (as−1−a′s−1) bs−1 + · · · + (ai−a′i) bi."
        problems: list[str] = []

        def _review(engine, src, candidate, **kwargs):
            problems.append(kwargs.get("problem", ""))
            return _SemanticArbitrationResult("equivalent", "公式，没有要翻的文字")

        pool = _pool()
        with patch("core.word_recovery_pool.translate_word_texts", _echo_translate), patch(
            "core.word_recovery_pool._run_semantic_arbitration", _review
        ):
            pool.add_candidate(source, source)
            pool.start()
            finished, outcome, error = _run_pool_with_timeout(pool)

        self.assertTrue(finished)
        self.assertIsNone(error)
        self.assertEqual(outcome.unresolved_sources, [])
        self.assertEqual(outcome.accepted_translations[source], source)
        self.assertIn(source, outcome.semantic_review_results)
        self.assertTrue(problems and "相同" in problems[0], problems)

    def test_review_rejects_untranslated_word(self) -> None:
        def _review(*args, **kwargs):
            return _SemanticArbitrationResult("not_equivalent", "没有翻译")

        pool = _pool()
        with patch("core.word_recovery_pool.translate_word_texts", _echo_translate), patch(
            "core.word_recovery_pool._run_semantic_arbitration", _review
        ):
            pool.add_candidate("Pump", "Pump")
            pool.start()
            finished, outcome, error = _run_pool_with_timeout(pool)

        self.assertTrue(finished)
        self.assertIsNone(error)
        self.assertEqual(outcome.unresolved_sources, ["Pump"])

    def test_echo_the_review_is_unsure_about_is_kept_without_marking(self) -> None:
        # 本机复验 #335：讲义表格里单独的「Ai」，复查答「无法确定」→ 重译三轮 → 保留原文、标黄。
        for answered, kept in ((True, True), (False, False)):
            with self.subTest(answered=answered):
                verdict = ReviewVerdict("uncertain", "看不出是变量还是缩写", answered=answered)
                pool = _pool()
                with patch("core.word_recovery_pool.translate_word_texts", _echo_translate), patch(
                    "core.word_recovery_pool.review_translations", lambda *a, **k: {"1": verdict}
                ):
                    pool.add_candidate("Ai", "Ai")
                    pool.start()
                    finished, outcome, error = _run_pool_with_timeout(pool)
                self.assertTrue(finished)
                self.assertIsNone(error)
                if kept:
                    self.assertEqual(outcome.unresolved_sources, [])
                    self.assertEqual(outcome.accepted_translations["Ai"], "Ai")
                else:
                    self.assertEqual(outcome.unresolved_sources, ["Ai"])

    def test_review_sees_the_text_around_the_cell(self) -> None:
        seen: list[str] = []

        def _review(engine, items, **kwargs):
            seen.extend(item.context for item in items)
            return {"1": ReviewVerdict("equivalent", "下标变量", answered=True)}

        pool = _WordRecoveryPool(
            engine=_ChatStubEngine(), target_lang="zh", retry_prompt="retry",
            retry_batch_settings=_retry_settings(), retry_attempts=2, source_lang="en",
            api_scheduler=None, concurrency=2, should_stop=lambda: False, enable_semantic=True,
            review_contexts={"pi": "前面：p1 ｜ p2；后面：pn"},
        )
        with patch("core.word_recovery_pool.translate_word_texts", _echo_translate), patch(
            "core.word_recovery_pool.review_translations", _review
        ):
            pool.add_candidate("pi", "pi")
            pool.start()
            finished, outcome, error = _run_pool_with_timeout(pool)
        self.assertTrue(finished)
        self.assertEqual(outcome.accepted_translations["pi"], "pi")
        self.assertEqual(seen, ["前面：p1 ｜ p2；后面：pn"])

    def test_review_prompt_allows_formulas_and_variables_unchanged(self) -> None:
        prompt = build_review_prompt()
        self.assertIn("公式、变量", prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)


def _queued(runner) -> list:
    items = []
    while not runner._queue.empty():
        items.append(runner._queue.get_nowait())
    return items


class EchoReviewRunnerTests(IsolatedAppDataTestCase):
    def test_echo_accepted_by_review_is_not_reported_as_untranslated(self) -> None:
        for text, returned in (("xiy + c", "xiy + c"), ("xyz,", "xyz，")):
            with self.subTest(returned=returned):
                self._check_runner(text, returned)

    def test_review_in_a_real_run_sees_the_neighbouring_cells(self) -> None:
        self._check_runner("pi", "pi", neighbours=("p1", "pn"))
        self.assertIn("前面：p1；后面：pn", self._contexts)

    def _check_runner(self, text: str, returned: str, neighbours: tuple[str, str] | None = None) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.docx"
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
                settings, source_root=root, source_lang="en",
            )

            def translate(texts, *_args, **kwargs):
                callback = kwargs.get("candidate_callback")
                for item in texts:
                    if callback is not None:
                        callback(item, returned)
                return {item: returned for item in texts}

            def retry_translate(texts, *args, **kwargs):
                return {item: returned for item in texts}

            with ExitStack() as stack:
                writer = _contracts.WordTaskResultContractTests._runner_patches(
                    self, stack, root=root, prepared_by_path={source: prepared}
                )
                coverage = stack.enter_context(
                    patch("core.word_task_runner._append_post_write_coverage_issues", return_value=0)
                )
                stack.enter_context(patch("core.word_task_runner.build_engine", return_value=_ChatStubEngine()))
                stack.enter_context(patch(
                    "core.word_task_runner.extract_word_segments",
                    return_value=[
                        WordSegment(source=item, kind="table_cell", location=f"table[0].cell[{i}]")
                        for i, item in enumerate((neighbours[0], text, neighbours[1]) if neighbours else (text,))
                    ],
                ))
                stack.enter_context(patch(
                    "core.word_task_runner.tm_manager.lookup_batch",
                    side_effect=lambda texts, *a, **k: {item: None for item in texts},
                ))
                stack.enter_context(patch("core.word_task_runner.translate_word_texts", side_effect=translate))
                stack.enter_context(patch("core.word_recovery_pool.translate_word_texts", retry_translate))
                self._contexts: list[str] = []

                def _review(*a, **k):
                    self._contexts.append(k.get("context", ""))
                    return _SemanticArbitrationResult("equivalent", "公式，没有要翻的文字")

                stack.enter_context(patch("core.word_recovery_pool._run_semantic_arbitration", _review))
                runner._run()

            if not neighbours:
                self.assertEqual(self._contexts, [""])  # 只有一段，没有前后文
            self.assertIn(returned, coverage.call_args.kwargs["already_target_sources"])
            self.assertEqual(writer.call_args.kwargs["translations"].get(text, text), returned)
            self.assertNotIn(text, writer.call_args.kwargs.get("review_marks") or {})
            logged = [getattr(m, "message", "") for m in _queued(runner) if getattr(m, "relay", False)]
            self.assertTrue(any("复查通过" in line for line in logged), logged)
