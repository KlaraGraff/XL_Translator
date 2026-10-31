"""规则没通过的译文改由模型复查（core/translation_review，maple 2026-10-31）。"""

from __future__ import annotations

import json
import threading

import pytest

from core import engine_dispatcher
from core.api_concurrency_control import ApiKeyTemporarilyUnavailableError
from core.cad_translation import CadTranslationPipeline, _valid_translation, cad_rule_problem
from core.engine_dispatcher import TranslationBatchRunStats, quality_rule_problem, translate_texts
from core.pdf_vector import classify as pdf_classify
from core.text_translation_service import TranslationResults
from core.translation_review import (
    OUTCOME_ACCEPTED,
    OUTCOME_FIXED,
    OUTCOME_REJECTED,
    ReviewItem,
    RuleFailure,
    build_review_batches,
    review_and_repair,
    review_translations,
)


class ReviewEngine:
    """假模型：translate_batch 按 ``translations`` 回答（列表依次给每一稿），chat 当复查员。"""

    engine_name = "openai/fake-review"

    def __init__(self, translations=None, verdicts=None, *, chat_error=None, skip_ids=()):
        self.translations = {k: (v if isinstance(v, list) else [v]) for k, v in (translations or {}).items()}
        #: 原文 → verdict（或 (verdict, reason)），也可以是按次数的列表
        self.verdicts = verdicts or {}
        self.chat_error = chat_error
        self.skip_ids = set(skip_ids)
        self.prompts: list[str] = []
        self.chats: list[dict] = []
        self.lock = threading.Lock()

    def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
        out = {}
        with self.lock:
            self.prompts.append(system_prompt)
            for text in texts:
                answers = self.translations.get(text) or [f"[{target_lang}] {text}"]
                out[text] = answers.pop(0) if len(answers) > 1 else answers[0]
        return out

    def chat(self, system, user):
        payload = json.loads(user)
        with self.lock:
            self.chats.append(payload)
        if self.chat_error is not None:
            raise self.chat_error
        results = []
        for item in payload["items"]:
            if item["id"] in self.skip_ids:
                continue
            verdict = self.verdicts.get(item["source"], "uncertain")
            if isinstance(verdict, list):
                verdict = verdict.pop(0) if len(verdict) > 1 else verdict[0]
            reason = "测试理由"
            if isinstance(verdict, tuple):
                verdict, reason = verdict
            results.append({"id": item["id"], "verdict": verdict, "reason": reason})
        return json.dumps({"results": results}, ensure_ascii=False)


def _translate(engine, texts, *, target="en", retries=3, stop=None, stats=None, **kwargs):
    stats = stats if stats is not None else TranslationBatchRunStats()
    results = translate_texts(
        texts, engine, target, "SYSTEM", 10, 2, source_lang="zh", stats=stats,
        item_retries=retries, should_stop=stop, **kwargs,
    )
    return results, stats


# ── 规则：哪些正常写法规则就放行，哪些要问模型（方案第 2 节的表）────────────────


@pytest.mark.parametrize(
    ("source", "translation"),
    [
        ("25厚1:2.5水泥砂浆找平", "Ragréage au mortier de ciment 1:2,5, épaisseur 25"),  # 反馈 #23
        ("坡度1:2.5", "Уклон 1:2,5"),  # 俄文小数逗号
        ("2026年3月24日", "24 mars 2026"),
        ("第十二条", "Article 12"),
        ("1,500元", "1 500 yuans"),
    ],
)
def test_cad_now_uses_the_shared_number_rule(source, translation):
    # 以前 CAD 按顺序逐字比，这几条全被丢掉；现在规则直接放行，不用花钱问模型。
    assert cad_rule_problem(source, translation) == ""
    assert _valid_translation(source, translation) == (True, "")


def test_cad_still_rejects_a_changed_number():
    assert _valid_translation("Room 101", "房间102")[0] is False
    assert _valid_translation("设备基坑：2830X1700mm", "Equipment pit: 2830X2100mm")[0] is False
    assert "1700" in cad_rule_problem("设备基坑：2830X1700mm", "Equipment pit: 2830X2100mm")
    # 复查判过意思一致的只查硬伤
    assert _valid_translation("Room 101", "房间102", check_rules=False) == (True, "")
    assert _valid_translation("Room 101", "房间�", check_rules=False)[0] is False


