from __future__ import annotations

import asyncio
import threading
import time
import unittest
from unittest.mock import patch

from core.api_scheduler import WeightedApiScheduler
from core.tm_cleaner import (
    CleanSuggestion,
    TmCleaningBatchError,
    _clean_async_impl,
    _run_cleaning_threaded,
)


class _Engine:
    engine_name = "fake-cloud"


class TmCleanerFailureTests(unittest.TestCase):
    def test_threaded_batch_failure_is_not_reported_as_completed(self) -> None:
        batches = [[{"id": 1}], [{"id": 2}]]
        progress: list[dict] = []

        with patch(
            "core.tm_cleaner._clean_batch_sync",
            side_effect=[[], RuntimeError("provider unavailable")],
        ):
            with self.assertRaisesRegex(TmCleaningBatchError, r"有 1 批未成功（共 2 批）"):
                _run_cleaning_threaded(
                    batches,
                    _Engine(),
                    progress.append,
                    concurrency=1,
                )

        self.assertNotEqual(progress[-1]["stage"], "completed")

    def test_async_batch_failure_is_not_reported_as_completed(self) -> None:
        batches = [[{"id": 1}], [{"id": 2}]]
        progress: list[dict] = []

        async def fake_clean(batch, _engine, _prompt):
            if batch[0]["id"] == 2:
                raise RuntimeError("provider unavailable")
            return []

        with patch("core.tm_cleaner._clean_batch_async", side_effect=fake_clean):
            with self.assertRaisesRegex(TmCleaningBatchError, r"有 1 批未成功（共 2 批）"):
                asyncio.run(
                    _clean_async_impl(
                        batches,
                        _Engine(),
                        progress.append,
                    )
                )

        self.assertNotEqual(progress[-1]["stage"], "completed")

    # 审计 高-11：一个批次失败时，其余批次已付费拿到的建议必须先落库再报错。
    def test_threaded_failure_keeps_suggestions_from_successful_batches(self) -> None:
        paid = CleanSuggestion(entry_id=1, source_text="源", old_target="old", new_target="new")
        persisted: list[list[dict]] = []

        with patch(
            "core.tm_cleaner._clean_batch_sync",
            side_effect=[[paid], RuntimeError("provider unavailable")],
        ), patch("core.tm_cleaner.tm_manager.persist_cleaning_suggestions", side_effect=persisted.append):
            with self.assertRaises(TmCleaningBatchError) as ctx:
                _run_cleaning_threaded([[{"id": 1}], [{"id": 2}]], _Engine(), lambda _p: None, concurrency=1)

        self.assertEqual([row["entry_id"] for row in persisted[-1]], [1])
        self.assertEqual(ctx.exception.model_suggestions, [paid])

    def test_async_failure_keeps_suggestions_from_successful_batches(self) -> None:
        paid = CleanSuggestion(entry_id=1, source_text="源", old_target="old", new_target="new")
        persisted: list[list[dict]] = []

        async def fake_clean(batch, _engine, _prompt):
            if batch[0]["id"] == 2:
                raise RuntimeError("provider unavailable")
            return [paid]

        with patch("core.tm_cleaner._clean_batch_async", side_effect=fake_clean), patch(
            "core.tm_cleaner.tm_manager.persist_cleaning_suggestions", side_effect=persisted.append
        ):
            with self.assertRaises(TmCleaningBatchError) as ctx:
                asyncio.run(_clean_async_impl([[{"id": 1}], [{"id": 2}]], _Engine(), lambda _p: None))

        self.assertEqual([row["entry_id"] for row in persisted[-1]], [1])
        self.assertEqual(ctx.exception.model_suggestions, [paid])

    # 审计 中-4：限流退避最长要睡 30 秒，停止标志必须交给退避本身，
    # 否则用户点了停止还要先把这一觉睡完才回来。
    def test_rate_limit_backoff_receives_the_cancel_event(self) -> None:
        scheduler = WeightedApiScheduler(1)  # 已在最低档，直接进宽限等待
        cancel_event = threading.Event()
        seen: list = []

        def _record_sleep(delay: float, should_stop) -> None:  # noqa: ANN001
            seen.append(should_stop)
            cancel_event.set()  # 用户在这一觉里按了停止
            time.sleep(0)

        with patch(
            "core.api_concurrency_control._interruptible_sleep", _record_sleep
        ), patch(
            "core.tm_cleaner._clean_batch_sync",
            side_effect=RuntimeError("HTTP 429: Too Many Requests"),
        ):
            with self.assertRaises(TmCleaningBatchError):
                _run_cleaning_threaded(
                    [[{"id": 1}]],
                    _Engine(),
                    lambda _p: None,
                    concurrency=1,
                    cancel_event=cancel_event,
                    api_scheduler=scheduler,
                )

        self.assertTrue(seen, "限流退避根本没有被调用，测试前提不成立")
        self.assertIsNotNone(seen[0], "限流退避没拿到停止标志，点了停止仍会睡满")
        self.assertTrue(seen[0](), "停止标志交给退避后必须能读到已停止")


if __name__ == "__main__":
    unittest.main(verbosity=2)
