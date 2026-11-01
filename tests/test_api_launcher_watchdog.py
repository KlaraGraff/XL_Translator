"""看门狗：收尾预算必须包住内层那条串行链，而且要等它真的收完再走。

对应 docs/CODE_AUDIT_2026-08-31.md 中-5。壳被强杀（Force Quit、Tauri 崩溃）
时 Rust 侧没机会发 SIGTERM，收尾全靠 ``api/launcher.py`` 的 parent watchdog：
发现父进程没了 → ``begin_shutdown`` 置停止标志 → ``server.should_exit = True``
→ 然后等内层链走完。

这条链是**串行叠加**的，三段首尾相接：① uvicorn 排空连接
``GRACEFUL_SHUTDOWN_SECONDS`` ≤ 10s；② 连接关完之后 lifespan 才调
``task_manager.shutdown()``（默认 timeout 12s），runner 的 finally 就在这一段里
删 LibreOffice profile、``word_translator_temp``、PDF 分页工作区；③ 之后才轮到
``mark_active_tasks_interrupted()`` + ``flush_history()`` 落盘。

上一版看门狗把强杀预算写死成 20s，而且到点无条件 ``os._exit``，既不跟内层一起
改，也不看收尾完没完——20 < 10 + 12，强退时 runner 的 finally 被砍在半路，
soffice 被 reparent 到 launchd 长期占着 UNO 端口。这里锁两条：预算用加法从内层
算出来，且循环轮询收尾是否结束、结束就立刻走。
"""

from __future__ import annotations

import ast
import os
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER_PATH = REPO_ROOT / "api" / "launcher.py"
TASK_MANAGER_PATH = REPO_ROOT / "api" / "task_manager.py"

WATCHDOG_THREAD_NAME = "translator-parent-watchdog"


def _load_launcher():
    """Import ``api.launcher``, standing in for ``api.app`` if need be.

    ``api.app`` → ``core.config_crypto`` needs ``core._embedded_key``; a
    developer checkout ships that file as a placeholder (the real key is
    injected only in release builds), so importing the launcher can fail for a
    reason that has nothing to do with the watchdog.  These tests only exercise
    ``api/launcher.py`` itself, so a minimal ``create_app`` stand-in keeps them
    runnable — and in a normal checkout the real module is used.
    """
    try:
        import api.launcher as launcher
    except ImportError:
        stand_in = types.ModuleType("api.app")
        stand_in.create_app = lambda **_kwargs: SimpleNamespace(state=SimpleNamespace())
        sys.modules["api.app"] = stand_in
        import api.launcher as launcher
    return launcher