@pytest.mark.parametrize(
    ("source", "translation", "target", "source_lang"),
    [
        ("共3层", "Three floors in total", "en", "zh"),
        ("Article 12", "第十二条", "zh", "en"),
        ("50%", "百分之五十", "zh", "en"),
        ("Schneider Electric", "Schneider Electric", "zh", "en"),
    ],
)
def test_correct_translations_the_rules_cannot_pass_go_to_review(source, translation, target, source_lang):
    assert quality_rule_problem(source, translation, target, source_lang=source_lang)


def test_an_unchanged_line_that_is_not_source_text_passes_without_review():
    # 中译法文档里的英文行原样回来是对的：不问模型，也不算退回。
    assert quality_rule_problem("MIT License", "MIT License", "fr", source_lang="zh") == ""


# ── 分批 ─────────────────────────────────────────────────────────────────


def test_batches_hold_at_most_ten_items_and_about_3000_chars():
    items = [ReviewItem(str(i), "原文" * 5, "translation") for i in range(25)]
    assert [len(batch) for batch in build_review_batches(items)] == [10, 10, 5]
    long_one = ReviewItem("long", "长" * 3200, "x")
    batches = build_review_batches([items[0], long_one, items[1]])
    assert [[item.id for item in batch] for batch in batches] == [["long"], ["0", "1"]]
    heavy = [ReviewItem(str(i), "字" * 900, "y" * 100) for i in range(4)]
    assert [len(batch) for batch in build_review_batches(heavy)] == [3, 1]


def test_answers_are_matched_by_id_and_a_missed_one_is_asked_again_alone():
    engine = ReviewEngine(verdicts={"甲": "equivalent", "乙": "not_equivalent"}, skip_ids={"2"})
    items = [ReviewItem("1", "甲", "A"), ReviewItem("2", "乙", "B")]
    verdicts = review_translations(engine, items, source_lang="zh", target_lang="en")
    # 第 2 条第一次、单独再问时都没答：拿不准
    assert verdicts["1"].equivalent
    assert verdicts["2"].verdict == "uncertain"
    assert [len(chat["items"]) for chat in engine.chats] == [2, 1]
    assert engine.chats[1]["items"][0]["id"] == "2"


def test_a_failed_review_is_unsure_and_an_unusable_key_bubbles_up():
    items = [ReviewItem("1", "甲", "A")]
    failing = ReviewEngine(chat_error=RuntimeError("网络中断"))
    assert review_translations(failing, items, source_lang="zh", target_lang="en")["1"].verdict == "uncertain"
    with pytest.raises(ApiKeyTemporarilyUnavailableError):
        review_translations(
            ReviewEngine(chat_error=ApiKeyTemporarilyUnavailableError("key")), items,
            source_lang="zh", target_lang="en",
        )


def test_stopped_review_sends_nothing():
    engine = ReviewEngine(verdicts={"甲": "equivalent"})
    verdicts = review_translations(
        engine, [ReviewItem("1", "甲", "A")], source_lang="zh", target_lang="en", should_stop=lambda: True,
    )
    assert verdicts["1"].verdict == "uncertain" and engine.chats == []


def test_repair_retranslates_with_the_reason_up_to_the_retry_count():
    calls: list[tuple[str, str, str]] = []

    def retranslate(source, previous, reason):
        calls.append((source, previous, reason))
        return "still wrong"

    engine = ReviewEngine(verdicts={"共3层": ("not_equivalent", "漏了 3")})
    outcomes = review_and_repair(
        [RuleFailure("共3层", "Floors", "原文中的数字在译文里没找到：3")],
        engine=engine,
        rule_problem=lambda s, t: "原文中的数字在译文里没找到：3",
        retranslate=retranslate,
        max_rounds=2,
        source_lang="zh",
        target_lang="en",
    )
    assert outcomes["共3层"].status == OUTCOME_REJECTED
    assert len(calls) == 2 and calls[0][2] == "漏了 3"
    # 每一稿都复查：第一稿 + 两次重译后的稿子
    assert len(engine.chats) == 3


