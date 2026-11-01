"""本地 Excel 自动化（core/excel_automation.py）的离线分支覆盖。

xlwings / pythoncom / psutil 全部用替身：真 Excel 只在装了它、并且授权了自动化
的桌面机器上才走得通，构建机与 CI 都没有，所以这里只钉住「起了就一定要退出、
退出不了就补一刀、Windows 的 COM 与文件占用该怎么收场」这层可复现的逻辑。
真机上 Excel 是否放行属于发布前的实机验收。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core import excel_automation, xls_converter


class ExcelAutomationTests(unittest.TestCase):
    def test_windows_is_supported_for_local_excel_automation(self) -> None:
        with patch.object(excel_automation.platform, "system", return_value="Windows"):
            self.assertTrue(excel_automation.supports_local_excel_automation())

    def test_windows_thread_initialization_uses_com_lifecycle(self) -> None:
        fake_pythoncom = SimpleNamespace(
            CoInitialize=MagicMock(),
            CoUninitialize=MagicMock(),
        )
        with (
            patch.object(excel_automation.platform, "system", return_value="Windows"),
            patch.dict("sys.modules", {"pythoncom": fake_pythoncom}),
        ):
            state = excel_automation.initialize_excel_thread()
            fake_pythoncom.CoInitialize.assert_called_once_with()

            excel_automation.finalize_excel_thread(state)
            fake_pythoncom.CoUninitialize.assert_called_once_with()

    def test_non_windows_thread_initialization_is_noop(self) -> None:
        with patch.object(excel_automation.platform, "system", return_value="Darwin"):
            self.assertIsNone(excel_automation.initialize_excel_thread())

    def test_probe_reports_windows_com_initialization_failure(self) -> None:
        with (
            patch.object(excel_automation.platform, "system", return_value="Windows"),
            patch.object(
                excel_automation,
                "initialize_excel_thread",
                side_effect=RuntimeError("COM missing"),
            ),
        ):
            ok, reason = excel_automation.probe_local_excel_automation()

        self.assertFalse(ok)
        self.assertIn("未能连接本机 Excel", reason)
        # 这句话会出现在设置界面上，所以不能把库层的英文原文端给用户；
        # 原文只留在 debug 日志里。
        self.assertNotIn("COM missing", reason)
        self.assertIn("本机缺少连接 Excel 所需的组件", reason)


class ExcelAppLifecycleTests(unittest.TestCase):
    """xlwings App 的起、用、退（核心是 ``probe_local_excel_automation`` 的 finally）。

    审计 C2 线把「Excel 自动化 App 的进程生命周期只在装了 Excel 的机器上走得到、
    本机没构造出环境」列进未查完清单。进程里的 COM 换不了，但「拉起之后无论探测
    成功还是炸掉都必须 quit 掉、quit 不掉就必须 kill」是纯分支逻辑，用假体就能
    离线钉死；真 Excel 是否可被控制仍属实机验收。
    """

    def _probe(self, **create_side_effect) -> tuple[bool, str, MagicMock, MagicMock]:
        app = MagicMock()
        finalize = MagicMock()
        create = MagicMock(return_value=app, **create_side_effect)
        with (
            patch.object(excel_automation.platform, "system", return_value="Darwin"),
            patch.object(excel_automation, "create_excel_app", create),
            patch.object(excel_automation, "finalize_excel_thread", finalize),
        ):
            ok, reason = excel_automation.probe_local_excel_automation()
        return ok, reason, app, finalize

    def test_the_probe_quits_the_app_and_releases_the_thread_afterwards(self) -> None:
        ok, reason, app, finalize = self._probe()

        self.assertTrue(ok)
        self.assertEqual(reason, "")
        app.quit.assert_called_once_with()
        app.kill.assert_not_called()
        finalize.assert_called_once_with(None)

    def test_the_probe_kills_the_app_when_quit_does_not_work(self) -> None:
        """quit 抛错时不能把 Excel 留在后台——这是任务结束后僵尸进程的来源。"""
        app = MagicMock()
        app.quit.side_effect = RuntimeError("Excel 忙")
        finalize = MagicMock()
        with (
            patch.object(excel_automation.platform, "system", return_value="Darwin"),
            patch.object(excel_automation, "create_excel_app", return_value=app),
            patch.object(excel_automation, "finalize_excel_thread", finalize),
        ):
            ok, reason = excel_automation.probe_local_excel_automation()

        self.assertTrue(ok)
        app.quit.assert_called_once_with()
        app.kill.assert_called_once_with()
        finalize.assert_called_once_with(None)

    def test_the_probe_still_releases_the_thread_when_xlwings_is_missing(self) -> None:
        ok, reason, app, finalize = self._probe(side_effect=ImportError("No module named 'xlwings'"))

        self.assertFalse(ok)
        self.assertIn("本机缺少连接 Excel 所需的组件", reason)
        app.quit.assert_not_called()
        finalize.assert_called_once_with(None)

    def test_the_probe_reports_an_excel_that_refuses_to_start(self) -> None:
        ok, reason, app, finalize = self._probe(side_effect=RuntimeError("ApplicationNotFoundError"))

        self.assertFalse(ok)
        self.assertIn("本机 Excel 未能启动", reason)
        self.assertNotIn("ApplicationNotFoundError", reason)
        app.quit.assert_not_called()
        finalize.assert_called_once_with(None)

    def test_create_excel_app_asks_for_an_invisible_app_by_default(self) -> None:
        """App 是隐藏启动的：翻译在后台跑，不能把 Excel 窗口弹到用户眼前。"""
        xw = SimpleNamespace(App=MagicMock(return_value="app"))
        with patch.dict("sys.modules", {"xlwings": xw}):
            self.assertEqual(excel_automation.create_excel_app(), "app")
            self.assertEqual(excel_automation.create_excel_app(visible=True, add_book=True), "app")

        self.assertEqual(
            xw.App.call_args_list,
            [
                unittest.mock.call(visible=False, add_book=False),
                unittest.mock.call(visible=True, add_book=True),
            ],
        )


class _FakeExcelApp:
    """一个够用的 xlwings ``App`` 替身：有 pid、能 quit/kill，quit 可以故意失败。"""

    def __init__(self, *, pid: int = 4242, quit_error: Exception | None = None) -> None:
        self.pid = pid
        self.quit_error = quit_error
        self.quit_calls = 0
        self.kill_calls = 0
        self.display_alerts = True

    def quit(self) -> None:
        self.quit_calls += 1
        if self.quit_error is not None:
            raise self.quit_error

    def kill(self) -> None:
        self.kill_calls += 1


class ExcelAppEndToEndTests(unittest.TestCase):
    """走一遍真正的 ``create_excel_app``：从 ``xlwings`` 模块到 App 的退出。

    ``create_excel_app`` 里 ``import xlwings as xw`` 之后立刻 ``xw.App(...)``，这
    一段在没装 Excel 的机器上从来跑不到；这里把 ``xlwings`` 换成假模块（模拟
    Excel.Application 拉起的 App），钉住「传参 → 拿 pid → quit」这条线。
    """

    def test_an_app_is_created_probed_and_quit_through_the_real_wrapper(self) -> None:
        app = _FakeExcelApp()
        fake_xlwings = SimpleNamespace(App=MagicMock(return_value=app))
        with (
            patch.dict("sys.modules", {"xlwings": fake_xlwings}),
            patch.object(excel_automation.platform, "system", return_value="Darwin"),
            patch.object(excel_automation, "finalize_excel_thread") as finalize,
        ):
            ok, reason = excel_automation.probe_local_excel_automation()

        self.assertTrue(ok, reason)
        fake_xlwings.App.assert_called_once_with(visible=False, add_book=False)
        self.assertEqual(excel_automation.get_excel_process_pid(app), app.pid)
        self.assertIs(app.display_alerts, False, "探测成功后必须关掉弹窗，否则批量翻译会被 Excel 对话框卡住")
        self.assertEqual((app.quit_calls, app.kill_calls), (1, 0))
        finalize.assert_called_once_with(None)

    def test_an_app_whose_quit_fails_is_killed_instead(self) -> None:
        app = _FakeExcelApp(quit_error=RuntimeError("Excel.Application 忙"))
        with (
            patch.dict("sys.modules", {"xlwings": SimpleNamespace(App=MagicMock(return_value=app))}),
            patch.object(excel_automation.platform, "system", return_value="Darwin"),
            patch.object(excel_automation, "finalize_excel_thread"),
        ):
            ok, reason = excel_automation.probe_local_excel_automation()

        self.assertTrue(ok, reason)
        self.assertEqual((app.quit_calls, app.kill_calls), (1, 1))


class WindowsOnlyPathTests(unittest.TestCase):
    """Windows 独有路径：COM 卸载与「文件被 Excel 占用，unlink 抛 PermissionError」。

    审计 C2 线原话是「Windows 独有路径全部没验证，本机是 macOS」。COM 本身换不了
    平台，但 ``initialize_excel_thread`` / ``finalize_excel_thread`` 的配对、以及
    清理半成品时 unlink 撞上 Windows 的 ``PermissionError`` 该怎么收场，都是可以用
    替身跑到的分支；真机上 Excel 是否放行仍属实机验收。
    """

    def test_com_is_initialized_and_uninitialized_on_the_same_thread_state(self) -> None:
        pythoncom = SimpleNamespace(CoInitialize=MagicMock(), CoUninitialize=MagicMock())
        with (
            patch.object(excel_automation.platform, "system", return_value="Windows"),
            patch.dict("sys.modules", {"pythoncom": pythoncom}),
        ):
            state = excel_automation.initialize_excel_thread()
            self.assertTrue(state.com_initialized)
            self.assertIs(state.pythoncom, pythoncom)

            excel_automation.finalize_excel_thread(state)

        pythoncom.CoUninitialize.assert_called_once_with()

    def test_finalizing_a_state_that_was_never_initialized_does_nothing(self) -> None:
        pythoncom = SimpleNamespace(CoInitialize=MagicMock(), CoUninitialize=MagicMock())
        with patch.dict("sys.modules", {"pythoncom": pythoncom}):
            for state in (None, SimpleNamespace(com_initialized=False), SimpleNamespace(pythoncom=None)):
                with self.subTest(state=state):
                    excel_automation.finalize_excel_thread(state)
        pythoncom.CoUninitialize.assert_not_called()

    def test_a_failing_com_uninitialize_is_swallowed(self) -> None:
        """退出 Worker 时 COM 卸载失败不能把任务结果一起带走。"""
        pythoncom = SimpleNamespace(
            CoInitialize=MagicMock(),
            CoUninitialize=MagicMock(side_effect=OSError("COM 已经被别人关了")),
        )
        with (
            patch.object(excel_automation.platform, "system", return_value="Windows"),
            patch.dict("sys.modules", {"pythoncom": pythoncom}),
        ):
            state = excel_automation.initialize_excel_thread()
            excel_automation.finalize_excel_thread(state)  # 不应抛出
        pythoncom.CoUninitialize.assert_called_once_with()

    def test_windows_without_pythoncom_says_what_is_missing(self) -> None:
        with (
            patch.object(excel_automation.platform, "system", return_value="Windows"),
            patch.dict("sys.modules", {"pythoncom": None}),
        ):
            with self.assertRaises(RuntimeError) as raised:
                excel_automation.initialize_excel_thread()
        self.assertIn("本机缺少连接 Excel 所需的组件", str(raised.exception))

    def test_a_partial_output_locked_by_excel_is_reported_not_raised(self) -> None:
        """Windows 上 Excel 还攥着半成品时 unlink 会抛 PermissionError。

        ``_discard_partial_output`` 只记一条 debug 日志就收手：清理失败不能盖住
        真正的转换异常，更不能把用户挡在「打不开这个文件」的报错前面。
        """
        half = Path(tempfile.mkdtemp()) / "half.xlsx"
        half.write_bytes(b"half written")
        locked = PermissionError(32, "The process cannot access the file because it is being used by another process")

        with patch.object(Path, "unlink", side_effect=locked):
            xls_converter._discard_partial_output(half)  # 不应抛出

        self.assertTrue(half.exists())

    def test_get_excel_process_pid_only_accepts_a_positive_int(self) -> None:
        self.assertEqual(excel_automation.get_excel_process_pid(SimpleNamespace(pid=4321)), 4321)
        for bad in (None, 0, -1, "4321", SimpleNamespace()):
            with self.subTest(pid=bad):
                self.assertIsNone(excel_automation.get_excel_process_pid(bad))


def _fake_process(pid: int) -> MagicMock:
    process = MagicMock()
    process.pid = pid
    return process


class TerminateProcessTreeTests(unittest.TestCase):
    """停止任务时的 Excel 兜底：按进程树杀，先礼后兵，杀不掉要如实返回 False。

    ``terminate_process_tree``（core/excel_automation.py）只在 Windows/macOS 真机
    上被调用，之前没有任何专项用例；这里用假的 ``psutil.Process`` 覆盖它的每条分支。
    """

    def _run(
        self,
        *,
        pid: int | None = 100,
        children: list[MagicMock] | None = None,
        wait_alive: list[list[int]] | None = None,
        process_error: Exception | None = None,
        force: bool = False,
    ) -> tuple[bool, MagicMock, MagicMock]:
        """跑一次 ``terminate_process_tree``；``wait_alive`` 逐次给出「还活着的 PID」。

        ``psutil.wait_procs`` 返回 ``(gone, alive)``，生产代码只看 ``alive``。
        """
        root = _fake_process(pid or 0)
        root.children.return_value = list(children or [])
        known = {root.pid: root, **{proc.pid: proc for proc in children or []}}
        rounds = iter(wait_alive or [[], []])

        def _wait(procs, timeout):  # noqa: ARG001 - 跟着 psutil.wait_procs 的签名走
            alive = [known[item] for item in next(rounds, [])]
            return [proc for proc in procs if proc not in alive], alive

        process = MagicMock(side_effect=process_error) if process_error else MagicMock(return_value=root)
        waits = MagicMock(side_effect=_wait)
        with (
            patch.object(excel_automation.psutil, "Process", process),
            patch.object(excel_automation.psutil, "wait_procs", waits),
        ):
            ok = excel_automation.terminate_process_tree(pid, force=force, timeout=1.0)
        return ok, root, waits

    @staticmethod
    def _waited(waits: MagicMock) -> list[int]:
        return [proc.pid for proc in waits.call_args[0][0]]

    def test_no_pid_means_nothing_to_do(self) -> None:
        with patch.object(excel_automation.psutil, "Process") as process:
            self.assertFalse(excel_automation.terminate_process_tree(None))
            self.assertFalse(excel_automation.terminate_process_tree(0))
        process.assert_not_called()

    def test_a_process_that_has_already_exited_counts_as_terminated(self) -> None:
        ok, _root, _waits = self._run(process_error=excel_automation.psutil.NoSuchProcess(100))
        self.assertTrue(ok)

    def test_excel_and_its_helpers_are_terminated_and_deduplicated(self) -> None:
        helper = _fake_process(101)
        addin = _fake_process(102)
        ok, root, waits = self._run(children=[helper, addin, helper])

        self.assertTrue(ok)
        root.terminate.assert_called_once_with()
        helper.kill.assert_not_called()
        helper.terminate.assert_called_once_with()
        addin.terminate.assert_called_once_with()
        # 进程树按「根 → 子进程」收集、倒序处理：先动子进程，最后才是 Excel 本身。
        self.assertEqual(self._waited(waits), [101, 102, 100])
        self.assertEqual(waits.call_args.kwargs["timeout"], 1.0)

    def test_force_kills_every_process_in_the_tree(self) -> None:
        helper = _fake_process(101)
        ok, root, waits = self._run(children=[helper], force=True)

        self.assertTrue(ok)
        root.kill.assert_called_once_with()
        root.terminate.assert_not_called()
        helper.kill.assert_called_once_with()
        helper.terminate.assert_not_called()
        self.assertEqual(self._waited(waits), [101, 100])

    def test_a_survivor_is_killed_after_the_polite_attempt(self) -> None:
        """``terminate`` 只发礼貌信号；Excel 不理会时还得补一刀 ``kill``。"""
        stuck = _fake_process(101)
        ok, root, waits = self._run(
            children=[stuck],
            wait_alive=[[100, 101], []],
        )

        self.assertTrue(ok)
        stuck.kill.assert_called_once_with()
        root.kill.assert_called_once_with()
        self.assertEqual(self._waited(waits), [100, 101])

    def test_a_process_that_survives_even_a_force_kill_is_reported_as_stuck(self) -> None:
        """杀不掉要返回 False，调用方才能提示用户手动退出 Excel。"""
        stuck = _fake_process(101)
        ok, root, _waits = self._run(children=[stuck], force=True, wait_alive=[[100, 101]])

        self.assertFalse(ok)
        stuck.kill.assert_called_once_with()
        root.kill.assert_called_once_with()

    def test_a_force_kill_that_does_not_land_does_not_wait_twice(self) -> None:
        """``force=True`` 已经是最强硬的手段，不必再补第二轮等待。"""
        stuck = _fake_process(101)
        _ok, _root, waits = self._run(children=[stuck], force=True, wait_alive=[[101]])

        self.assertEqual(waits.call_count, 1)

    def test_processes_that_vanish_midway_do_not_break_the_cleanup(self) -> None:
        gone = _fake_process(101)
        gone.terminate.side_effect = excel_automation.psutil.NoSuchProcess(101)
        ok, root, _waits = self._run(children=[gone])

        self.assertTrue(ok)
        root.terminate.assert_called_once_with()
        gone.terminate.assert_called_once_with()


if __name__ == "__main__":
    unittest.main(verbosity=2)
