"""同时发出几条互不依赖的模型请求（语言识别、文档说明），结果按输入顺序收回。

每个文件的语言识别、每份 PDF 的文档说明都是一次独立的模型请求，前后没有
依赖。逐个等的话，十个文件就是十倍的等待；这里按页面的并发设置同时发出。
"""
from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import TypeVar

T = TypeVar("T")
R = TypeVar("R")


def map_concurrently(
    fn: Callable[[T], R],
    items: Sequence[T],
    *,
    max_workers: int,
    on_done: Callable[[int, int], None] | None = None,
) -> list[R]:
    """对每个 item 调一次 ``fn``，最多 ``max_workers`` 个同时进行；结果按输入顺序返回。

    ``on_done(index, done)`` 在每个 item 返回后调用一次（``done`` 是已返回的个数，
    只增不减），回调之间不会交错。``fn`` 抛出的异常在全部结束后原样抛出。
    """
    items = list(items)
    workers = min(max(1, int(max_workers or 1)), len(items))
    if workers <= 1:
        results = []
        for index, item in enumerate(items):
            results.append(fn(item))
            if on_done is not None:
                on_done(index, index + 1)
        return results

    lock = threading.Lock()
    done = 0

    def run(index: int, item: T) -> R:
        nonlocal done
        value = fn(item)
        with lock:
            done += 1
            if on_done is not None:
                on_done(index, done)
        return value

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="model-call") as executor:
        futures = [executor.submit(run, index, item) for index, item in enumerate(items)]
        return [future.result() for future in futures]


def run_in_background(fn: Callable[[], R], *, name: str) -> Future[R]:
    """在后台守护线程里跑 ``fn``，立刻返回它的 Future（任务退出时不用等它）。"""
    future: Future[R] = Future()

    def run() -> None:
        if not future.set_running_or_notify_cancel():
            return
        try:
            future.set_result(fn())
        except BaseException as exc:  # noqa: BLE001 - handed to whoever waits on the future
            future.set_exception(exc)

    threading.Thread(target=run, name=name, daemon=True).start()
    return future


def wait_result(future: Future[R], *, should_stop: Callable[[], bool], default: R) -> R:
    """等 Future 的结果；中途要求停止就不再等，返回 ``default``。"""
    while True:
        try:
            return future.result(timeout=0.2)
        except FutureTimeout:
            if should_stop():
                return default