# ── 接进 translate_texts（Excel、CAD、PDF 原位、扩展格式都经过这里）─────────────


def test_review_accepts_a_correct_translation_the_rules_flagged():
    engine = ReviewEngine({"共3层": "Three floors in total"}, {"共3层": "equivalent"})
    results, stats = _translate(engine, ["共3层", "门"])
    assert results["共3层"] == "Three floors in total"
    assert stats.review_accepted == {"共3层": "测试理由"}
    assert stats.quality_reset_count == 0
    # 只有没过规则的那一条送去复查
    assert [item["source"] for chat in engine.chats for item in chat["items"]] == ["共3层"]


def test_not_equivalent_is_retranslated_with_the_problem_and_the_fix_is_used():
    engine = ReviewEngine(
        {"25厚1:2.5水泥砂浆找平": ["Mortar screed 1:2.5", "Mortar screed 1:2.5, 25 mm thick"]},
        {"25厚1:2.5水泥砂浆找平": ("not_equivalent", "漏了厚度 25")},
    )
    results, stats = _translate(engine, ["25厚1:2.5水泥砂浆找平"])
    assert results["25厚1:2.5水泥砂浆找平"] == "Mortar screed 1:2.5, 25 mm thick"
    assert stats.review_fixed_sources == {"25厚1:2.5水泥砂浆找平"}
    assert not stats.review_accepted and stats.quality_reset_count == 0
    retry_prompt = engine.prompts[-1]
    assert "漏了厚度 25" in retry_prompt and "Mortar screed 1:2.5" in retry_prompt


@pytest.mark.parametrize(
    "engine",
    [
        ReviewEngine({"共3层": "Floors"}, {"共3层": "uncertain"}),
        ReviewEngine({"共3层": "Floors"}, chat_error=RuntimeError("超时")),
    ],
    ids=["uncertain", "error"],
)
def test_unsure_or_failed_review_falls_back_to_the_old_behaviour(engine):
    results, stats = _translate(engine, ["共3层"])
    assert results["共3层"] == "共3层"
    assert stats.quality_reset_count == 1
    assert "共3层" in stats.quality_reset_reasons and not stats.review_accepted


def test_stopping_skips_the_review():
    engine = ReviewEngine({"共3层": "Floors"}, {"共3层": "equivalent"})
    stopped = {"value": False}

    def stop():
        return stopped["value"]

    def progress(done, total):
        stopped["value"] = True

    results, stats = _translate(engine, ["共3层"], stop=stop, progress_callback=progress)
    assert results["共3层"] == "共3层" and engine.chats == []


def test_an_engine_without_chat_keeps_the_old_quality_gate():
    class NoChat:
        engine_name = "openai/no-chat"

        def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
            return {text: "Floors" for text in texts}

    results, stats = _translate(NoChat(), ["共3层"])
    assert results["共3层"] == "共3层" and stats.quality_reset_count == 1


def test_extra_check_failures_are_reviewed_too():
    engine = ReviewEngine({"Bolt 12": "螺栓十二"}, {"Bolt 12": "equivalent"})
    results, stats = _translate(engine, ["Bolt 12"], target="zh", extra_check=cad_rule_problem)
    assert results["Bolt 12"] == "螺栓十二" and "Bolt 12" in stats.review_accepted
    assert "12" in engine.chats[0]["items"][0]["rule_finding"]


def test_review_requests_use_the_recovery_lane_of_the_shared_scheduler():
    from core.api_scheduler import WeightedApiScheduler

    seen: list[str] = []
    scheduler = WeightedApiScheduler(2)
    original = scheduler.slot

    def slot(weight, *, category="normal", should_stop=None):
        seen.append(category)
        return original(weight, category=category, should_stop=should_stop)

    scheduler.slot = slot
    engine = ReviewEngine({"共3层": "Floors"}, {"共3层": "equivalent"})
    _translate(engine, ["共3层"], api_scheduler=scheduler)
    assert "recovery" in seen


