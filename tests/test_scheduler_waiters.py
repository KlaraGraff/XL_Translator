"""Waiters queued on the API schedulers must always be able to leave.

Two historical ways a waiter could hang forever: a weight clamped to the
capacity at entry time never fits again once 429 feedback shrinks the
capacity below it, and a FairApiGroupScheduler sibling thread loses the
shared per-task queue entry when another thread of the same task acquires.

A third historical gap: the group scheduler recorded ``waiting_recovery_count``
and ``active_recovery_weight`` but never read them, so recovery (retry /
review) threads on the production path had no priority over the same task's
main translation traffic.
"""

from __future__ import annotations

import threading
import time
import unittest

from core.api_scheduler import (
    API_CONCURRENCY_ACTION_REDUCED,
    API_REQUEST_CATEGORY_NORMAL,
    API_REQUEST_CATEGORY_RECOVERY,
    ApiSchedulerAcquireCancelled,
    WeightedApiScheduler,
)
from core.task_resources import FairApiGroupScheduler


class WeightedSchedulerReducedCapacityTests(unittest.TestCase):
    def test_a_waiter_sized_for_the_old_capacity_is_admitted_after_reduction(self) -> None:
        scheduler = WeightedApiScheduler(2)
        held = scheduler.acquire_lease(2)

        acquired: list[object] = []
        waiter = threading.Thread(
            target=lambda: acquired.append(scheduler.acquire_lease(2)),
            daemon=True,
        )
        waiter.start()
        time.sleep(0.1)

        decision = scheduler.register_concurrency_limit_hit(held.generation)
        self.assertEqual(decision.action, API_CONCURRENCY_ACTION_REDUCED)
        self.assertEqual(scheduler.capacity, 1)

        scheduler.release(held)
        waiter.join(timeout=5)
        # Without re-clamping, the waiter demands weight 2 from a capacity of
        # 1 forever and this join times out.
        self.assertFalse(waiter.is_alive())
        self.assertEqual(acquired[0].weight, 1)
        scheduler.release(acquired[0])


class FairSchedulerSiblingWaiterTests(unittest.TestCase):
    def test_sibling_waiters_of_one_task_all_get_their_slot(self) -> None:
        group = FairApiGroupScheduler()
        group.add_task("task-a", 1)
        first = group.acquire_lease("task-a", 1)

        finished: list[str] = []
        errors: list[BaseException] = []

        def _worker(name: str) -> None:
            try:
                lease = group.acquire_lease("task-a", 1)
                group.release(lease)
                finished.append(name)
            except BaseException as exc:  # noqa: BLE001 - collected for the assert
                errors.append(exc)

        siblings = [
            threading.Thread(target=_worker, args=(name,), daemon=True)
            for name in ("second", "third")
        ]
        for thread in siblings:
            thread.start()
        # Both siblings share one queue entry for "task-a"; they must be
        # waiting before the slot frees for the historical hang to matter.
        time.sleep(0.2)
        group.release(first)

        for thread in siblings:
            thread.join(timeout=5)
        # The thread that acquired first used to remove the shared entry,
        # leaving the other sibling waiting for a queue turn that never came.
        self.assertEqual(errors, [])
        self.assertFalse(any(thread.is_alive() for thread in siblings))
        self.assertEqual(sorted(finished), ["second", "third"])


