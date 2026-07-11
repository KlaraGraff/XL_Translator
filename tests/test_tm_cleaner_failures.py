from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from core.tm_cleaner import (
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
