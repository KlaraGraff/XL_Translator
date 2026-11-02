"""模型回答写到输出长度上限被截断时：不原样重发同一批，拆小再发（#281 第 1 条）。

以前 finish_reason=length 被当成「结果不完整」的网络类错误：同一批原样重发
三次，每次一样长、一样被截断、一样计费，最后整批保留原文。
"""

from __future__ import annotations

import unittest

from core import text_transport as transport
from core.engine_dispatcher import TranslationBatchRunStats, translate_texts
from engines.base_engine import TranslationEngine
from engines.claude_engine import _extract_claude_text


class _OutputLimitEngine(TranslationEngine):
    """一次最多回得出 ``limit`` 条译文，多了就像真模型那样被截断。"""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.calls: list[list[str]] = []

    @property
    def engine_name(self) -> str:
        return "fake/output-limit"

    def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
        self.calls.append(list(texts))
        if len(texts) > self.limit:
            raise transport.TextTransportError(
                transport.TRUNCATED_MESSAGE, kind=transport.TRUNCATED_ANSWER
            )
        return {text: f"translated {text}" for text in texts}


class TruncatedAnswerTests(unittest.TestCase):
    def test_chat_completions_length_stop_is_reported_as_truncated(self) -> None:
        with self.assertRaises(transport.TextTransportError) as caught:
            transport.extract_chat_text(
                {"choices": [{"finish_reason": "length", "message": {"content": '["a", "b'}}]}
            )
        self.assertEqual(caught.exception.kind, transport.TRUNCATED_ANSWER)
        self.assertIn("截断", str(caught.exception))

    def test_responses_max_output_tokens_is_reported_as_truncated(self) -> None:
        payload = {
            "type": "response.incomplete",
            "response": {
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [],
            },
        }
        import json

        with self.assertRaises(transport.TextTransportError) as caught:
            transport.extract_responses_events([f"data: {json.dumps(payload)}"])
        self.assertEqual(caught.exception.kind, transport.TRUNCATED_ANSWER)

    def test_other_incomplete_responses_keep_their_old_kind(self) -> None:
        with self.assertRaises(transport.TextTransportError) as caught:
            transport.extract_chat_text({"choices": [{"finish_reason": "content_filter"}]})
        self.assertEqual(caught.exception.kind, "incomplete")

    def test_claude_max_tokens_stop_is_reported_as_truncated(self) -> None:
        with self.assertRaises(transport.TextTransportError) as caught:
            _extract_claude_text(
                {"stop_reason": "max_tokens", "content": [{"type": "text", "text": '["a'}]}
            )
        self.assertEqual(caught.exception.kind, transport.TRUNCATED_ANSWER)

    def test_truncated_batch_is_split_instead_of_resent(self) -> None:
        texts = [f"item {index}" for index in range(8)]
        engine = _OutputLimitEngine(limit=4)
        stats = TranslationBatchRunStats()
        errors: list[str] = []

        result = translate_texts(
            texts,
            engine,
            "fr",
            "system prompt",
            batch_size=8,
            concurrency=1,
            error_callback=errors.append,
            source_lang="en",
            stats=stats,
        )

        self.assertEqual(result, {text: f"translated {text}" for text in texts})
        # 8 条被截断一次，就拆成 4 + 4；不再把 8 条原样重发。
        self.assertEqual([len(call) for call in engine.calls], [8, 4, 4])
        self.assertEqual(stats.transient_retry_count, 0)
        self.assertEqual(stats.untranslated_count, 0)
        self.assertTrue(any("截断" in message for message in errors), errors)

    def test_a_single_item_that_is_still_truncated_keeps_its_source_with_the_reason(self) -> None:
        engine = _OutputLimitEngine(limit=0)
        stats = TranslationBatchRunStats()
        errors: list[str] = []

        result = translate_texts(
            ["only one"],
            engine,
            "fr",
            "system prompt",
            batch_size=8,
            concurrency=1,
            error_callback=errors.append,
            source_lang="en",
            stats=stats,
        )

        self.assertEqual(result, {"only one": "only one"})
        self.assertEqual(len(engine.calls), 1)
        self.assertTrue(any("截断" in message for message in errors), errors)


if __name__ == "__main__":
    unittest.main()
