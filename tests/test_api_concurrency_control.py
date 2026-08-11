from __future__ import annotations

import unittest
from unittest.mock import patch

from core.api_concurrency_control import (
    ApiKeyTemporarilyUnavailableError,
    handle_api_concurrency_limit,
    is_api_concurrency_limit_error,
    reset_minimum_capacity_watch,
)
from core.api_scheduler import (
    API_CONCURRENCY_ACTION_REDUCED,
    WeightedApiScheduler,
)


def _limit_error() -> RuntimeError:
    return RuntimeError("HTTP 429: Too Many Requests")


class MinimumCapacityPolicyTests(unittest.TestCase):
    """A key at the minimum cap gets waited out, not declared dead."""

    def setUp(self) -> None:
        super().setUp()
        for name, value in (
            ("MINIMUM_CAPACITY_BASE_DELAY", 0.01),
            ("MINIMUM_CAPACITY_MAX_DELAY", 0.01),
        ):
            patcher = patch(f"core.api_concurrency_control.{name}", value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_limit_above_the_minimum_still_reduces_capacity(self) -> None:
        scheduler = WeightedApiScheduler(8)

        decision = handle_api_concurrency_limit(
            _limit_error(),
            scheduler=scheduler,
            request_generation=None,
            context_label="Excel",
        )

        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, API_CONCURRENCY_ACTION_REDUCED)
        self.assertLess(decision.current_capacity, 8)

    def test_at_the_minimum_the_caller_is_told_to_retry_not_to_give_up(self) -> None:
        scheduler = WeightedApiScheduler(1)
        reset_minimum_capacity_watch(scheduler)
        messages: list[str] = []

        decision = handle_api_concurrency_limit(
            _limit_error(),
            scheduler=scheduler,
            request_generation=None,
            context_label="Excel",
            error_callback=messages.append,
        )

        self.assertIsNotNone(decision)
        self.assertTrue(any("等待" in message for message in messages))

    def test_a_key_limited_past_the_grace_window_fails_the_task(self) -> None:
        scheduler = WeightedApiScheduler(1)
        reset_minimum_capacity_watch(scheduler)

        with patch("core.api_concurrency_control.MINIMUM_CAPACITY_GRACE_SECONDS", 0.02):
            handle_api_concurrency_limit(
                _limit_error(),
                scheduler=scheduler,
                request_generation=None,
                context_label="Excel",
            )
            with self.assertRaises(ApiKeyTemporarilyUnavailableError):
                for _ in range(50):
                    handle_api_concurrency_limit(
                        _limit_error(),
                        scheduler=scheduler,
                        request_generation=None,
                        context_label="Excel",
                    )

    def test_the_wait_stops_early_when_the_task_is_being_cancelled(self) -> None:
        scheduler = WeightedApiScheduler(1)
        reset_minimum_capacity_watch(scheduler)

        with patch("core.api_concurrency_control.MINIMUM_CAPACITY_BASE_DELAY", 30.0), \
             patch("core.api_concurrency_control.MINIMUM_CAPACITY_MAX_DELAY", 30.0):
            decision = handle_api_concurrency_limit(
                _limit_error(),
                scheduler=scheduler,
                request_generation=None,
                context_label="Excel",
                should_stop=lambda: True,
            )

        self.assertIsNotNone(decision)

    def test_progress_resets_the_episode(self) -> None:
        scheduler = WeightedApiScheduler(4)
        reset_minimum_capacity_watch(scheduler)

        # One reduction counts as progress, so the next at-minimum hit starts a
        # fresh grace window rather than inheriting an expired one.
        handle_api_concurrency_limit(
            _limit_error(),
            scheduler=scheduler,
            request_generation=None,
            context_label="Excel",
        )
        with patch("core.api_concurrency_control.MINIMUM_CAPACITY_GRACE_SECONDS", 60.0):
            decision = handle_api_concurrency_limit(
                _limit_error(),
                scheduler=scheduler,
                request_generation=None,
                context_label="Excel",
            )

        self.assertIsNotNone(decision)

    def test_one_limit_episode_writes_at_most_two_run_log_lines(self) -> None:
        """A burst of 429s is one event to the user, not twenty."""
        scheduler = WeightedApiScheduler(40)
        reset_minimum_capacity_watch(scheduler)
        messages: list[str] = []

        with patch("core.api_concurrency_control.MINIMUM_CAPACITY_GRACE_SECONDS", 600.0):
            for _ in range(30):
                handle_api_concurrency_limit(
                    _limit_error(),
                    scheduler=scheduler,
                    request_generation=None,
                    context_label="Excel",
                    error_callback=messages.append,
                )

        self.assertLessEqual(len(messages), 2, messages)
        self.assertTrue(any("放慢" in message for message in messages), messages)
        self.assertTrue(any("最慢档" in message for message in messages), messages)

    def test_the_user_facing_lines_carry_no_internal_capacity_numbers(self) -> None:
        scheduler = WeightedApiScheduler(40)
        reset_minimum_capacity_watch(scheduler)
        messages: list[str] = []

        handle_api_concurrency_limit(
            _limit_error(),
            scheduler=scheduler,
            request_generation=None,
            context_label="Excel",
            error_callback=messages.append,
        )

        self.assertEqual(len(messages), 1)
        self.assertNotIn("40", messages[0])
        self.assertNotIn("32", messages[0])
        self.assertNotIn("并发上限", messages[0])

    def test_the_failure_message_does_not_leak_the_grace_window(self) -> None:
        scheduler = WeightedApiScheduler(1)
        reset_minimum_capacity_watch(scheduler)

        with patch("core.api_concurrency_control.MINIMUM_CAPACITY_GRACE_SECONDS", 0.02):
            with self.assertRaises(ApiKeyTemporarilyUnavailableError) as caught:
                for _ in range(50):
                    handle_api_concurrency_limit(
                        _limit_error(),
                        scheduler=scheduler,
                        request_generation=None,
                        context_label="Excel",
                    )

        text = str(caught.exception)
        self.assertNotIn("120", text)
        self.assertIn("请稍后重试", text)

    def test_a_non_limit_error_is_left_alone(self) -> None:
        scheduler = WeightedApiScheduler(1)

        decision = handle_api_concurrency_limit(
            RuntimeError("401 unauthorized: invalid API key"),
            scheduler=scheduler,
            request_generation=None,
            context_label="Excel",
        )

        self.assertIsNone(decision)


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