class FairSchedulerRecoveryPriorityTests(unittest.TestCase):
    """Recovery work must get a slot while one task's normal threads are busy.

    ``TaskGroupScheduler`` forwards ``category=RECOVERY`` into
    ``FairApiGroupScheduler``, and that scheduler is what every runner gets in
    production.  Its admission check used to ignore the category entirely, so
    the recovery bookkeeping was dead code and review / retry threads within a
    task queued behind its own main translation traffic.
    """

    def test_recovery_gets_a_slot_despite_full_normal_load(self) -> None:
        group = FairApiGroupScheduler()
        group.add_task("task-a", 4)
        stop_normal = threading.Event()
        normal_acquired = threading.Semaphore(0)

        def _normal_worker() -> None:
            while not stop_normal.is_set():
                lease = group.acquire_lease(
                    "task-a", 1, category=API_REQUEST_CATEGORY_NORMAL
                )
                normal_acquired.release()
                time.sleep(0.02)
                group.release(lease, category=API_REQUEST_CATEGORY_NORMAL)

        normal_threads = [
            threading.Thread(target=_normal_worker, daemon=True) for _ in range(4)
        ]
        for thread in normal_threads:
            thread.start()
        self.addCleanup(stop_normal.set)
        for _ in range(4):
            self.assertTrue(
                normal_acquired.acquire(timeout=5), "普通请求没有把 4 个槽位占满"
            )

        deadline = time.monotonic() + 0.5
        started = time.monotonic()
        try:
            lease = group.acquire_lease(
                "task-a",
                1,
                category=API_REQUEST_CATEGORY_RECOVERY,
                should_stop=lambda: time.monotonic() > deadline,
            )
        except ApiSchedulerAcquireCancelled:
            self.fail(
                "恢复请求 500ms 内一个槽位都没拿到：FairApiGroupScheduler 忽略 request category，"
                "普通请求满载时 recovery 没有软上限保护（生产路径的恢复优先级是死代码）"
            )
        waited = time.monotonic() - started
        group.release(lease, category=API_REQUEST_CATEGORY_RECOVERY)
        stop_normal.set()
        for thread in normal_threads:
            thread.join(timeout=5)
        self.assertLess(waited, 0.3, f"恢复请求等待 {waited * 1000:.0f}ms 才拿到槽位")

    def test_normal_load_keeps_the_reserved_share_free_for_recovery(self) -> None:
        """The soft cap, not just the recovery request, must actually bind.

        Capacity 6 with the default 0.8 ratio leaves ``normal_soft_limit`` = 4.
        Four normal slots plus one recovery slot still fit the global budget,
        so only the soft cap stops a fifth normal request from taking the last
        free slot and starving recovery again.
        """
        group = FairApiGroupScheduler()
        group.add_task("task-a", 6)
        self.assertEqual(group.normal_soft_limit, 4)
        normal_leases = [
            group.acquire_lease("task-a", 1, category=API_REQUEST_CATEGORY_NORMAL)
            for _ in range(4)
        ]
        self.addCleanup(
            lambda: [
                group.release(lease, category=API_REQUEST_CATEGORY_NORMAL)
                for lease in normal_leases
            ]
        )
        recovery_lease = group.acquire_lease(
            "task-a", 1, category=API_REQUEST_CATEGORY_RECOVERY
        )
        self.addCleanup(
            lambda: group.release(recovery_lease, category=API_REQUEST_CATEGORY_RECOVERY)
        )

        deadline = time.monotonic() + 0.3
        with self.assertRaises(ApiSchedulerAcquireCancelled):
            group.acquire_lease(
                "task-a",
                1,
                category=API_REQUEST_CATEGORY_NORMAL,
                should_stop=lambda: time.monotonic() > deadline,
            )

    def test_normal_load_is_unrestricted_without_recovery_pressure(self) -> None:
        """The soft cap must not slow down a group that has no recovery work."""
        group = FairApiGroupScheduler()
        group.add_task("task-a", 6)
        leases = [
            group.acquire_lease("task-a", 1, category=API_REQUEST_CATEGORY_NORMAL)
            for _ in range(6)
        ]
        for lease in leases:
            group.release(lease, category=API_REQUEST_CATEGORY_NORMAL)
        self.assertEqual(group.snapshot().active_normal_weight, 0)
def _wait_until_queued(group: FairApiGroupScheduler, owner_key: str, *, timeout: float = 2.0) -> bool:
    """Wait until ``owner_key`` is really on the group's FIFO queue.

    只睡固定毫秒数在这里不够稳：机器慢一点，线程可能还没走到 ``acquire_lease``
    里排队，后面「它被卡住了」的断言就变成了在断言一个根本不存在的等待者。
    """
    with group._condition:  # noqa: SLF001 - this test pins the scheduler's own queue
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if owner_key in group._waiting_task_set:  # noqa: SLF001
                return True
            group._condition.wait(timeout=0.02)  # noqa: SLF001
    return False




if __name__ == "__main__":
    unittest.main(verbosity=2)
