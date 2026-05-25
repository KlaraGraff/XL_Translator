from __future__ import annotations

import unittest

from core.pdf_review import (
    PdfPageReviewResult,
    PdfReviewIssue,
    parse_pdf_review_result,
)


class PdfReviewTests(unittest.TestCase):
    def test_parse_review_json_keeps_minor_suggestions_non_blocking(self) -> None:
        result = parse_pdf_review_result(
            """
            {
              "pass": true,
              "blocking_issues": [],
              "minor_suggestions": ["术语可进一步统一"],
              "summary": "可采用"
            }
            """
        )

        self.assertTrue(result.passed)
        self.assertEqual(result.minor_suggestions, ["术语可进一步统一"])
        self.assertEqual(result.blocking_issues, [])

    def test_parse_invalid_review_response_fails_closed(self) -> None:
        result = parse_pdf_review_result("模型只说了一句看起来还行")

        self.assertFalse(result.passed)
        self.assertEqual(result.blocking_issues[0].type, "invalid_review_response")

    def test_parse_plain_negative_review_judgement_fails_without_blocking_json(self) -> None:
        result = parse_pdf_review_result("N")

        self.assertFalse(result.passed)
        self.assertEqual(result.blocking_issues[0].type, "review_failed")
        self.assertEqual(result.summary, "审核未通过。")

    def test_review_manifest_shape(self) -> None:
        result = PdfPageReviewResult(
            passed=False,
            blocking_issues=[
                PdfReviewIssue(
                    type="wrong_translation",
                    location="表格右上角",
                    problem="编号标签误译",
                    suggestion="保留为报告号",
                )
            ],
            minor_suggestions=["字体略细"],
            summary="需重新生成",
            raw_text="{...}",
        )

        payload = result.to_manifest()

        self.assertFalse(payload["pass"])
        self.assertEqual(payload["blocking_issues"][0]["location"], "表格右上角")
        self.assertEqual(payload["minor_suggestions"], ["字体略细"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
