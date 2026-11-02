"""#283：第一次没认出原文语言时，换更大的样本再问一次；两次都没认出才走兜底。"""
from __future__ import annotations

import unittest

from core.language_preflight import (
    DEFAULT_MAX_SAMPLES,
    RETRY_MAX_SAMPLES,
    extract_retry_candidates,
    preflight_file_source_languages,
    preflight_files,
)

_JA = '{"source_langs":["ja"]}'


def _texts(count: int) -> list[str]:
    return [f"項目{i}" for i in range(count)] + [f"これは{i}番目の図面です" for i in range(count)]


class RetryTests(unittest.TestCase):
    def _detect(self, replies):
        calls: list[list[str]] = []

        def detector(samples, _target):
            calls.append(list(samples))
            reply = replies[len(calls) - 1]
            if isinstance(reply, Exception):
                raise reply
            return reply

        return calls, detector

    def test_first_answer_is_used_without_a_second_request(self) -> None:
        calls, detector = self._detect([_JA])
        result = preflight_files({"a.xlsx": _texts(30)}, detector, target_lang="zh")["a.xlsx"]
        self.assertEqual(result.source_langs, ("ja",))
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.request_count, 1)
        self.assertFalse(result.retried)

    def test_unusable_answer_or_failed_request_is_retried_with_a_larger_sample(self) -> None:
        for first in ("???", '{"source_langs":["und"]}', RuntimeError("timeout")):
            with self.subTest(first=repr(first)):
                calls, detector = self._detect([first, _JA])
                result = preflight_files({"a.xlsx": _texts(30)}, detector, target_lang="zh")["a.xlsx"]
                self.assertEqual(result.source_langs, ("ja",))
                self.assertEqual(result.request_count, 2)
                self.assertTrue(result.retried)
                self.assertEqual(len(calls[0]), DEFAULT_MAX_SAMPLES)
                self.assertEqual(len(calls[1]), RETRY_MAX_SAMPLES)

    def test_two_misses_fall_back_to_unknown(self) -> None:
        calls, detector = self._detect(["???", RuntimeError("down")])
        result = preflight_files({"a.xlsx": _texts(30)}, detector, target_lang="zh")["a.xlsx"]
        self.assertEqual(result.source_langs, ())
        self.assertEqual(len(calls), 2)

    def test_file_without_any_usable_sentence_sends_nothing(self) -> None:
        calls, detector = self._detect([])
        result = preflight_files({"a.xlsx": ["123", "2026-11-01", "=SUM(A1)"]}, detector, target_lang="zh")["a.xlsx"]
        self.assertEqual(calls, [])
        self.assertEqual(result.request_count, 0)

    def test_retry_sample_prefers_unseen_sentences_with_kana(self) -> None:
        first = [f"項目{i}" for i in range(12)]
        texts = first + ["品名", "これは図面です", "数量"]
        retry = extract_retry_candidates(texts, first)
        self.assertEqual(retry[0], "これは図面です")
        self.assertEqual(set(retry[:3]), {"これは図面です", "品名", "数量"})
        self.assertEqual(len(retry), len(texts))

    def test_request_style_entry_point_retries_too(self) -> None:
        replies = iter(["???", _JA])
        result = preflight_file_source_languages(
            _texts(5), target_lang="zh", request=lambda _system, _user: next(replies)
        )
        self.assertEqual(result.source_langs, ("ja",))
        self.assertEqual(result.request_count, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
