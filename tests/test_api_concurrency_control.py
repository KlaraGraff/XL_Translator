from __future__ import annotations

import errno
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import patch

import httpx

from core import api_concurrency_control
from core.api_concurrency_control import (
    ApiKeyTemporarilyUnavailableError,
    UPSTREAM_RETRY_AFTER_MAX_SECONDS,
    extract_retry_after,
    handle_api_concurrency_limit,
    is_api_concurrency_limit_error,
    is_local_descriptor_limit_error,
    reset_minimum_capacity_watch,
)
from core.api_scheduler import (
    API_CONCURRENCY_ACTION_REDUCED,
    WeightedApiScheduler,
)


def _limit_error() -> RuntimeError:
    return RuntimeError("HTTP 429: Too Many Requests")


def _response_with_headers(
    headers: dict[str, str] | None = None,
    status: int = 429,
    text: str = "Too Many Requests",
) -> httpx.Response:
    request = httpx.Request("POST", "https://service.test/v1/chat/completions")
    return httpx.Response(status, headers=headers or {}, text=text, request=request)


def _http_error(
    headers: dict[str, str] | None = None,
    status: int = 429,
    text: str = "Too Many Requests",
) -> httpx.HTTPStatusError:
    response = _response_with_headers(headers, status, text)
    return httpx.HTTPStatusError(f"HTTP {status}", request=response.request, response=response)


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

    def test_wrapped_local_descriptor_limit_reduces_shared_capacity(self) -> None:
        scheduler = WeightedApiScheduler(8)
        messages: list[str] = []
        try:
            raise OSError(errno.EMFILE, "Too many open files")
        except OSError as cause:
            error = RuntimeError("connection failed")
            error.__cause__ = cause

        self.assertTrue(is_local_descriptor_limit_error(error))
        self.assertFalse(is_api_concurrency_limit_error(error))
        decision = handle_api_concurrency_limit(
            error,
            scheduler=scheduler,
            request_generation=None,
            context_label="PDF",
            error_callback=messages.append,
        )
        self.assertIsNotNone(decision)
        self.assertLess(decision.current_capacity, 8)
        self.assertTrue(any("本机同时打开的网络连接过多" in message and "降低速度" in message for message in messages))
        self.assertFalse(any("接口反馈请求过于频繁" in message for message in messages))

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
        self.assertTrue(decision.should_retry)
        self.assertTrue(any("稍后将重试" in message for message in messages))

    def test_local_descriptor_limit_at_minimum_waits_without_upstream_message(self) -> None:
        scheduler = WeightedApiScheduler(1)
        reset_minimum_capacity_watch(scheduler)
        messages: list[str] = []
        decision = handle_api_concurrency_limit(
            OSError(errno.EMFILE, "Too many open files"),
            scheduler=scheduler,
            request_generation=None,
            context_label="PDF",
            error_callback=messages.append,
            should_stop=lambda: True,
        )
        self.assertIsNotNone(decision)
        self.assertTrue(decision.should_retry)
        self.assertTrue(any("本机同时打开的网络连接仍然过多" in message for message in messages))
        self.assertFalse(any("模型服务仍提示繁忙" in message for message in messages))

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

    def test_the_minute_line_never_says_it_waited_zero_seconds(self) -> None:
        # 本机验证 2026-10-21：一批过去以后计时重新开始，下一次碰到繁忙就写了
        # 「已等待 0 秒，正在继续重试」。只有真的连续等了一分钟才说。
        scheduler = WeightedApiScheduler(1)
        reset_minimum_capacity_watch(scheduler)
        messages: list[str] = []
        clock = [1000.0]

        def hit() -> None:
            handle_api_concurrency_limit(
                _limit_error(), scheduler=scheduler, request_generation=None,
                context_label="PDF", error_callback=messages.append,
            )

        with patch("core.api_concurrency_control.time.monotonic", lambda: clock[0]), \
             patch("core.api_concurrency_control.MINIMUM_CAPACITY_BASE_DELAY", 0.0), \
             patch("core.api_concurrency_control.MINIMUM_CAPACITY_MAX_DELAY", 0.0), \
             patch("core.api_concurrency_control.MINIMUM_CAPACITY_GRACE_SECONDS", 600.0):
            hit()  # 「已降至最低速度」
            clock[0] += 70
            reset_minimum_capacity_watch(scheduler)  # a batch got through
            hit()
            self.assertFalse([m for m in messages if "已等待" in m], messages)
            clock[0] += 70
            hit()  # still busy a minute later: now the line is due

        waits = [m for m in messages if "已等待" in m]
        self.assertEqual(len(waits), 1, messages)
        self.assertIn("已等待 70 秒", waits[0])

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
        self.assertTrue(any("降低发送速度" in message for message in messages), messages)
        self.assertTrue(any("最低速度" in message for message in messages), messages)

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


