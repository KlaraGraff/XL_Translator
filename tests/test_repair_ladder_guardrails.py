# -*- coding: utf-8 -*-
"""修复阶梯批量护栏与主流程重构的回归锁定（评审 C6/C9/U0-U5/U9）。

- run_repair_ladder：上限、熔断、停止、拒收理由、进度回调；
- 惯例贯通：TM 卫生沿用主流程投出的文档级序号惯例，不再各投各的；
- TM 写入取「文件最终译文」而非 API 原始返回；
- TM 清洗批次失败时 0 API 惯例建议挂在异常上带出，不整批蒸发。
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from core.residual_pipeline import run_residual_pass
from core.residual_repair import (
    METHOD_FEEDBACK_RETRANSLATION,
    RepairLadderResult,
    run_repair_ladder,
)
from core.tm_hygiene import sanitize_tm_pairs


class _Unit:
    """带 source_text / target_text 的最小残留单元。"""

    def __init__(self, source_text: str, target_text: str):
        self.source_text = source_text
        self.target_text = target_text


def _sentence_unit(index: int) -> _Unit:
    # sentence_block 类别：阶梯只走带反馈重译，每单元恰好一次 send。
    # 源文用汉字数字编号——阿拉伯数字会触发 C5 数字包含校验，
    # 让「验收通过」的固定回复被正当拒收，测的就不再是护栏本身了。
    label = "一二三四五六七"[index - 1]
    return _Unit(
        f"本工程{label}号楼采用整体浇筑工艺施工。",
        f"本工程{label}号楼采用整体浇筑工艺 pour la construction.",
    )


_ACCEPTED_REPLY = (
    '{"repaired": "Ce projet utilise un procede de coulage integral '
    'pour la construction."}'
)


class RepairLadderGuardrailTest(unittest.TestCase):
    """批量护栏：上限、熔断、停止都不许静默丢单元。"""

    def test_breaker_stops_after_consecutive_transport_failures(self):
        calls = []

        def dead_send(system, user):
            calls.append(user)
            raise RuntimeError("connection refused")

        units = [_sentence_unit(i) for i in range(1, 7)]
        result = run_repair_ladder(
            units, target_lang="fr", send=dead_send, breaker_threshold=3
        )
        self.assertTrue(result.breaker_tripped)
        # 每个 sentence_block 单元一次重译请求，3 次连败后熔断
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(result.remaining), 6)
        self.assertEqual(result.accepted, {})

    def test_success_resets_breaker_counter(self):
        state = {"n": 0}

        def flaky_send(system, user):
            state["n"] += 1
            if state["n"] % 2 == 1:
                raise RuntimeError("timeout")
            return _ACCEPTED_REPLY

        units = [_sentence_unit(i) for i in range(1, 7)]
        result = run_repair_ladder(
            units, target_lang="fr", send=flaky_send, breaker_threshold=3
        )
        # 失败-成功交替，连续失败数永远到不了 3
        self.assertFalse(result.breaker_tripped)

    def test_over_cap_units_are_kept_but_not_sent(self):
        calls = []

        def counting_send(system, user):
            calls.append(user)
            return _ACCEPTED_REPLY

        units = [_sentence_unit(i) for i in range(1, 6)]
        result = run_repair_ladder(
            units, target_lang="fr", send=counting_send, max_units=2
        )
        self.assertEqual(result.over_cap_count, 3)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(result.accepted), 2)
        # 超限单元一个不丢，原样留在待复核清单里
        self.assertEqual(len(result.remaining), 3)

    def test_stop_keeps_remaining_units(self):
        state = {"sent": 0}

        def send_once(system, user):
            state["sent"] += 1
            return _ACCEPTED_REPLY

        units = [_sentence_unit(i) for i in range(1, 4)]
        result = run_repair_ladder(
            units,
            target_lang="fr",
            send=send_once,
            should_stop=lambda: state["sent"] >= 1,
        )
        self.assertEqual(len(result.accepted), 1)
        self.assertEqual(len(result.remaining), 2)

    def test_reject_reasons_and_progress_are_reported(self):
        progress = []
        units = [_sentence_unit(1)]
        result = run_repair_ladder(
            units,
            target_lang="fr",
            send=lambda s, u: "协议外的自由发挥",
            on_progress=lambda done, total: progress.append((done, total)),
        )
        self.assertEqual(progress, [(1, 1)])
        self.assertEqual(result.accepted, {})
        reasons = result.reject_reasons[units[0].source_text]
        self.assertTrue(any("协议内回复" in reason for reason in reasons))

    def test_accepted_repair_reports_method(self):
        units = [_sentence_unit(1)]
        result = run_repair_ladder(
            units, target_lang="fr", send=lambda s, u: _ACCEPTED_REPLY
        )
        self.assertEqual(
            result.method_counts, {METHOD_FEEDBACK_RETRANSLATION: 1}
        )
        self.assertIsInstance(result, RepairLadderResult)


class ConventionThreadingTest(unittest.TestCase):
    """文档级序号惯例必须贯通到子集调用，不许各投各的。"""

    # 兄弟段全是罗马数字括号写法，子集自行投票会得出 paren_roman
    _ROMAN_SIBLINGS = [
        ("（一）表层裂缝", "(I) Fissures superficielles"),
        ("（二）结构裂缝", "(II) Fissures structurelles"),
        ("（三）质量控制", "（三）Contrôle de la qualité"),
    ]

    def test_run_residual_pass_uses_caller_convention(self):
        result = run_residual_pass(
            self._ROMAN_SIBLINGS, target_lang="fr", convention="paren_arabic"
        )
        self.assertEqual(result.convention, "paren_arabic")
        self.assertEqual(
            result.fixes.get("（三）质量控制"),
            "(3) Contrôle de la qualité",
        )

    def test_run_residual_pass_still_votes_without_caller_convention(self):
        result = run_residual_pass(self._ROMAN_SIBLINGS, target_lang="fr")
        self.assertEqual(result.convention, "paren_roman")
        self.assertEqual(
            result.fixes.get("（三）质量控制"),
            "(III) Contrôle de la qualité",
        )

    def test_sanitize_tm_pairs_follows_doc_convention(self):
        hygiene = sanitize_tm_pairs(
            self._ROMAN_SIBLINGS, target_lang="fr", convention="paren_arabic"
        )
        fixed = dict(hygiene.pairs)["（三）质量控制"]
        self.assertEqual(fixed, "(3) Contrôle de la qualité")


class TmWriteFinalTextTest(unittest.TestCase):
    """TM 只许存「文件最终译文」：修复阶梯改完的版本，不是 API 原始返回。"""

    def _runner(self):
        from core.task_runner import TaskRunner
        from settings import AppSettings

        return TaskRunner([], AppSettings())

    def test_batch_path_stores_final_translations(self):
        runner = self._runner()
        raw = "（三）Contrôle de la qualité 未修复原始返回"
        final = "(3) Contrôle de la qualité"
        with patch(
            "core.task_runner.tm_manager.insert_batch", return_value=1
        ) as insert:
            written, error = runner.store_api_results_in_tm(
                auto_source_lang=False,
                normal_api_language_results={},
                normal_api_translations={"（三）质量控制": raw},
                text_source_scopes={},
                target_lang="fr",
                lang_pair="zh-fr",
                max_len=200,
                engine_name="fake/engine",
                final_translations={"（三）质量控制": final},
                convention="paren_arabic",
            )
        self.assertIsNone(error)
        stored_pairs = insert.call_args.args[0]
        self.assertEqual(stored_pairs, [("（三）质量控制", final)])

    def test_auto_language_path_stores_final_translations(self):
        from core.language_preflight import TranslationLanguageResult

        runner = self._runner()
        item = TranslationLanguageResult(
            "（三）质量控制",
            "（三）Contrôle de la qualité",
            source_lang="zh",
            target_lang="fr",
        )
        with patch(
            "core.task_runner.tm_manager.insert_auto_entries", return_value=1
        ) as insert:
            runner.store_api_results_in_tm(
                auto_source_lang=True,
                normal_api_language_results={"（三）质量控制": item},
                normal_api_translations={},
                text_source_scopes={"（三）质量控制": [frozenset({"zh"})]},
                target_lang="fr",
                lang_pair=None,
                max_len=200,
                engine_name="fake/engine",
                final_translations={"（三）质量控制": "(3) Contrôle de la qualité"},
                convention="paren_arabic",
            )
        entries = insert.call_args.args[0]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["translation"], "(3) Contrôle de la qualité")


class CleaningPartialSuggestionTest(unittest.TestCase):
    """TM 清洗批次失败时，0 API 惯例建议必须挂在异常上带出去。"""

    def test_batch_error_carries_convention_suggestions(self):
        from core.tm_cleaner import (
            CleanSuggestion,
            TmCleaningBatchError,
            run_cleaning,
        )

        class _Engine:
            engine_name = "fake-cloud"

        deterministic = [
            CleanSuggestion(
                entry_id=7,
                source_text="第三节 质量控制",
                old_target="Troisième section Contrôle",
                new_target="Section 3 Contrôle",
            )
        ]
        with patch(
            "core.tm_cleaner.tm_manager.get_all_entries_for_cleaning",
            return_value=[{"id": 7, "source_text": "第三节 质量控制", "translation": "x"}],
        ), patch(
            "core.tm_cleaner.build_convention_suggestions",
            return_value=deterministic,
        ), patch(
            "core.tm_cleaner._run_cleaning_threaded",
            side_effect=TmCleaningBatchError(1, 2, "provider unavailable"),
        ):
            with self.assertRaises(TmCleaningBatchError) as ctx:
                run_cleaning("zh-fr", _Engine())
        self.assertEqual(ctx.exception.partial_suggestions, deterministic)


if __name__ == "__main__":
    unittest.main()
