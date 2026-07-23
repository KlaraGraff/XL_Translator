from __future__ import annotations

import unittest

from core.language_preflight import (
    MIXED_SOURCE_LANG,
    UNKNOWN_SOURCE_LANG,
    extract_preflight_candidates,
    normalize_translation_language_result,
    parse_preflight_response,
    preflight_files,
    preflight_file_source_languages,
)


class LanguagePreflightTests(unittest.TestCase):
    def test_candidates_are_bounded_and_exclude_non_content(self) -> None:
        candidates = extract_preflight_candidates(
            [
                "=SUM(A1:A2)",
                "2026-07-24",
                "1.",
                "施工方案需要翻译。",
                "施工方案需要翻译。",
                "A substantial English sentence for detection.",
            ],
            max_samples=2,
            max_sample_chars=80,
            max_total_chars=120,
        )
        self.assertEqual(
            candidates,
            ["施工方案需要翻译。", "A substantial English sentence for detection."],
        )

    def test_parser_limits_to_two_actual_languages_and_rejects_unknown(self) -> None:
        languages, uncertain, error = parse_preflight_response(
            '{"source_langs":["zh","en","fr","mixed"]}'
        )
        self.assertEqual(languages, ("zh", "en"))
        self.assertTrue(uncertain)
        self.assertEqual(error, "")

        languages, uncertain, error = parse_preflight_response(
            '{"source_langs":["auto","und"]}'
        )
        self.assertEqual(languages, ())
        self.assertTrue(uncertain)
        self.assertTrue(error)

    def test_each_file_has_at_most_one_detector_request(self) -> None:
        calls: list[tuple[list[str], str]] = []

        def detector(samples: list[str], target_lang: str) -> str:
            calls.append((samples, target_lang))
            return '{"source_langs":["fr","en"]}'

        results = preflight_files(
            {
                "one.xlsx": ["Bonjour le monde", "Bonjour le monde"],
                "empty.xlsx": ["=SUM(A1:A2)", "12345"],
                "two.docx": ["Hello from Word"],
            },
            detector,
            target_lang="zh",
        )
        self.assertEqual(len(calls), 2)
        self.assertEqual(results["one.xlsx"].request_count, 1)
        self.assertEqual(results["empty.xlsx"].request_count, 0)
        self.assertEqual(results["two.docx"].source_langs, ("fr", "en"))
        self.assertEqual(results["one.xlsx"].tm_lang_pairs("zh"), ("fr-zh", "en-zh"))

    def test_translation_result_requires_actual_source_for_tm(self) -> None:
        result = normalize_translation_language_result(
            "Bonjour",
            {"translation": "你好", "source_lang": "fr"},
            target_lang="zh",
            allowed_source_langs=("fr", "en"),
        )
        self.assertEqual(result.source_lang, "fr")
        self.assertTrue(result.tm_eligible)

        for reported in ("auto", MIXED_SOURCE_LANG, UNKNOWN_SOURCE_LANG, "xx"):
            result = normalize_translation_language_result(
                "ambiguous",
                {"translation": "译文", "source_lang": reported},
                target_lang="zh",
                allowed_source_langs=("fr", "en"),
            )
            self.assertEqual(result.source_lang, UNKNOWN_SOURCE_LANG)
            self.assertFalse(result.tm_eligible)

    def test_no_candidate_file_does_not_call_request(self) -> None:
        calls = 0

        def request(_system: str, _user: str) -> str:
            nonlocal calls
            calls += 1
            return '{"source_langs":["en"]}'

        result = preflight_file_source_languages(
            ["=A1", "100%"],
            target_lang="zh",
            request=request,
        )
        self.assertEqual(result.request_count, 0)
        self.assertEqual(calls, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
