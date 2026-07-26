"""Engine-level failure semantics: no silent identity output, no futile retries.

A misconfigured key must fail fast instead of burning the whole exponential
backoff budget, and an exhausted local engine must raise so the dispatcher
records the failure instead of shipping originals as translations.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from engines.base_engine import is_retryable_engine_error
from engines.claude_engine import ClaudeEngine
from engines.ollama_engine import OllamaEngine


class _AuthError(Exception):
    def __init__(self, status_code: int = 401) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class _FailingClient:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.posts = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def post(self, *args, **kwargs):
        self.posts += 1
        raise self.error


class RetryPredicateTests(unittest.TestCase):
    def test_credential_and_validation_errors_are_not_retried(self) -> None:
        for status in (400, 401, 402, 403, 404, 405, 410, 422):
            self.assertFalse(
                is_retryable_engine_error(_AuthError(status)),
                status,
            )

    def test_rate_limits_server_errors_and_transport_errors_are_retried(self) -> None:
        for status in (429, 500, 502, 503):
            self.assertTrue(
                is_retryable_engine_error(_AuthError(status)),
                status,
            )
        self.assertTrue(is_retryable_engine_error(ValueError("no status here")))

    def test_status_on_the_response_attribute_is_honoured(self) -> None:
        class _WrappedError(Exception):
            def __init__(self, response) -> None:
                super().__init__("wrapped")
                self.response = response

        class _Response:
            status_code = 403

        self.assertFalse(is_retryable_engine_error(_WrappedError(_Response())))


class ClaudeRetryBehaviourTests(unittest.TestCase):
    def test_a_rejected_key_fails_after_a_single_attempt(self) -> None:
        fake = _FailingClient(_AuthError(401))
        engine = ClaudeEngine(api_key="bad-key", model="m")
        with patch("engines.claude_engine.httpx.Client", return_value=fake):
            with self.assertRaises(_AuthError):
                engine.translate_batch(["hi"], "en", "prompt")
        # Retrying a 401 through the whole backoff budget only delays the
        # user's feedback about a misconfigured key.
        self.assertEqual(fake.posts, 1)


class OllamaExhaustionTests(unittest.TestCase):
    def test_exhausted_retries_raise_instead_of_returning_originals(self) -> None:
        engine = OllamaEngine(model="m", concurrency=2)
        calls: list[int] = []

        async def _always_fail(system: str, user_msg: str) -> str:
            calls.append(1)
            raise RuntimeError("ollama down")

        async def _no_sleep(_delay: float) -> None:
            return None

        with (
            patch.object(engine, "_call_ollama", new=_always_fail),
            patch("engines.ollama_engine.asyncio.sleep", new=_no_sleep),
        ):
            # The old behaviour returned {text: text} here and the task
            # reported success with untranslated output and no failure detail.
            with self.assertRaises(RuntimeError):
                engine.translate_batch(["你好", "世界"], "en", "prompt")
        self.assertTrue(calls)


if __name__ == "__main__":
    unittest.main(verbosity=2)
