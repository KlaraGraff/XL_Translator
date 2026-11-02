"""「上游说并发太多」时怎么降速：两条调度路径共用 decide_concurrency_limit_hit。

以前 WeightedApiScheduler 和 core.task_resources 的连接组调度器各写一份同样的判断，
改一边容易忘另一边。这里直接测共用的判断，再确认两边都按它改自己的容量。
"""

from __future__ import annotations

from core.api_scheduler import (
    API_CONCURRENCY_ACTION_REDUCED,
    API_CONCURRENCY_ACTION_RETRY_CURRENT,
    API_CONCURRENCY_ACTION_UNAVAILABLE,
    WeightedApiScheduler,
    build_adaptive_capacity_levels,
    decide_concurrency_limit_hit,
)
from core.task_resources import FairApiGroupScheduler


def _decide(capacity: int, minimum: int, generation: int, request_generation: int | None):
    return decide_concurrency_limit_hit(
        capacity=capacity,
        minimum_capacity=minimum,
        levels=build_adaptive_capacity_levels(10),
        generation=generation,
        request_generation=request_generation,
    )


def test_a_signal_from_an_older_burst_only_retries() -> None:
    decision = _decide(10, 2, generation=3, request_generation=2)
    assert decision.action == API_CONCURRENCY_ACTION_RETRY_CURRENT
    assert (decision.current_capacity, decision.generation) == (10, 3)


def test_at_the_minimum_the_connection_is_unavailable() -> None:
    decision = _decide(2, 2, generation=0, request_generation=0)
    assert decision.action == API_CONCURRENCY_ACTION_UNAVAILABLE
    assert decision.current_capacity == 2


def test_otherwise_it_steps_down_one_level_and_starts_a_new_generation() -> None:
    decision = _decide(10, 2, generation=0, request_generation=None)
    assert decision.action == API_CONCURRENCY_ACTION_REDUCED
    assert (decision.previous_capacity, decision.current_capacity, decision.generation) == (10, 8, 1)


def test_both_schedulers_apply_the_shared_decision() -> None:
    weighted = WeightedApiScheduler(10)
    weighted_decision = weighted.register_concurrency_limit_hit(None)
    assert weighted_decision.action == API_CONCURRENCY_ACTION_REDUCED
    assert weighted.capacity == weighted_decision.current_capacity == 8

    group = FairApiGroupScheduler()
    group.add_task("task-a", 10)
    group_decision = group.register_concurrency_limit_hit(None)
    assert group_decision.action == API_CONCURRENCY_ACTION_REDUCED
    assert group.capacity == group_decision.current_capacity == 8

    # 降速之前发出的请求再报一次「太多」：两边都只重试，不再降。
    stale_weighted = weighted_decision.generation - 1
    stale_group = group_decision.generation - 1
    assert weighted.register_concurrency_limit_hit(stale_weighted).action == API_CONCURRENCY_ACTION_RETRY_CURRENT
    assert group.register_concurrency_limit_hit(stale_group).action == API_CONCURRENCY_ACTION_RETRY_CURRENT
    assert (weighted.capacity, group.capacity) == (8, 8)
