from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

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
            with self.assertRaisesRegex(TmCleaningBatchError, "1/2 个清洗批次失败"):
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
            with self.assertRaisesRegex(TmCleaningBatchError, "1/2 个清洗批次失败"):
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