# ── 各页面怎么用复查结果 ─────────────────────────────────────────────────


def test_cad_keeps_a_review_accepted_translation_and_reports_rejected_reasons(tmp_path):
    def _dxf(*pairs):
        return "".join(f"{code}\n{value}\n" for code, value in pairs)

    source = tmp_path / "plan.dxf"
    source.write_text(_dxf((0, "TEXT"), (1, "Bolt 12"), (0, "TEXT"), (1, "Room 101"), (0, "EOF")), encoding="utf-8")
    output = tmp_path / "plan_zh.dxf"

    def translator(texts, glossary):
        return TranslationResults(
            {"Bolt 12": "螺栓十二"},
            review_accepted={"Bolt 12": "十二就是 12"},
            rejected={"Room 101": "数字 101 改成了 102"},
        )

    result = CadTranslationPipeline(converter=None, translator=translator).translate_file(source, output)
    written = output.read_text(encoding="utf-8")
    assert "Bolt 12" not in written and "\\U+87BA" in written  # 老格式 DXF 按 \U+XXXX 写字
    assert "Room 101" in written
    assert result.review_accepted == [{"source": "Bolt 12", "translation": "螺栓十二", "reason": "十二就是 12"}]
    assert result.unresolved == [{"source": "Room 101", "reason": "数字 101 改成了 102"}]


def test_pdf_inplace_writes_review_accepted_rows_and_lists_rejected_ones():
    seen_checks = []

    def translate(texts, prompt, extra_check=None):
        seen_checks.append(extra_check)
        return TranslationResults(
            {"Niveau 3": "第三层"},
            review_accepted={"Niveau 3": "三就是 3"},
            rejected={"Pente 2,5 %": "坡度数字丢了"},
        )

    rows = [
        {"id": "a", "source_text": "Niveau 3", "origin": "text"},
        {"id": "b", "source_text": "Pente 2,5 %", "origin": "text"},
    ]
    for row in rows:
        row.setdefault("display_rotation", 0)
    pdf_classify.classify_and_translate(rows, translate=translate, source_lang="fr", target_lang="zh")
    by_id = {row["id"]: row for row in rows}
    assert by_id["a"]["decision"] == "write"
    assert by_id["a"]["decision_reason"] == "model_review_accepted"
    assert by_id["b"]["decision"] == "review" and by_id["b"]["decision_reason"] == "check_failed"
    assert by_id["b"]["review_note"] == "坡度数字丢了"
    check = seen_checks[0]
    assert check is not None and "2.5" in check("Pente 2,5 %", "坡度") + check("Pente 2,5 %", "坡度")


def test_word_meaning_check_goes_through_the_shared_reviewer():
    from core.word_task_runner import _run_semantic_arbitration

    engine = ReviewEngine(verdicts={"合同总价": "equivalent"})
    result = _run_semantic_arbitration(
        engine, "合同总价", "Prix total du contrat", target_lang="fr", source_lang="zh", api_scheduler=None,
    )
    assert result.equivalent
    assert engine.chats[0]["items"][0] == {
        "id": "1", "source": "合同总价", "translation": "Prix total du contrat", "rule_finding": "",
    }


def test_service_drops_rejected_sources_and_carries_the_review(monkeypatch):
    from core.text_translation_service import TextTranslationService
    from settings import AppSettings

    def fake_translate_texts(texts, engine, target_lang, prompt, *args, stats=None, **kwargs):
        stats.record_quality_reset("坏", "数字丢了")
        stats.record_review_accepted("好", "意思一致")
        return {"坏": "坏", "好": "Good"}

    monkeypatch.setattr(engine_dispatcher, "translate_texts", fake_translate_texts)
    service = TextTranslationService(AppSettings(), surface_key="cad", rate_surface="cad", target_lang="en")
    monkeypatch.setattr(service, "engine", lambda: object())
    results = service.translate(["坏", "好"], prompt="P")
    assert dict(results) == {"好": "Good"}
    assert results.rejected == {"坏": "数字丢了"} and results.review_accepted == {"好": "意思一致"}
    assert OUTCOME_ACCEPTED and OUTCOME_FIXED  # constants exported