def _eval_constant(node: ast.expr, env: dict[str, float]):
    """Evaluate a module-level numeric constant, following ``Name`` references."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in env:
            raise KeyError(f"无法解析常量 {node.id}")
        return env[node.id]
    if isinstance(node, ast.BinOp):
        left = _eval_constant(node.left, env)
        right = _eval_constant(node.right, env)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
    raise ValueError(f"不是可求值的数字常量：{ast.dump(node)}")


def _module_numbers(path: Path) -> dict[str, float]:
    """Read the module-level numeric constants out of a source file.

    Read from the source rather than ``import``-ed: the budget invariants are
    about the values written in the file, and the launcher must stay checkable
    even where the developer key stub blocks the import.
    """
    env: dict[str, float] = {}
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if not isinstance(node, ast.Assign):
            continue
        try:
            value = _eval_constant(node.value, env)
        except (KeyError, ValueError):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                env[target.id] = float(value)
    return env


def _shutdown_default_timeout(path: Path) -> float:
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.FunctionDef) or node.name != "shutdown":
            continue
        args = node.args
        positional = args.args[len(args.args) - len(args.defaults):]
        defaults = dict(zip([arg.arg for arg in positional], args.defaults))
        keyword = {
            key.arg: value
            for key, value in zip(args.kwonlyargs, args.kw_defaults)
            if value is not None
        }
        default = keyword.get("timeout", defaults.get("timeout"))
        if default is not None:
            return float(ast.literal_eval(default))
    raise AssertionError("api/task_manager.py 里找不到 shutdown(timeout=...) 的默认值")


class WatchdogBudgetTests(unittest.TestCase):
    """强杀预算的内外不等式（和 src-tauri/src/main.rs 的镜像断言同源）。"""

    def test_force_exit_budget_covers_the_inner_shutdown_chain(self) -> None:
        constants = _module_numbers(LAUNCHER_PATH)
        drain = constants["GRACEFUL_SHUTDOWN_SECONDS"]
        watchdog = constants["WATCHDOG_FORCE_EXIT_SECONDS"]
        unwind = _shutdown_default_timeout(TASK_MANAGER_PATH)
        inner_chain = drain + unwind

        self.assertGreaterEqual(
            watchdog,
            inner_chain,
            f"看门狗强杀预算 {watchdog}s 包不住内层收尾链 "
            f"{drain}s（uvicorn 排空）+ {unwind}s（task_manager.shutdown）= {inner_chain}s；"
            "强退时 runner 的 finally（删 LibreOffice profile / word_translator_temp / "
            "PDF 分页工作区）会被 os._exit 砍在半路。",
        )

    def test_force_exit_budget_keeps_a_margin_for_the_bookkeeping_step(self) -> None:
        # ③ mark_active_tasks_interrupted + flush_history 发生在 ② 之后，Python
        # 侧没有自己的超时；余量必须非零，否则两个 deadline 又会贴在一起。
        constants = _module_numbers(LAUNCHER_PATH)
        inner_chain = constants["GRACEFUL_SHUTDOWN_SECONDS"] + _shutdown_default_timeout(TASK_MANAGER_PATH)

        self.assertGreater(
            constants["WATCHDOG_FORCE_EXIT_SECONDS"],
            inner_chain,
            "看门狗预算必须严格大于内层链条，不能只是「对齐」",
        )
        self.assertGreaterEqual(
            constants["WATCHDOG_FORCE_EXIT_SECONDS"] - inner_chain,
            constants["SHUTDOWN_MARGIN_SECONDS"],
            "给 mark_active_tasks_interrupted + flush_history 留的余量被吃掉了",
        )

    def test_force_exit_budget_is_the_sum_of_its_parts(self) -> None:
        # 和 Rust 侧 SIDECAR_STOP_TIMEOUT 同源：谁动了任何一段，总额自己跟着走，
        # 而不是又退化成「写死的二十来秒，跟内层没关系」。
        constants = _module_numbers(LAUNCHER_PATH)
        expected = (
            constants["GRACEFUL_SHUTDOWN_SECONDS"]
            + _shutdown_default_timeout(TASK_MANAGER_PATH)
            + constants["SHUTDOWN_MARGIN_SECONDS"]
        )

        self.assertEqual(
            constants["WATCHDOG_FORCE_EXIT_SECONDS"],
            expected,
            "看门狗预算必须等于 ① 排空 + ② unwind + ③ 余量，和 src-tauri/src/main.rs 的 "
            "SIDECAR_STOP_TIMEOUT 保持同一个加法",
        )
        self.assertEqual(
            constants["TASK_UNWIND_SECONDS"],
            _shutdown_default_timeout(TASK_MANAGER_PATH),
            "TASK_UNWIND_SECONDS 是 task_manager.shutdown(timeout=) 的镜像，两边必须一起改",
        )


class WatchdogExitTests(unittest.TestCase):
    """看门狗要轮询收尾有没有结束，收完了立刻走，不再睡满整个预算。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.launcher = _load_launcher()

    def test_wait_for_shutdown_returns_as_soon_as_the_unwind_finishes(self) -> None:
        stopped = threading.Event()
        threading.Timer(0.2, stopped.set).start()

        started = time.monotonic()
        finished = self.launcher._wait_for_shutdown(stopped, 30.0)
        elapsed = time.monotonic() - started

        self.assertTrue(finished, "收尾已经结束，_wait_for_shutdown 应当立刻返回 True")
        self.assertLess(elapsed, 5.0, f"收尾结束后又多等了 {elapsed:.1f}s")

    def test_wait_for_shutdown_gives_up_at_the_deadline(self) -> None:
        stopped = threading.Event()

        started = time.monotonic()
        finished = self.launcher._wait_for_shutdown(stopped, 0.3)
        elapsed = time.monotonic() - started

        self.assertFalse(finished, "收尾没有结束，_wait_for_shutdown 应当返回 False")
        self.assertGreaterEqual(elapsed, 0.3)
        self.assertLess(elapsed, 5.0, f"越过 deadline 之后还多等了 {elapsed:.1f}s")

    def test_a_wedged_shutdown_still_force_exits_at_the_budget(self) -> None:
        # 另一头同样要守住：收尾真的卡死时不能用「等它做完」把自己也拖住。
        self._watch_and_wait(budget=0.4, stop_after=None, max_wait=10.0)

    def test_watchdog_is_not_armed_without_a_parent_pid(self) -> None:
        # 从命令行/MCP 自己起的引擎没有壳，不该有看门狗。
        env = {key: value for key, value in os.environ.items() if key != "TRANSLATOR_SIDECAR_PARENT_PID"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(self.launcher.os, "_exit") as exit_call,
        ):
            self.launcher._start_parent_watchdog(SimpleNamespace(should_exit=False), lambda: None, threading.Event())

        self.assertEqual(
            [t for t in threading.enumerate() if t.name == WATCHDOG_THREAD_NAME],
            [],
            "没有 TRANSLATOR_SIDECAR_PARENT_PID 时不该起看门狗线程",
        )
        exit_call.assert_not_called()

    def _watch_and_wait(self, *, budget: float, stop_after: float | None, max_wait: float):
        stopped = threading.Event()
        server = SimpleNamespace(should_exit=False)
        exited = threading.Event()
        exit_args: list[tuple] = []

        def fake_exit(*args) -> None:
            exit_args.append(args)
            exited.set()

        with (
            patch.dict(os.environ, {"TRANSLATOR_SIDECAR_PARENT_PID": str(os.getpid() + 1)}),
            patch.object(self.launcher, "_parent_process_is_alive", return_value=False),
            patch.object(self.launcher, "WATCHDOG_FORCE_EXIT_SECONDS", budget),
            patch.object(self.launcher, "end_child_processes"),
            patch.object(self.launcher.os, "_exit", side_effect=fake_exit),
        ):
            started = time.monotonic()
            self.launcher._start_parent_watchdog(server, lambda: None, stopped)
            if stop_after is not None:
                threading.Timer(stop_after, stopped.set).start()

            deadline = time.monotonic() + max_wait
            while not exited.is_set() and time.monotonic() < deadline:
                time.sleep(0.02)
            elapsed = time.monotonic() - started

        return server, exited, exit_args, elapsed

    def test_watchdog_leaves_right_after_the_unwind_instead_of_sleeping_out_the_budget(self) -> None:
        # 预算故意放大到 30s：只有「轮询到收尾结束就立刻走」才能在几百毫秒内退出，
        # 旧实现会一直睡到 deadline，这个用例会超时。
        server, exited, exit_args, elapsed = self._watch_and_wait(
            budget=30.0, stop_after=0.3, max_wait=10.0
        )

        self.assertTrue(
            exited.is_set(),
            f"收尾早就结束了，看门狗 {elapsed:.1f}s 还没退出——它没有等收尾做完，而是在睡满预算",
        )
        self.assertLess(elapsed, 10.0, f"看门狗用了 {elapsed:.1f}s 才走，说明它并不观察收尾是否结束")
        self.assertTrue(server.should_exit, "看门狗必须先让 uvicorn 开始收尾再等它")
        self.assertEqual(exit_args, [(0,)], "看门狗最终仍要以 0 退出")

    def tearDown(self) -> None:
        # 被 patch 住 os._exit 之后线程会正常返回，这里只等到它真的退出，
        # 免得下一个用例看到残留的看门狗线程。
        for thread in threading.enumerate():
            if thread.name == WATCHDOG_THREAD_NAME:
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
