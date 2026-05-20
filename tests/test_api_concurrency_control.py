from __future__ import annotations

import unittest

from core.api_concurrency_control import is_api_concurrency_limit_error


class ApiConcurrencyControlTests(unittest.TestCase):
    def test_detects_concurrency_limit_feedback(self) -> None:
        exc = RuntimeError("上游反馈：当前 API Key 并发数已达到上限，请降低并发。")

        self.assertTrue(is_api_concurrency_limit_error(exc))

    def test_detects_too_many_requests_feedback(self) -> None:
        exc = RuntimeError("HTTP 429: Too Many Requests")

        self.assertTrue(is_api_concurrency_limit_error(exc))

    def test_does_not_treat_quota_or_auth_as_concurrency_limit(self) -> None:
        quota_exc = RuntimeError("insufficient_quota: billing hard limit exceeded")
        auth_exc = RuntimeError("401 unauthorized: invalid API key")

        self.assertFalse(is_api_concurrency_limit_error(quota_exc))
        self.assertFalse(is_api_concurrency_limit_error(auth_exc))


if __name__ == "__main__":
    unittest.main(verbosity=2)
