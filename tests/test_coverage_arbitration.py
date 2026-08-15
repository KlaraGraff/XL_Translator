"""补译模式「这一对真的是原文＋译文吗」复核的回归。

判错的代价不对称：判成"已有译文"而其实不是，那段中文就永远留在文档里，体检也发现
不了（体检用的是同一套启发式）；判反了顶多多插一条译文，看得见、删得掉。所以下面
每一条都在守同一件事——宁可多翻，不可漏翻，同时别为此白烧模型调用。
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from docx import Document

from core import coverage_review, word_task_runner

from core.coverage_arbitration import (
    RETRANSLATE_MODEL,
    RETRANSLATE_UNCERTAIN,
    TRUST_KNOWN_TRANSLATION,
    TRUST_MODEL,
    TRUST_NO_MODEL,
    TRUST_OVER_CAP,
    TRUST_SHORT_TOKEN,
    ArbitrationOutcome,
    ArbitrationPair,
    PairReview,
    apply_arbitration,
    collect_arbitration_candidates,
    review_coverage_pairs,
)
from core.api_concurrency_control import ApiKeyTemporarilyUnavailableError
from core.mixed_language import MIXED_MARK_FOREIGN_NOISE
from core.translation_coverage import (
    COVERAGE_COVERED,
    COVERAGE_IGNORED,
    COVERAGE_SOURCE_ONLY,
    CoverageUnit,
    looks_like_foreign_acronym,
    looks_like_target_text,
)
from core.word_coverage import build_word_coverage_plan, write_untranslated_docx

# 一段真实长度的中文，和一条明显只翻了个开头的"译文"——长度比 0.2，可疑。
LONG_SOURCE = (
    "本工程受甲供短柱供货严重滞后影响，土建结构施工无法按原计划穿插进行，"
    "经与监理及业主协商，工期顺延一百六十九天。"
)
PARTIAL_TARGET = "Retard de livraison."
FULL_TARGET = (
    "En raison du retard important de livraison des potelets courts fournis par le "
    "maître d’ouvrage, les travaux de structure n’ont pas pu être menés selon le "
    "planning initial ; après concertation avec le maître d’œuvre et le maître "
    "d’ouvrage, le délai est prolongé de cent soixante-neuf jours."
)


# 长度正常、句子通顺，但讲的完全是另一件事——取消长度比预筛之前，这种"配错了对"
# 的组合会被免费信任、根本到不了模型跟前。
MISMATCHED_TARGET = (
    "Le présent chapitre décrit les mesures de sécurité applicables aux travaux "
    "en hauteur ainsi que les équipements de protection individuelle exigés."
)


def _pair(source: str, target: str, *, index: int = 0) -> CoverageUnit:
    return CoverageUnit(
        source_text=source,
        target_text=target,
        status=COVERAGE_COVERED,
        location=f"body.paragraph[{index}]",
        kind="paragraph",
        reason="下一段为目标语言译文。",
        data={"paragraph_index": index},
    )


def _batch_arbitrate(verdict: str, *, seen: list[list[ArbitrationPair]] | None = None):
    """把一个固定裁定包成批量回调：一次收一批，按 id 返回逐对结果。"""

    def arbitrate(pairs):
        batch = list(pairs)
        if seen is not None:
            seen.append(batch)
        return {pair.id: verdict for pair in batch}

    return arbitrate


class ForeignAcronymTests(unittest.TestCase):
    def test_all_caps_abbreviations_count_as_translation(self) -> None:
        for token in ("CCTEB", "ONEE", "SARL", "PV", "N/A", "BTR-ANODE-CCTEB-032"):
            self.assertTrue(looks_like_foreign_acronym(token), token)
            self.assertTrue(
                looks_like_target_text(token, source_lang="zh", target_lang="fr"),
                token,
            )

    def test_ordinary_words_and_chinese_are_not_abbreviations(self) -> None:
        for token in ("Béton", "Travaux", "施工单位", "A", "工程"):
            self.assertFalse(looks_like_foreign_acronym(token), token)


class CandidateSelectionTests(unittest.TestCase):
    def test_every_covered_position_is_reviewed_including_table_cells(self) -> None:
        units = [
            _pair("工程概况", "Présentation du projet", index=0),
            CoverageUnit(
                source_text="未译内容",
                status=COVERAGE_SOURCE_ONLY,
                location="body.paragraph[2]",
                kind="paragraph",
                reason="",
            ),
            CoverageUnit(
                source_text="表格原文",
                target_text="Texte du tableau",
                status=COVERAGE_COVERED,
                location="table[0].cell[0]",
                kind="table_cell",
                reason="",
            ),
            CoverageUnit(
                source_text="目录",
                status=COVERAGE_IGNORED,
                location="body.paragraph[3]",
                kind="paragraph",
                reason="",
            ),
        ]

        candidates = collect_arbitration_candidates(units)

        # 表格里最容易配错对——表头、单位名称、编号列，一格错位整列跟着错。
        self.assertEqual(
            [unit.location for unit in candidates],
            ["body.paragraph[0]", "table[0].cell[0]"],
        )


class CheapRuleTests(unittest.TestCase):
    """免费规则命中的对，一次模型调用都不能花。"""

    def test_short_foreign_token_is_trusted_without_the_model(self) -> None:
        seen: list[list[ArbitrationPair]] = []

        outcome = review_coverage_pairs(
            [_pair("序号", "N°"), _pair("施工单位", "CCTEB")],
            arbitrate=_batch_arbitrate("not_equivalent", seen=seen),
        )

        self.assertEqual(seen, [])
        self.assertEqual(outcome.model_check_count, 0)
        self.assertEqual(outcome.retranslated, [])
        self.assertEqual(
            {review.reason for review in outcome.reviews}, {TRUST_SHORT_TOKEN}
        )

    def test_known_translation_match_is_trusted_without_the_model(self) -> None:
        seen: list[list[ArbitrationPair]] = []

        outcome = review_coverage_pairs(
            [_pair(LONG_SOURCE, PARTIAL_TARGET)],
            # 记忆库里就是这条译名——译名是特定的，比对字符串就够了。
            known_translations={LONG_SOURCE: PARTIAL_TARGET},
            arbitrate=_batch_arbitrate("not_equivalent", seen=seen),
        )

        self.assertEqual(seen, [])
        self.assertEqual(outcome.reviews[0].reason, TRUST_KNOWN_TRANSLATION)

    def test_known_translation_comparison_ignores_case_and_spacing(self) -> None:
        outcome = review_coverage_pairs(
            [_pair(LONG_SOURCE, "  retard   DE livraison.  ")],
            known_translations={LONG_SOURCE: PARTIAL_TARGET},
            arbitrate=_batch_arbitrate("not_equivalent"),
        )

        self.assertEqual(outcome.reviews[0].reason, TRUST_KNOWN_TRANSLATION)

    def test_known_translation_mismatch_still_goes_to_the_model(self) -> None:
        outcome = review_coverage_pairs(
            [_pair(LONG_SOURCE, PARTIAL_TARGET)],
            known_translations={LONG_SOURCE: FULL_TARGET},
            arbitrate=_batch_arbitrate("not_equivalent"),
        )

        self.assertEqual(outcome.model_check_count, 1)
        self.assertEqual(outcome.reviews[0].reason, RETRANSLATE_MODEL)

    def test_a_normal_length_but_unrelated_pair_reaches_the_model(self) -> None:
        """长度比正常但配错了对——这正是删掉长度比免费规则要救回来的那一类。"""
        seen: list[list[ArbitrationPair]] = []

        outcome = review_coverage_pairs(
            [_pair(LONG_SOURCE, MISMATCHED_TARGET)],
            arbitrate=_batch_arbitrate("not_equivalent", seen=seen),
        )

        self.assertEqual(outcome.model_check_count, 1)
        self.assertEqual(
            [(pair.source, pair.candidate) for batch in seen for pair in batch],
            [(LONG_SOURCE, MISMATCHED_TARGET)],
        )
        self.assertEqual(outcome.reviews[0].reason, RETRANSLATE_MODEL)

    def test_a_faithful_full_length_translation_still_reaches_the_model(self) -> None:
        """完整译文也照送不误：长度对得上不代表配对没错，只有模型能分辨。"""
        outcome = review_coverage_pairs(
            [_pair(LONG_SOURCE, FULL_TARGET)],
            arbitrate=_batch_arbitrate("equivalent"),
        )

        self.assertEqual(outcome.model_check_count, 1)
        self.assertEqual(outcome.reviews[0].reason, TRUST_MODEL)

    def test_a_whole_paragraph_followed_by_a_stray_marker_still_goes_to_the_model(
        self,
    ) -> None:
        """整段中文后面跟着一个孤零零的 II / PV，是排版残留，不是它的译文。"""
        for stray in ("II", "PV", "N°", "kg"):
            with self.subTest(stray=stray):
                outcome = review_coverage_pairs(
                    [_pair(LONG_SOURCE, stray)],
                    arbitrate=_batch_arbitrate("not_equivalent"),
                )

                self.assertEqual(outcome.model_check_count, 1)
                self.assertEqual(len(outcome.retranslated), 1)

    def test_a_short_pair_without_a_foreign_token_goes_to_the_model(self) -> None:
        """"工期" vs "Délai" 不再靠长度比放行——Délai 不是简称，只能问模型。"""
        outcome = review_coverage_pairs(
            [_pair("工期", "Délai")],
            arbitrate=_batch_arbitrate("equivalent"),
        )

        self.assertEqual(outcome.model_check_count, 1)
        self.assertEqual(outcome.reviews[0].reason, TRUST_MODEL)


class ModelVerdictTests(unittest.TestCase):
    def test_equivalent_keeps_the_pair_covered(self) -> None:
        outcome = review_coverage_pairs(
            [_pair(LONG_SOURCE, PARTIAL_TARGET)],
            arbitrate=_batch_arbitrate("equivalent"),
        )

        self.assertEqual(outcome.reviews[0].reason, TRUST_MODEL)
        self.assertEqual(apply_arbitration(outcome), [])
        self.assertEqual(outcome.reviews[0].unit.status, COVERAGE_COVERED)

    def test_not_equivalent_flips_the_pair_back_to_untranslated(self) -> None:
        unit = _pair(LONG_SOURCE, PARTIAL_TARGET)
        outcome = review_coverage_pairs(
            [unit], arbitrate=_batch_arbitrate("not_equivalent")
        )

        flipped = apply_arbitration(outcome)

        self.assertEqual(flipped, [unit])
        self.assertEqual(unit.status, COVERAGE_SOURCE_ONLY)
        self.assertEqual(unit.data["arbitration"], RETRANSLATE_MODEL)

    def test_uncertain_also_retranslates(self) -> None:
        """拿不准就翻——漏翻是看不见的，多翻一条是看得见的。"""
        unit = _pair(LONG_SOURCE, PARTIAL_TARGET)
        outcome = review_coverage_pairs(
            [unit], arbitrate=_batch_arbitrate("uncertain")
        )

        self.assertEqual(outcome.reviews[0].reason, RETRANSLATE_UNCERTAIN)
        self.assertEqual(apply_arbitration(outcome), [unit])
        self.assertEqual(unit.status, COVERAGE_SOURCE_ONLY)

    def test_unusable_engine_keeps_the_heuristic_verdict(self) -> None:
        """本地引擎没有 chat，不能因为"没法确认"就把整份翻好的文档重翻一遍。"""
        units = [
            _pair(LONG_SOURCE, PARTIAL_TARGET, index=0),
            _pair(LONG_SOURCE, MISMATCHED_TARGET, index=1),
            _pair("工期", "Délai", index=2),
        ]

        outcome = review_coverage_pairs(units, arbitrate=None)

        self.assertEqual(outcome.model_check_count, 0)
        self.assertEqual(outcome.model_batch_count, 0)
        self.assertEqual(apply_arbitration(outcome), [])
        self.assertEqual(
            {review.reason for review in outcome.reviews}, {TRUST_NO_MODEL}
        )
        self.assertTrue(all(unit.status == COVERAGE_COVERED for unit in units))

    def test_model_check_cap_keeps_the_rest_covered_and_is_reported(self) -> None:
        units = [_pair(LONG_SOURCE, PARTIAL_TARGET, index=i) for i in range(5)]

        outcome = review_coverage_pairs(
            units,
            arbitrate=_batch_arbitrate("not_equivalent"),
            max_model_checks=2,
        )

        self.assertEqual(outcome.model_check_count, 2)
        self.assertEqual(outcome.skipped_over_cap, 3)
        self.assertEqual(len(apply_arbitration(outcome)), 2)
        # 超限的那几对保持原判，理由要如实说明"没判"，不能借用某条免费规则的名字。
        self.assertEqual(
            [
                review.reason
                for review in outcome.reviews
                if review.reason == TRUST_OVER_CAP
            ],
            [TRUST_OVER_CAP] * 3,
        )

    def test_model_batch_is_announced_before_it_starts(self) -> None:
        """预处理阶段唯一的网络等待，界面上必须有交代——对数和批数都要说。"""
        announced: list[tuple[int, int]] = []

        review_coverage_pairs(
            [_pair(LONG_SOURCE, PARTIAL_TARGET, index=i) for i in range(5)]
            + [_pair("序号", "N°", index=9)],
            arbitrate=_batch_arbitrate("equivalent"),
            batch_size=2,
            notify_model_checks=lambda count, batches: announced.append(
                (count, batches)
            ),
        )

        self.assertEqual(announced, [(5, 3)])

    def test_nothing_is_announced_when_no_pair_reaches_the_model(self) -> None:
        announced: list[tuple[int, int]] = []

        review_coverage_pairs(
            [_pair("序号", "N°")],
            arbitrate=_batch_arbitrate("equivalent"),
            notify_model_checks=lambda count, batches: announced.append(
                (count, batches)
            ),
        )

        self.assertEqual(announced, [])


class BatchProtocolTests(unittest.TestCase):
    """批量协议：按 id 认领裁定，认不到的一律 uncertain，绝不静默丢对。"""

    def test_pairs_are_split_into_batches_of_the_configured_size(self) -> None:
        seen: list[list[ArbitrationPair]] = []
        units = [_pair(LONG_SOURCE, PARTIAL_TARGET, index=i) for i in range(7)]

        outcome = review_coverage_pairs(
            units,
            arbitrate=_batch_arbitrate("equivalent", seen=seen),
            batch_size=3,
            max_workers=1,
        )

        self.assertEqual([len(batch) for batch in seen], [3, 3, 1])
        self.assertEqual(outcome.model_batch_count, 3)
        self.assertEqual(outcome.model_check_count, 7)
        # 每一对都拿到了裁定，一个都没漏。
        self.assertEqual(len(outcome.reviews), 7)
        self.assertEqual(outcome.retranslated, [])

    def test_verdicts_are_matched_by_id_not_by_position(self) -> None:
        """回来的顺序被打乱也要对得上——按位置对齐会把 A 的裁定安到 B 头上。"""
        units = [_pair(LONG_SOURCE, PARTIAL_TARGET, index=i) for i in range(3)]
        bad_id: list[str] = []

        def arbitrate(pairs):
            batch = list(pairs)
            # 只有第二对不等义，而且结果倒着返回。
            bad_id.append(batch[1].id)
            return {
                batch[2].id: "equivalent",
                batch[1].id: "not_equivalent",
                batch[0].id: "equivalent",
            }

        outcome = review_coverage_pairs(units, arbitrate=arbitrate, max_workers=1)
        flipped = apply_arbitration(outcome)

        self.assertEqual(bad_id, ["1"])
        self.assertEqual([unit.location for unit in flipped], ["body.paragraph[1]"])

    def test_a_missing_id_falls_back_to_uncertain(self) -> None:
        units = [_pair(LONG_SOURCE, PARTIAL_TARGET, index=i) for i in range(3)]

        def arbitrate(pairs):
            batch = list(pairs)
            # 模型漏返了第二对——不能当没这回事，也不能顺移成别人的裁定。
            return {batch[0].id: "equivalent", batch[2].id: "equivalent"}

        outcome = review_coverage_pairs(units, arbitrate=arbitrate, max_workers=1)

        self.assertEqual(len(outcome.reviews), 3)
        self.assertEqual(
            [review.reason for review in outcome.reviews],
            [TRUST_MODEL, RETRANSLATE_UNCERTAIN, TRUST_MODEL],
        )

    def test_unknown_ids_and_unparseable_verdicts_are_treated_as_uncertain(
        self,
    ) -> None:
        units = [_pair(LONG_SOURCE, PARTIAL_TARGET, index=i) for i in range(2)]

        def arbitrate(pairs):
            batch = list(pairs)
            return {
                "999": "equivalent",  # 不属于这批的 id，不能顶替任何一对
                batch[0].id: "oui, c'est ça",  # 认不出来的裁定
                batch[1].id: "equivalent",
            }

        outcome = review_coverage_pairs(units, arbitrate=arbitrate, max_workers=1)

        self.assertEqual(
            [review.reason for review in outcome.reviews],
            [RETRANSLATE_UNCERTAIN, TRUST_MODEL],
        )

    def test_a_batch_result_that_is_not_a_mapping_is_treated_as_uncertain(self) -> None:
        units = [_pair(LONG_SOURCE, PARTIAL_TARGET, index=i) for i in range(2)]

        outcome = review_coverage_pairs(
            units, arbitrate=lambda pairs: ["equivalent", "equivalent"], max_workers=1
        )

        self.assertEqual(
            [review.reason for review in outcome.reviews],
            [RETRANSLATE_UNCERTAIN, RETRANSLATE_UNCERTAIN],
        )

    def test_batches_carry_cleaned_text_and_stable_ids(self) -> None:
        seen: list[list[ArbitrationPair]] = []

        review_coverage_pairs(
            [_pair(f"  {LONG_SOURCE}  ", f"  {PARTIAL_TARGET}  ")],
            arbitrate=_batch_arbitrate("equivalent", seen=seen),
        )

        pair = seen[0][0]
        self.assertEqual(pair.id, "0")
        self.assertEqual(pair.source, LONG_SOURCE)
        self.assertEqual(pair.candidate, PARTIAL_TARGET)


class WriterIntegrationTests(unittest.TestCase):
    """改判之后，那一段必须真的被翻译并写进输出文档——否则整套复核是空转。"""

    def test_flipped_pair_is_translated_into_the_output_document(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_path = Path(tmp) / "source.docx"
            doc = Document()
            doc.add_paragraph(LONG_SOURCE)
            doc.add_paragraph("PV")  # 排版残留，被启发式误判成译文
            doc.save(source_path)

            plan = build_word_coverage_plan(
                source_path, target_lang="fr", source_lang="zh"
            )
            self.assertEqual(plan.source_texts, [])  # 启发式：整段都算已翻好

            outcome = review_coverage_pairs(
                plan.units, arbitrate=_batch_arbitrate("not_equivalent")
            )
            apply_arbitration(outcome)

            # 改判必须体现在写入器读的那个属性上，而不只是留在 outcome 里。
            self.assertEqual(plan.source_texts, [LONG_SOURCE])

            out_path = write_untranslated_docx(
                source_path=source_path,
                output_dir=Path(tmp) / "out",
                plan=plan,
                translations={LONG_SOURCE: FULL_TARGET},
                target_lang="fr",
            )

            texts = [p.text for p in Document(out_path).paragraphs]
            self.assertEqual(texts[:3], [LONG_SOURCE, FULL_TARGET, "PV"])

    def test_flipped_table_cell_keeps_the_suspect_text_and_gains_a_translation(
        self,
    ) -> None:
        """表格改判：新译文追加进格子，原来那半译文一个字都不删。

        删掉的话，万一模型判错了，用户连"原来写的是什么"都看不到；留着最多是一格里
        两条译文，肉眼一比就知道该留哪条。
        """
        with tempfile.TemporaryDirectory() as tmp:
            source_path = Path(tmp) / "source.docx"
            doc = Document()
            doc.add_paragraph("正文占位")
            table = doc.add_table(rows=1, cols=1)
            cell = table.cell(0, 0)
            cell.text = LONG_SOURCE
            cell.add_paragraph(MISMATCHED_TARGET)  # 配错了对的"译文"
            doc.save(source_path)

            plan = build_word_coverage_plan(
                source_path, target_lang="fr", source_lang="zh"
            )
            cell_units = [u for u in plan.units if u.kind == "table_cell"]
            self.assertEqual([u.status for u in cell_units], [COVERAGE_COVERED])
            self.assertNotIn(LONG_SOURCE, plan.source_texts)

            outcome = review_coverage_pairs(
                plan.units, arbitrate=_batch_arbitrate("not_equivalent")
            )
            apply_arbitration(outcome)
            self.assertIn(LONG_SOURCE, plan.source_texts)

            out_path = write_untranslated_docx(
                source_path=source_path,
                output_dir=Path(tmp) / "out",
                plan=plan,
                translations={LONG_SOURCE: FULL_TARGET},
                target_lang="fr",
            )

            written = Document(out_path).tables[0].cell(0, 0)
            lines = [p.text for p in written.paragraphs if p.text.strip()]
            self.assertEqual(lines, [LONG_SOURCE, MISMATCHED_TARGET, FULL_TARGET])


class RunnerGlueTests(unittest.TestCase):
    def _runner(self, logs: list[tuple[str, str]]):
        runner = word_task_runner.WordTaskRunner.__new__(
            word_task_runner.WordTaskRunner
        )
        runner._log = lambda level, message: logs.append((level, message))
        runner._stop_event = threading.Event()
        return runner

    def test_arbitration_failure_does_not_take_the_whole_file_down(self) -> None:
        """复核挂了只能退回原判——外层会把异常当成"文件打不开"，那样连译文都不出。"""
        logs: list[tuple[str, str]] = []
        unit = _pair(LONG_SOURCE, PARTIAL_TARGET)
        plan = SimpleNamespace(units=[unit])
        issues: list[dict] = []

        def boom(*args, **kwargs):
            raise RuntimeError("接口限流")

        with mock.patch.object(coverage_review, "review_coverage_pairs", boom):
            self._runner(logs)._arbitrate_coverage_pairs(
                plan,
                engine=object(),
                api_scheduler=None,
                target_lang="fr",
                source_lang="zh",
                lang_pair=None,
                concurrency=4,
                file_name="方案.docx",
                file_identity="方案.docx",
                quality_issues=issues,
                review_marks={},
            )

        self.assertEqual(unit.status, COVERAGE_COVERED)
        self.assertEqual(issues, [])
        self.assertTrue(
            any(level == "WARNING" and "接口限流" in message for level, message in logs),
            logs,
        )

    def test_a_clean_document_still_says_the_check_ran(self) -> None:
        """结论是"全都没问题"也要吭声，否则分不清是查过了还是压根没跑。"""
        logs: list[tuple[str, str]] = []
        plan = SimpleNamespace(units=[_pair("序号", "N°")])

        self._runner(logs)._arbitrate_coverage_pairs(
            plan,
            engine=object(),
            api_scheduler=None,
            target_lang="fr",
            source_lang="zh",
            lang_pair=None,
            concurrency=4,
            file_name="方案.docx",
            file_identity="方案.docx",
            quality_issues=[],
            review_marks={},
        )

        self.assertTrue(
            any("补译复核：1 对已有译文" in message for _, message in logs), logs
        )

    def test_a_flipped_pair_is_highlighted_and_reported_as_needing_review(self) -> None:
        """判否的那一段旁边留着一条可疑译文，必须上底色——报告里躺一条灰记录没人看。"""
        logs: list[tuple[str, str]] = []
        unit = _pair(LONG_SOURCE, MISMATCHED_TARGET)
        plan = SimpleNamespace(units=[unit])
        issues: list[dict] = []
        review_marks: dict[str, str] = {}

        outcome = ArbitrationOutcome(
            reviews=[PairReview(unit=unit, trusted=False, reason=RETRANSLATE_MODEL)],
            model_check_count=1,
            model_batch_count=1,
        )

        with mock.patch.object(
            coverage_review, "review_coverage_pairs", lambda *a, **kw: outcome
        ):
            self._runner(logs)._arbitrate_coverage_pairs(
                plan,
                engine=object(),
                api_scheduler=None,
                target_lang="fr",
                source_lang="zh",
                lang_pair=None,
                concurrency=4,
                file_name="方案.docx",
                file_identity="方案.docx",
                quality_issues=issues,
                review_marks=review_marks,
            )

        self.assertEqual(unit.status, COVERAGE_SOURCE_ONLY)
        self.assertEqual(review_marks, {LONG_SOURCE: MIXED_MARK_FOREIGN_NOISE})
        self.assertEqual(len(issues), 1)
        # 「已解决」是灰的，用户不会点开；这条要和其它需要人看的问题同一个等级。
        self.assertEqual(issues[0]["severity"], "needs_review")
        self.assertEqual(issues[0]["problem"], "紧邻段落不是这一段的译文")
        self.assertIn("人工核对", issues[0]["status"])

    def test_pairs_flipped_only_because_no_verdict_came_back_are_not_highlighted(
        self,
    ) -> None:
        """接口抖一下就是整整一批落到 uncertain——给它们全涂红，底色就废了。"""
        logs: list[tuple[str, str]] = []
        units = [_pair(LONG_SOURCE, FULL_TARGET, index=i) for i in range(3)]
        plan = SimpleNamespace(units=units)
        issues: list[dict] = []
        review_marks: dict[str, str] = {}

        outcome = ArbitrationOutcome(
            reviews=[
                PairReview(unit=unit, trusted=False, reason=RETRANSLATE_UNCERTAIN)
                for unit in units
            ],
            model_check_count=3,
            model_batch_count=1,
        )

        with mock.patch.object(
            coverage_review, "review_coverage_pairs", lambda *a, **kw: outcome
        ):
            self._runner(logs)._arbitrate_coverage_pairs(
                plan,
                engine=object(),
                api_scheduler=None,
                target_lang="fr",
                source_lang="zh",
                lang_pair=None,
                concurrency=4,
                file_name="方案.docx",
                file_identity="方案.docx",
                quality_issues=issues,
                review_marks=review_marks,
            )

        # 照样从严补译，只是不上底色：报告里留一条"已处理"的记录就够了。
        self.assertTrue(all(unit.status == COVERAGE_SOURCE_ONLY for unit in units))
        self.assertEqual(review_marks, {})
        self.assertEqual([issue["severity"] for issue in issues], ["resolved"] * 3)
        self.assertIn("未取得判定结果", issues[0]["status"])

    def test_the_batch_log_says_how_many_pairs_and_how_many_batches(self) -> None:
        logs: list[tuple[str, str]] = []
        units = [_pair(LONG_SOURCE, MISMATCHED_TARGET, index=i) for i in range(3)]
        plan = SimpleNamespace(units=units)

        def fake_review(*args, **kwargs):
            kwargs["notify_model_checks"](3, 2)
            return ArbitrationOutcome(
                reviews=[
                    PairReview(unit=unit, trusted=True, reason=TRUST_MODEL)
                    for unit in units
                ],
                model_check_count=3,
                model_batch_count=2,
            )

        with mock.patch.object(coverage_review, "review_coverage_pairs", fake_review):
            self._runner(logs)._arbitrate_coverage_pairs(
                plan,
                engine=object(),
                api_scheduler=None,
                target_lang="fr",
                source_lang="zh",
                lang_pair=None,
                concurrency=4,
                file_name="方案.docx",
                file_identity="方案.docx",
                quality_issues=[],
                review_marks={},
            )

        messages = [message for _, message in logs]
        self.assertTrue(any("分 2 批发出" in message for message in messages), messages)
        self.assertTrue(
            any("3 对送模型判定（分 2 批）" in message for message in messages), messages
        )


class BatchRequestTests(unittest.TestCase):
    """coverage_review.run_pair_arbitration_batch：一次请求判一批，结果按 id 回填。"""

    def _pairs(self, count: int) -> list[ArbitrationPair]:
        return [
            ArbitrationPair(id=str(i), source=LONG_SOURCE, candidate=FULL_TARGET)
            for i in range(count)
        ]

    def _engine(self, reply):
        calls: list[tuple[str, str]] = []

        def chat(system_prompt: str, user_payload: str) -> str:
            calls.append((system_prompt, user_payload))
            if isinstance(reply, Exception):
                raise reply
            return reply

        return SimpleNamespace(chat=chat, engine_name="fake"), calls

    def test_one_request_covers_the_whole_batch(self) -> None:
        engine, calls = self._engine(
            json.dumps(
                {
                    "results": [
                        {"id": "0", "verdict": "equivalent"},
                        {"id": "1", "verdict": "not_equivalent"},
                        {"id": "2", "verdict": "uncertain"},
                    ]
                }
            )
        )

        verdicts = coverage_review.run_pair_arbitration_batch(
            engine,
            self._pairs(3),
            target_lang="fr",
            source_lang="zh",
            api_scheduler=None,
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(
            verdicts, {"0": "equivalent", "1": "not_equivalent", "2": "uncertain"}
        )
        # 每一对都带着 id 进请求，模型才有东西可回。
        payload = json.loads(calls[0][1])
        self.assertEqual([item["id"] for item in payload["pairs"]], ["0", "1", "2"])

    def test_a_failed_request_returns_nothing_so_the_batch_falls_back_to_uncertain(
        self,
    ) -> None:
        engine, _ = self._engine(RuntimeError("网络中断"))
        warnings: list[str] = []

        verdicts = coverage_review.run_pair_arbitration_batch(
            engine,
            self._pairs(2),
            target_lang="fr",
            source_lang="zh",
            api_scheduler=None,
            error_callback=warnings.append,
        )

        self.assertEqual(verdicts, {})
        self.assertTrue(any("网络中断" in message for message in warnings), warnings)

    def test_an_unusable_key_keeps_bubbling_up(self) -> None:
        """这是调度信号，吞掉会让整轮任务在一把不能用的 key 上空转。"""
        engine, _ = self._engine(ApiKeyTemporarilyUnavailableError("key 暂不可用"))

        with self.assertRaises(ApiKeyTemporarilyUnavailableError):
            coverage_review.run_pair_arbitration_batch(
                engine,
                self._pairs(1),
                target_lang="fr",
                source_lang="zh",
                api_scheduler=None,
            )

    def test_garbage_reply_yields_no_verdicts(self) -> None:
        for reply in ("不是 JSON", "[]", json.dumps({"results": "equivalent"})):
            with self.subTest(reply=reply):
                engine, _ = self._engine(reply)
                self.assertEqual(
                    coverage_review.run_pair_arbitration_batch(
                        engine,
                        self._pairs(2),
                        target_lang="fr",
                        source_lang="zh",
                        api_scheduler=None,
                    ),
                    {},
                )

    def test_the_prompt_is_the_lenient_one_not_the_machine_translation_judge(
        self,
    ) -> None:
        """人工写的法定译名跟字面翻译对不齐，按机器译文的尺度卡会把它们全判成错。"""
        prompt = coverage_review.build_pair_arbitration_prompt()

        for expected in ("法定译名", "惯用译名", "同义词", "语序", "缩写", "简洁"):
            self.assertIn(expected, prompt)
        self.assertNotEqual(
            prompt, word_task_runner._build_semantic_arbitration_prompt()
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
