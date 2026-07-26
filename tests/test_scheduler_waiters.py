"""Waiters queued on the API schedulers must always be able to leave.

Two historical ways a waiter could hang forever: a weight clamped to the
capacity at entry time never fits again once 429 feedback shrinks the
capacity below it, and a FairApiGroupScheduler sibling thread loses the
shared per-task queue entry when another thread of the same task acquires.
"""

from __future__ import annotations

import threading
import time
import unittest

from core.api_scheduler import (
    API_CONCURRENCY_ACTION_REDUCED,
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
