"""关闭软件时不经引擎进程树启动的程序也要结束，没停下的任务要留下在等什么。"""

from __future__ import annotations

import subprocess
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.cad_translation import CadPipelineError, SubprocessCadConverter


class MacOdaEndedWithTheEngineTests(unittest.TestCase):
    def test_a_killed_open_also_ends_the_oda_instance_it_launched(self) -> None:
        # Mac 上 ODA 经 LaunchServices 启动，不是引擎的子进程；引擎结束 open 之后
        # 要按本次独有的临时目录把 ODA 也结束。
        calls: list[list[str]] = []

        def fake_run(command, **_kwargs):
            calls.append(list(command))
            if command[0].endswith("open"):
                raise subprocess.CalledProcessError(-15, command, output="", stderr="")
            return SimpleNamespace(returncode=0)

        converter = SubprocessCadConverter("/Applications/ODAFileConverter.app/Contents/MacOS/ODAFileConverter")
        with patch("core.cad_translation.subprocess.run", side_effect=fake_run):
            with self.assertRaises(CadPipelineError):
                converter._run_macos_app(
                    Path("/Applications/ODAFileConverter.app"),
                    ["/tmp/xl-cad-convert-abc/in", "/tmp/xl-cad-convert-abc/out", "ACAD2018", "DXF", "0", "1"],
                    Path("/tmp/xl-cad-convert-abc/out"), "a", "DXF", 60,
                )
        self.assertIn(["pkill", "-f", "/tmp/xl-cad-convert-abc/in"], calls)

    def test_a_failed_launch_does_not_kill_anything(self) -> None:
        calls: list[list[str]] = []

        def fake_run(command, **_kwargs):
            calls.append(list(command))
            raise subprocess.CalledProcessError(1, command, output="", stderr="LSOpenURLsWithRole() failed")

        converter = SubprocessCadConverter("/Applications/ODAFileConverter.app/Contents/MacOS/ODAFileConverter")
        with patch("core.cad_translation.subprocess.run", side_effect=fake_run):
            with self.assertRaises(CadPipelineError):
                converter._run_macos_app(
                    Path("/Applications/ODAFileConverter.app"), ["/tmp/in", "/tmp/out"], Path("/tmp/out"), "a", "DXF", 60,
                )
        self.assertFalse(any(command[0] == "pkill" for command in calls))


class StuckTaskIsExplainedTests(unittest.TestCase):
    def test_the_waiting_thread_is_written_to_the_log(self) -> None:
        import os

        from api import task_manager

        release = threading.Event()
        # 让线程停在一段「文件名在 core/ 里」的代码上，模拟卡在转换或模型请求里的任务。
        stuck_code = compile("release.wait()", os.path.join(os.sep, "app", "core", "slow_converter.py"), "exec")
        worker = threading.Thread(target=exec, args=(stuck_code, {"release": release}), name="stuck-runner", daemon=True)
        worker.start()
        time.sleep(0.05)
        messages: list[str] = []
        try:
            with patch.object(task_manager.logger, "warning", side_effect=messages.append):
                task_manager._log_threads_still_running([SimpleNamespace(terminal=True, rerun_active=False)])
                self.assertEqual(messages, [])  # 都停下了就不写
                task_manager._log_threads_still_running([SimpleNamespace(terminal=False, rerun_active=False)])
        finally:
            release.set()
            worker.join(timeout=2)
        logged = [message for message in messages if "stuck-runner" in message]
        self.assertEqual(len(logged), 1)
        self.assertIn("slow_converter.py", logged[0])


class ModelRequestsCancelledOnCloseTests(unittest.TestCase):
    """关闭时在途的模型请求立刻取消：Mac 复测里任务正等着语言预检的回复，关闭等满了 12 秒。"""

    def tearDown(self) -> None:
        from core import text_transport

        text_transport._APP_CLOSING.clear()

    def test_a_request_budget_stops_once_the_app_is_closing(self) -> None:
        from core import text_transport

        budget = text_transport.RequestBudget()
        self.assertGreater(budget.check(), 0)
        text_transport.cancel_all_requests()
        with self.assertRaises(text_transport.TextTransportError) as caught:
            budget.check()
        self.assertEqual(caught.exception.kind, "cancelled")


if __name__ == "__main__":
    unittest.main(verbosity=2)
