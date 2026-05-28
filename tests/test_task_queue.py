import unittest

from core.api_scheduler import WeightedApiScheduler
from core.task_queue import (
    ApiConcurrencyGroupKey,
    ApiConcurrencyRequirement,
    TASK_STATUS_CANCELED,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_QUEUED,
    TASK_STATUS_RUNNING,
    TRANSLATION_TYPE_EXCEL,
    TRANSLATION_TYPE_PDF,
    TRANSLATION_TYPE_WORD,
    TranslationTask,
    TranslationTaskQueue,
    TranslationTaskSnapshot,
    is_api_group_blocking_error,
    mask_secret,
)


def _task(
    task_type: str,
    *,
    group: ApiConcurrencyGroupKey,
    concurrency: int,
    title: str | None = None,
) -> TranslationTask:
    return TranslationTask(
        snapshot=TranslationTaskSnapshot(
            title=title or task_type,
            translation_type=task_type,
            file_count=1,
            target_language="fr",
        ),
        group_requirements=(
            ApiConcurrencyRequirement(
                key=group,
                declared_concurrency=concurrency,
                provider="custom_openai",
                role="translation",
                role_label="翻译模型",
                key_fingerprint="sk-abcd...wxyz",
            ),
        ),
    )


class TranslationTaskQueueTests(unittest.TestCase):
    def test_capacity_uses_max_running_and_queued_tasks(self):
        group = ApiConcurrencyGroupKey("cloud", "https://api.example.com/v1", "hash")
        queue = TranslationTaskQueue()
        first = queue.arrange(_task(TRANSLATION_TYPE_EXCEL, group=group, concurrency=3))
        second = queue.arrange(_task(TRANSLATION_TYPE_PDF, group=group, concurrency=8))

        self.assertEqual(first.status, TASK_STATUS_QUEUED)
        self.assertEqual(second.status, TASK_STATUS_QUEUED)
        self.assertEqual(queue.scheduler_for(group).snapshot().capacity, 8)

        queue.cancel(second.task_id)
        self.assertEqual(queue.scheduler_for(group).snapshot().capacity, 3)

    def test_same_translation_type_does_not_start_twice(self):
        group = ApiConcurrencyGroupKey("cloud", "https://api.example.com/v1", "hash")
        queue = TranslationTaskQueue()
        first = queue.arrange(_task(TRANSLATION_TYPE_WORD, group=group, concurrency=5))
        second = queue.arrange(_task(TRANSLATION_TYPE_WORD, group=group, concurrency=5))

        started = queue.next_startable()
        self.assertEqual(started.task_id, first.task_id)
        self.assertEqual(started.status, TASK_STATUS_RUNNING)
        self.assertIsNone(queue.next_startable())

        queue.finish(first.task_id, TASK_STATUS_COMPLETED)
        next_started = queue.next_startable()
        self.assertEqual(next_started.task_id, second.task_id)

    def test_scheduler_skips_temporarily_unavailable_first_task(self):
        group = ApiConcurrencyGroupKey("cloud", "https://api.example.com/v1", "hash")
        other = ApiConcurrencyGroupKey("cloud", "https://other.example.com/v1", "hash")
        queue = TranslationTaskQueue()
        first = queue.arrange(_task(TRANSLATION_TYPE_PDF, group=group, concurrency=1))
        second = queue.arrange(_task(TRANSLATION_TYPE_EXCEL, group=other, concurrency=1))
        scheduler = queue.scheduler_for(group)
        lease = scheduler.acquire_lease(1)
        try:
            started = queue.next_startable()
        finally:
            scheduler.release(lease)

        self.assertEqual(started.task_id, second.task_id)
        self.assertEqual(queue.task(first.task_id).status, TASK_STATUS_QUEUED)

    def test_blocked_group_blocks_same_group_later_tasks(self):
        group = ApiConcurrencyGroupKey("cloud", "https://api.example.com/v1", "hash")
        other = ApiConcurrencyGroupKey("cloud", "https://other.example.com/v1", "hash")
        queue = TranslationTaskQueue()
        blocked = queue.arrange(_task(TRANSLATION_TYPE_PDF, group=group, concurrency=5))
        same_group = queue.arrange(_task(TRANSLATION_TYPE_EXCEL, group=group, concurrency=5))
        other_group = queue.arrange(_task(TRANSLATION_TYPE_WORD, group=other, concurrency=5))
        queue.block_groups([group], "凭据不可用")

        started = queue.next_startable()

        self.assertEqual(started.task_id, other_group.task_id)
        self.assertEqual(queue.task(blocked.task_id).block_reason, "凭据不可用")
        self.assertEqual(queue.task(same_group.task_id).block_reason, "凭据不可用")

    def test_cancel_moves_to_history_and_removes_active_count(self):
        group = ApiConcurrencyGroupKey("cloud", "https://api.example.com/v1", "hash")
        queue = TranslationTaskQueue()
        task = queue.arrange(_task(TRANSLATION_TYPE_EXCEL, group=group, concurrency=2))

        canceled = queue.cancel(task.task_id)

        self.assertEqual(canceled.status, TASK_STATUS_CANCELED)
        self.assertEqual(queue.active_count(TRANSLATION_TYPE_EXCEL), 0)
        self.assertEqual(len(queue.historical_tasks()), 1)

    def test_shared_scheduler_can_change_capacity(self):
        scheduler = WeightedApiScheduler(2)
        scheduler.set_capacity(5)

        self.assertEqual(scheduler.snapshot().capacity, 5)


class SecretMaskTests(unittest.TestCase):
    def test_mask_secret_keeps_leading_and_trailing_characters(self):
        self.assertEqual(mask_secret("sk-abcdef-wxyz"), "sk-a...wxyz")


class ApiGroupBlockingErrorTests(unittest.TestCase):
    def test_model_or_key_errors_block_api_group(self):
        self.assertTrue(is_api_group_blocking_error("invalid api key"))
        self.assertTrue(is_api_group_blocking_error("PDF 翻译模型配置不可用：模型名称不能为空"))

    def test_ordinary_file_errors_do_not_block_api_group(self):
        self.assertFalse(is_api_group_blocking_error("某个文件写入失败"))


if __name__ == "__main__":
    unittest.main()
