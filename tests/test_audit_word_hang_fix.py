"""审计高-8：Word 恢复池两条永久挂死路径的回归测试。

两条路径都表现为 `wait_for_completion()` 永不返回（任务线程空转、UI 停在
「运行中」、finally 里的临时文件清理与线程池 shutdown 全部不执行）：

1. 停止落在两轮重试之间：`_schedule_retry_locked` 因停止标志不再排下一轮，
   而等待循环只看「全部完成」，`complete()` 永假。
2. 线程池任务抛异常：`_future_done` 吞掉异常却不复位 `retry_inflight` /
   `semantic_inflight`，同样让 `complete()` 永假。

对抗审查（WT1-M1 / WT1-M2）又实测复现了两条与线程池锁序有关的挂死，同样钉在
这里：

3. 取消回调里再排队（WT1-M1）：`shutdown(cancel_futures=True)` 的线程持着
   `ThreadPoolExecutor` 内部的 `_shutdown_lock` 逐个 `future.cancel()`，取消回调
   同线程走到兜底复位；一旦兜底复位又去 `executor.submit`，就是同一把非重入锁
   的二次获取——永久自锁。
4. `_condition` 与 `executor._shutdown_lock` 的 ABBA 环（WT1-M2）：worker 持
   `_condition` 去 submit（要 `_shutdown_lock`），停止线程持 `_shutdown_lock` 在
   取消回调里要 `_condition`，两边互等。

测试一律把 `wait_for_completion()` / `shutdown()` 放到子线程里跑，用超时判定
「有没有挂死」，钉的是「停止/异常/致命错误之后恢复池必须退出，并交回已经恢复的
译文」这一行为。
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.api_concurrency_control import ApiKeyTemporarilyUnavailableError  # noqa: E402
from core.word_task_runner import (  # noqa: E402
    _evaluate_word_translation,
    _WordRecoveryPool,
    _WordRecoveryState,
)
from engines.base_engine import TranslationEngine  # noqa: E402
from settings import WordBatchSettings  # noqa: E402


WAIT_TIMEOUT_SECONDS = 15.0


class _StubEngine(TranslationEngine):
    """只为满足构造参数存在；重试与仲裁在测试里都被 patch 掉。"""

    @property
    def engine_name(self) -> str:
        return "fake/audit-word-hang"

    def translate_batch(
        self,
        texts: list[str],
        target_lang: str,
        system_prompt: str,
        source_lang: str = "zh",
    ) -> dict[str, str]:
        return {text: text for text in texts}


class _ChatStubEngine(_StubEngine):
    """带 chat() 的引擎，否则恢复池不会启用语义仲裁。"""

    def chat(self, messages, **kwargs):  # pragma: no cover - 仲裁本身被 patch
        return ""


def _retry_settings() -> WordBatchSettings:
    settings = WordBatchSettings()
    settings.max_paragraphs_per_batch = 1
    return settings


def _validation_of(source: str, candidate: str):
    """按恢复池自己的口径评估一稿候选译文，取出其中的校验结果。

    直接手搓 TranslationValidationResult 容易和 `_semantic_candidate_is_eligible`
    的门槛对不上（用例会静默变成空跑），所以走真实评估函数。
    """
    return _evaluate_word_translation(
        source,
        candidate,
        source_lang="zh",
        target_lang="en",
        allow_recovery=True,
    ).validation


def _run_pool_with_timeout(pool: _WordRecoveryPool):
    """在子线程里等恢复池收尾，返回 (是否按时退出, outcome, 异常)。"""
    box: dict[str, object] = {}

    def _target() -> None:
        try:
            box["outcome"] = pool.wait_for_completion()
        except BaseException as exc:  # noqa: BLE001 - 测试要看到任何异常
            box["error"] = exc

    worker = threading.Thread(target=_target, daemon=True)
    worker.start()
    worker.join(WAIT_TIMEOUT_SECONDS)
    finished = not worker.is_alive()
    if not finished:
        # 挂死时把线程池强行关掉，避免拖累后续用例。关停自己也可能被同一处死锁
        # 卡住（WT1-M1 就是卡在 shutdown 的取消循环里），所以放进守护线程、只等
        # 一小会儿——否则「红」会变成整个 pytest 挂住，看不到失败信息。
        closer = threading.Thread(
            target=lambda: pool.shutdown(cancel_futures=True),
            daemon=True,
        )
        closer.start()
        closer.join(1.0)
    return finished, box.get("outcome"), box.get("error")


class WordRecoveryStopBetweenRoundsTest(unittest.TestCase):
    """路径一：停止落在恢复池两轮重试之间。"""

    def test_stop_between_retry_rounds_does_not_hang(self) -> None:
        source = "本条款的赔偿上限为合同总价的百分之十。"
        stop_flag = threading.Event()
        calls: list[int] = []

        def _fake_translate(texts, *args, **kwargs):
            calls.append(1)
            # 第一轮重试跑完立刻按下停止：停止正好落在两轮之间
            stop_flag.set()
            return {text: text for text in texts}

        pool = _WordRecoveryPool(
            engine=_StubEngine(),
            target_lang="fr",
            retry_prompt="retry",
            retry_batch_settings=_retry_settings(),
            retry_attempts=3,
            source_lang="zh",
            api_scheduler=None,
            concurrency=2,
            should_stop=stop_flag.is_set,
            enable_semantic=False,
        )
        with patch("core.word_task_runner.translate_word_texts", _fake_translate):
            pool.add_candidate(source, source)
            finished, outcome, error = _run_pool_with_timeout(pool)

        self.assertTrue(finished, "停止落在两轮重试之间时恢复池永久挂死")
        self.assertIsNone(error)
        self.assertIsNotNone(outcome)
        # 预算没跑完就停止：这一段算未恢复，交给人工复核通道
        self.assertEqual(outcome.unresolved_sources, [source])
        self.assertLess(len(calls), 3)

    def test_stop_still_returns_already_recovered_translations(self) -> None:
        """停止不等于丢结果：已经恢复的译文必须照常交回。"""
        recovered = "已经恢复的段落。"
        pending = "还没轮到的段落，赔偿上限为百分之十。"
        stop_flag = threading.Event()

        def _fake_translate(texts, *args, **kwargs):
            stop_flag.set()
            return {text: text for text in texts}

        pool = _WordRecoveryPool(
            engine=_StubEngine(),
            target_lang="fr",
            retry_prompt="retry",
            retry_batch_settings=_retry_settings(),
            retry_attempts=3,
            source_lang="zh",
            api_scheduler=None,
            concurrency=2,
            should_stop=stop_flag.is_set,
            enable_semantic=False,
        )
        with patch("core.word_task_runner.translate_word_texts", _fake_translate):
            # 第一段初稿就通过校验，直接被接受
            pool.add_candidate(recovered, "Le paragraphe a bien ete traduit ici.")
            pool.add_candidate(pending, pending)
            finished, outcome, error = _run_pool_with_timeout(pool)

        self.assertTrue(finished, "停止后恢复池永久挂死")
        self.assertIsNone(error)
        self.assertIn(recovered, outcome.accepted_translations)
        self.assertEqual(outcome.unresolved_sources, [pending])

    def test_stop_before_first_round_does_not_hang(self) -> None:
        """停止发生在第一轮排队之前（候选来自主翻译、停止随即按下）。"""
        source = "违约金按合同总价的百分之五计算。"
        stop_flag = threading.Event()

        pool = _WordRecoveryPool(
            engine=_StubEngine(),
            target_lang="fr",
            retry_prompt="retry",
            retry_batch_settings=_retry_settings(),
            retry_attempts=3,
            source_lang="zh",
            api_scheduler=None,
            concurrency=2,
            should_stop=stop_flag.is_set,
            enable_semantic=False,
        )

        def _fake_translate(texts, *args, **kwargs):  # pragma: no cover - 不该被调用
            raise AssertionError("停止后不应再发起重试请求")

        with patch("core.word_task_runner.translate_word_texts", _fake_translate):
            pool.add_candidate(source, source)
            stop_flag.set()
            finished, outcome, error = _run_pool_with_timeout(pool)

        self.assertTrue(finished, "停止后恢复池永久挂死")
        self.assertIsNone(error)
        self.assertEqual(outcome.unresolved_sources, [source])


class WordRecoveryFutureFailureTest(unittest.TestCase):
    """路径二：线程池任务抛异常后 inflight 标志不复位。"""

    def test_retry_worker_exception_does_not_hang(self) -> None:
        source = "赔偿上限为合同总价的百分之十。"
        logs: list[tuple[str, str]] = []

        def _boom(texts, *args, **kwargs):
            raise RuntimeError("上游接口炸了")

        pool = _WordRecoveryPool(
            engine=_StubEngine(),
            target_lang="fr",
            retry_prompt="retry",
            retry_batch_settings=_retry_settings(),
            retry_attempts=2,
            source_lang="zh",
            api_scheduler=None,
            concurrency=2,
            should_stop=lambda: False,
            log_callback=lambda level, msg: logs.append((level, msg)),
            enable_semantic=False,
        )
        with patch("core.word_task_runner.translate_word_texts", _boom):
            pool.add_candidate(source, source)
            finished, outcome, error = _run_pool_with_timeout(pool)

        self.assertTrue(finished, "重试任务抛异常后恢复池永久挂死")
        self.assertIsNone(error)
        self.assertEqual(outcome.unresolved_sources, [source])
        self.assertTrue(
            any(level == "WARN" for level, _ in logs),
            "重试任务失败必须留下一条可见日志",
        )

    def test_retry_worker_exception_keeps_remaining_attempt_budget(self) -> None:
        """一轮崩掉不该吞掉剩余轮次：下一轮成功仍要恢复译文。"""
        source = "赔偿上限为合同总价的百分之十。"
        calls: list[int] = []

        def _boom_then_ok(texts, *args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("第一轮上游抖动")
            return {text: "The compensation cap is ten percent of the price." for text in texts}

        pool = _WordRecoveryPool(
            engine=_StubEngine(),
            target_lang="en",
            retry_prompt="retry",
            retry_batch_settings=_retry_settings(),
            retry_attempts=3,
            source_lang="zh",
            api_scheduler=None,
            concurrency=2,
            should_stop=lambda: False,
            enable_semantic=False,
        )
        with patch("core.word_task_runner.translate_word_texts", _boom_then_ok):
            pool.add_candidate(source, source)
            finished, outcome, error = _run_pool_with_timeout(pool)

        self.assertTrue(finished, "重试任务抛异常后恢复池永久挂死")
        self.assertIsNone(error)
        self.assertEqual(outcome.fixed_sources, [source])
        self.assertEqual(len(calls), 2)

    def test_semantic_worker_exception_does_not_hang(self) -> None:
        source = "增设墙体厚度300mm，并按施工图纸执行。"
        candidate = "The wall shall be built according to the drawings."

        def _fake_translate(texts, *args, **kwargs):
            return {text: text for text in texts}

        arbitration_calls: list[int] = []

        def _boom(*args, **kwargs):
            arbitration_calls.append(1)
            raise RuntimeError("仲裁请求炸了")

        pool = _WordRecoveryPool(
            engine=_ChatStubEngine(),
            target_lang="en",
            retry_prompt="retry",
            retry_batch_settings=_retry_settings(),
            retry_attempts=1,
            source_lang="zh",
            api_scheduler=None,
            concurrency=2,
            should_stop=lambda: False,
            enable_semantic=True,
        )
        with patch("core.word_task_runner.translate_word_texts", _fake_translate), patch(
            "core.word_task_runner._run_semantic_arbitration", _boom
        ):
            pool.add_candidate(source, candidate)
            finished, outcome, error = _run_pool_with_timeout(pool)

        self.assertTrue(finished, "语义仲裁任务抛异常后恢复池永久挂死")
        self.assertIsNone(error)
        self.assertEqual(outcome.unresolved_sources, [source])
        self.assertTrue(arbitration_calls, "这条用例必须真的排出一次语义仲裁才有意义")

    def test_pool_executor_is_shut_down_after_stop(self) -> None:
        """挂死的连带后果：线程池不关停。停止后必须已经关停。"""
        source = "赔偿上限为合同总价的百分之十。"
        stop_flag = threading.Event()

        def _fake_translate(texts, *args, **kwargs):
            stop_flag.set()
            return {text: text for text in texts}

        pool = _WordRecoveryPool(
            engine=_StubEngine(),
            target_lang="fr",
            retry_prompt="retry",
            retry_batch_settings=_retry_settings(),
            retry_attempts=3,
            source_lang="zh",
            api_scheduler=None,
            concurrency=2,
            should_stop=stop_flag.is_set,
            enable_semantic=False,
        )
        with patch("core.word_task_runner.translate_word_texts", _fake_translate):
            pool.add_candidate(source, source)
            finished, _outcome, error = _run_pool_with_timeout(pool)

        self.assertTrue(finished, "停止后恢复池永久挂死")
        self.assertIsNone(error)
        self.assertTrue(pool._executor_shutdown, "恢复池退出后线程池必须已经关停")


class WordRecoveryStopBudgetTest(unittest.TestCase):
    """单独钉住「停止即把剩余重试预算记满」这半边。

    等待循环的停止出口只是第二道保险；真正让这一段 `complete()` 成立的是
    `_schedule_retry_locked` 停止时把 `attempts_done` 记满。少了它，这一段永远
    差着轮次，任何按「全部完成」判定的调用方都会退回死循环。
    """

    def _pool(self, stop_flag: threading.Event) -> _WordRecoveryPool:
        return _WordRecoveryPool(
            engine=_ChatStubEngine(),
            target_lang="fr",
            retry_prompt="retry",
            retry_batch_settings=_retry_settings(),
            retry_attempts=3,
            source_lang="zh",
            api_scheduler=None,
            concurrency=2,
            should_stop=stop_flag.is_set,
            enable_semantic=True,
        )

    def test_stop_marks_remaining_retry_budget_exhausted(self) -> None:
        source = "赔偿上限为合同总价的百分之十。"
        stop_flag = threading.Event()
        pool = self._pool(stop_flag)
        self.addCleanup(pool.shutdown, cancel_futures=True)

        state = _WordRecoveryState(source=source)
        with pool._condition:
            pool._states[source] = state
            stop_flag.set()
            pool._schedule_retry_locked(state)

            self.assertFalse(state.retry_inflight, "停止后不该把这一段挂在「重试中」")
            self.assertEqual(state.attempts_done, 3, "停止后剩余重试预算必须就地记满")
            self.assertTrue(state.complete(3))
            self.assertTrue(
                pool._all_complete_locked(),
                "停止后 _all_complete_locked() 仍为假 —— 等待循环会退回死循环",
            )

    def test_stop_does_not_schedule_new_semantic_arbitration(self) -> None:
        """停止后不该再花钱发语义仲裁请求（审查 WT1-M2 顺带指出）。"""
        source = "增设墙体厚度300mm，并按施工图纸执行。"
        candidate = "The wall shall be built according to the drawings."
        stop_flag = threading.Event()
        pool = self._pool(stop_flag)
        self.addCleanup(pool.shutdown, cancel_futures=True)

        state = _WordRecoveryState(source=source)
        validation = _validation_of(source, candidate)
        with pool._condition:
            pool._states[source] = state
            stop_flag.set()
            pool._schedule_semantic_locked(state, candidate, validation)

        self.assertEqual(state.semantic_inflight, 0, "停止后不该再排语义仲裁")
        self.assertEqual(
            len(pool._futures), 0, "停止后不该再往线程池里塞任何仲裁任务"
        )


class WordRecoveryFatalErrorTest(unittest.TestCase):
    """审查 WT1-M1：致命错误发生时队列里还留着没开跑的重试任务。

    `wait_for_completion` 会在这条路径上走 `shutdown(cancel_futures=True)`。
    取消回调是由「持着 ThreadPoolExecutor._shutdown_lock 的那个线程」同步调用
    的，兜底复位若在这里再往线程池排队，就是同一把非重入锁的二次获取。
    """

    def test_fatal_error_with_queued_retries_does_not_hang(self) -> None:
        sources = [f"第{index}条：赔偿上限为合同总价的百分之十。" for index in range(8)]
        first_call = threading.Event()

        def _fake_translate(texts, *args, **kwargs):
            if not first_call.is_set():
                first_call.set()
                raise ApiKeyTemporarilyUnavailableError("密钥临时不可用")
            # 后续 worker 占住唯一的线程，保证取消循环启动时队列里确实还有
            # 没开跑的重试任务可取消（生产上「段落数远多于并发」恒成立）。
            time.sleep(1.5)
            return {text: text for text in texts}

        pool = _WordRecoveryPool(
            engine=_StubEngine(),
            target_lang="fr",
            retry_prompt="retry",
            retry_batch_settings=_retry_settings(),
            retry_attempts=3,
            source_lang="zh",
            api_scheduler=None,
            concurrency=1,
            should_stop=lambda: False,
            enable_semantic=False,
            defer_until_started=True,
        )
        with patch("core.word_task_runner.translate_word_texts", _fake_translate):
            for source in sources:
                pool.add_candidate(source, source)
            finished, _outcome, error = _run_pool_with_timeout(pool)

        self.assertTrue(
            finished,
            "致命错误 + 队列里有未开跑的重试任务：恢复池在取消回调里自锁，永久挂死",
        )
        self.assertIsInstance(
            error,
            ApiKeyTemporarilyUnavailableError,
            "致命错误必须原样抛给上层，而不是被挂死或吞掉",
        )


class WordRecoveryShutdownDeadlockTest(unittest.TestCase):
    """审查 WT1-M2：`_condition` 与 `executor._shutdown_lock` 的 ABBA 环。

    交错靠注入延时强制放大（只放大窗口，不改变锁序）：让 worker 停在
    `executor.submit` 上，同时让主线程进入 `shutdown(cancel_futures=True)` 的取消
    循环。锁序只要允许「持 `_condition` 去 submit」，两边就互等。
    """

    def test_shutdown_while_worker_submits_does_not_deadlock(self) -> None:
        sources = [f"第{index}条：赔偿上限为合同总价的百分之十。" for index in range(8)]
        in_submit = threading.Event()
        hold = threading.Event()

        def _fake_translate(texts, *args, **kwargs):
            # 原样返回中文 → 校验不过 → worker 会继续排下一轮，从而调 submit
            return {text: text for text in texts}

        pool = _WordRecoveryPool(
            engine=_StubEngine(),
            target_lang="fr",
            retry_prompt="retry",
            retry_batch_settings=_retry_settings(),
            retry_attempts=5,
            source_lang="zh",
            api_scheduler=None,
            concurrency=1,
            should_stop=lambda: False,
            enable_semantic=False,
            defer_until_started=True,
        )
        self.addCleanup(hold.set)

        real_submit = pool._executor.submit
        submit_count = {"n": 0}

        def _slow_submit(fn, *args, **kwargs):
            submit_count["n"] += 1
            # 只卡 worker 线程里「第一轮之后」的那次排队
            if (
                threading.current_thread().name.startswith("ThreadPoolExecutor")
                and submit_count["n"] > len(sources)
            ):
                in_submit.set()
                hold.wait(5.0)
            return real_submit(fn, *args, **kwargs)

        pool._executor.submit = _slow_submit

        with patch("core.word_task_runner.translate_word_texts", _fake_translate):
            for source in sources:
                pool.add_candidate(source, source)
            pool.start()
            self.assertTrue(
                in_submit.wait(10.0),
                "没能造出「worker 正在排队」的交错，这条用例就是空跑",
            )
            closer = threading.Thread(
                target=lambda: pool.shutdown(cancel_futures=True),
                daemon=True,
            )
            closer.start()
            # 让关停线程真正进到 Executor.shutdown 的取消循环里
            time.sleep(0.5)
            hold.set()
            closer.join(WAIT_TIMEOUT_SECONDS)

        self.assertFalse(
            closer.is_alive(),
            "停止时 worker 正在排队：_condition 与 executor._shutdown_lock 形成 ABBA 死锁",
        )


if __name__ == "__main__":
    unittest.main()
