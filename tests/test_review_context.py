"""复查时带上前后文（反馈 #28：讲义表格里单独一格「pi」，复查每次判得不一样）。"""

from __future__ import annotations

from core.review_context import contexts_by_file, neighbour_contexts
from core.translation_review import (
    ReviewItem,
    build_review_batches,
    build_review_prompt,
    review_translations,
)
from tests.test_translation_review import ReviewEngine


def test_each_text_gets_the_texts_around_it():
    texts = ["p1", "p2", "pi", "pn", "Sum of all pi"]
    contexts = neighbour_contexts(texts, before=2, after=2)
    assert contexts["pi"] == "前面：p1 ｜ p2；后面：pn ｜ Sum of all pi"
    assert contexts["p1"] == "后面：p2 ｜ pi"
    assert contexts["Sum of all pi"] == "前面：pi ｜ pn"


def test_repeated_text_uses_its_first_place_and_long_text_is_clipped():
    contexts = neighbour_contexts(["a b", "x" * 100, "pi", "a b"], before=2, after=1, item_chars=10)
    assert contexts["pi"] == "前面：a b ｜ xxxxxxxxx…"
    assert contexts["a b"] == "后面：xxxxxxxxx…"


def test_files_do_not_lend_each_other_context():
    locations = {
        "pi": [{"file": "a.docx"}],
        "p2": [{"file": "a.docx"}],
        "Pump": [{"file": "b.docx"}],
    }
    contexts = contexts_by_file(locations)
    assert contexts["pi"] == "后面：p2"
    assert "Pump" not in contexts


def test_context_is_sent_to_the_reviewer_and_explained_in_the_prompt():
    engine = ReviewEngine(verdicts={"pi": "equivalent"})
    review_translations(
        engine,
        [ReviewItem("1", "pi", "pi", "译文和原文相同", "前面：p1 ｜ p2；后面：pn"), ReviewItem("2", "Pump", "泵")],
        source_lang="en",
        target_lang="zh",
    )
    items = {item["id"]: item for chat in engine.chats for item in chat["items"]}
    assert items["1"]["context"] == "前面：p1 ｜ p2；后面：pn"
    assert "context" not in items["2"]
    assert "context" in build_review_prompt() and "下标变量" in build_review_prompt()


def test_context_counts_toward_the_batch_size():
    items = [ReviewItem(str(i), "pi", "pi", "", "x" * 1000) for i in range(4)]
    assert len(build_review_batches(items, max_items=10, max_chars=3000)) == 2