class RetryAfterHeaderTests(unittest.TestCase):
    """审计 中-13：限流退避只按本地公式算，上游给的 Retry-After 一律被丢掉。

    上游说「60 秒后再来」，本地 2 秒后又打过去，把限流窗口不断刷新；反过来上游
    说「1 秒即可」时又白等 30 秒。这里守住「读得到、认得出、等得够」三件事。
    """

    def _hit_with(self, exc: BaseException, *, should_stop=None) -> None:
        scheduler = WeightedApiScheduler(1)
        reset_minimum_capacity_watch(scheduler)
        handle_api_concurrency_limit(
            exc,
            scheduler=scheduler,
            request_generation=None,
            context_label="Excel",
            should_stop=should_stop,
        )

    def test_the_response_header_reaches_the_limit_classifier(self) -> None:
        # 探针原本的断言：限流响应里的 Retry-After 不能被无声丢弃。
        response = _response_with_headers({"retry-after": "60"})
        texts = list(api_concurrency_control._collect_response_texts(response))

        self.assertIn("retry-after: 60", texts)

    def test_seconds_form_is_read(self) -> None:
        self.assertEqual(extract_retry_after(_http_error({"Retry-After": "25"})), 25.0)
        self.assertEqual(extract_retry_after(_http_error({"Retry-After": "1.5"})), 1.5)

    def test_http_date_form_is_read(self) -> None:
        deadline = datetime.now(timezone.utc) + timedelta(seconds=20)
        header = format_datetime(deadline, usegmt=True)

        seconds = extract_retry_after(_http_error({"Retry-After": header}))

        self.assertIsNotNone(seconds)
        self.assertAlmostEqual(seconds, 20.0, delta=3.0)

    def test_a_date_already_passed_means_no_wait(self) -> None:
        past = datetime.now(timezone.utc) - timedelta(seconds=45)
        header = format_datetime(past, usegmt=True)

        self.assertIsNone(extract_retry_after(_http_error({"Retry-After": header})))

    def test_the_x_ratelimit_reset_headers_are_not_a_retry_interval(self) -> None:
        # 它们是「额度完全回满还要多久」，每个响应都带，当成重试间隔会把退避拉到几分钟。
        self.assertIsNone(extract_retry_after(_http_error({"x-ratelimit-reset-requests": "1m30s"})))
        self.assertIsNone(extract_retry_after(_http_error({"x-ratelimit-reset-tokens": "1.5s"})))

    def test_the_header_is_found_behind_an_exception_wrapper(self) -> None:
        # text_transport 会把 HTTP 错误包进自己的异常类型，头在 __cause__ 上。
        wrapper = RuntimeError("协议候选已耗尽。")
        wrapper.__cause__ = _http_error({"retry-after": "30"})

        self.assertEqual(extract_retry_after(wrapper), 30.0)

    def test_a_malicious_header_is_capped(self) -> None:
        self.assertEqual(
            extract_retry_after(_http_error({"retry-after": "99999"})),
            UPSTREAM_RETRY_AFTER_MAX_SECONDS,
        )
        # inf / nan 这种畸形数值同样不能进到 time.sleep 里。
        self.assertIsNone(extract_retry_after(_http_error({"retry-after": "inf"})))

    def test_a_response_without_the_header_means_no_wait(self) -> None:
        self.assertIsNone(extract_retry_after(_http_error({})))
        self.assertIsNone(extract_retry_after(_http_error({"retry-after": "soon"})))
        self.assertIsNone(extract_retry_after(RuntimeError("HTTP 429: Too Many Requests")))

    def test_the_at_minimum_wait_follows_upstreams_retry_after(self) -> None:
        delays: list[float] = []
        exc = _http_error({"retry-after": "25"})

        with patch("core.api_concurrency_control.MINIMUM_CAPACITY_BASE_DELAY", 0.01), \
             patch("core.api_concurrency_control.MINIMUM_CAPACITY_MAX_DELAY", 0.01), \
             patch("core.api_concurrency_control._interruptible_sleep", lambda delay, _stop: delays.append(delay)):
            self._hit_with(exc)

        self.assertEqual(len(delays), 1)
        self.assertGreaterEqual(delays[0], 25.0)

    def test_a_shorter_upstream_wait_never_shortens_the_local_backoff(self) -> None:
        delays: list[float] = []
        exc = _http_error({"retry-after": "1"})

        with patch("core.api_concurrency_control.MINIMUM_CAPACITY_BASE_DELAY", 30.0), \
             patch("core.api_concurrency_control.MINIMUM_CAPACITY_MAX_DELAY", 30.0), \
             patch("core.api_concurrency_control._interruptible_sleep", lambda delay, _stop: delays.append(delay)):
            self._hit_with(exc)

        self.assertEqual(len(delays), 1)
        self.assertGreater(delays[0], 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
