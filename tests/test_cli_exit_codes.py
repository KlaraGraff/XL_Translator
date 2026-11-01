"""Exit codes of the command line, as documented in cli/main.py.

0 finished (some files may have failed), 1 nothing came out, 2 bad arguments
or refused input, 3 no engine, 4 needs a confirmation.  Ctrl+C while waiting
is 130, not an exit code of its own, and is covered here too.  The end-to-end
cases with a real engine are in tests/test_cli.py; these need none.
"""

from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from cli import flow
from cli.main import main
from core.engine_client import EngineError, EngineUnavailable, TaskEvent

EXCEL = {
    "key": "excel",
    "builtin": True,
    "label": {"zh": "Excel 表格"},
    "extensions": ["xlsx"],
    "capabilities": {"requires_source": True},
    "options": [],
}


def _status(state: str, file_states: list[str]) -> dict:
    return {
        "task_id": "t1",
        "surface": "excel",
        "state": state,
        "terminal": True,
        "result": {"output_dir": "/out"},
        "file_progress": {
            "files": [
                {"name": f"{index}.xlsx", "state": file_state, "result": {"output_path": f"/out/{index}.xlsx"}}
                for index, file_state in enumerate(file_states)
            ]
        },
    }


class ResultExitCodeTests(unittest.TestCase):
    def _code(self, state: str, file_states: list[str]) -> int:
        return flow.exit_code_for(flow.summarize(_status(state, file_states)))

    def test_all_failed_results_return_one(self) -> None:
        self.assertEqual(self._code("completed_with_issues", ["failed", "failed"]), flow.EXIT_FAILED)

    def test_at_least_one_success_returns_zero(self) -> None:
        self.assertEqual(self._code("completed_with_issues", ["failed", "generated"]), flow.EXIT_OK)
        self.assertEqual(self._code("done", ["generated"]), flow.EXIT_OK)

    def test_a_task_without_a_file_list_counts_by_its_state(self) -> None:
        self.assertEqual(self._code("done", []), flow.EXIT_OK)
        self.assertEqual(self._code("error", []), flow.EXIT_FAILED)

    def test_stopped_errored_or_interrupted_tasks_return_one(self) -> None:
        for state in ("stopped", "error", "interrupted"):
            with self.subTest(state=state):
                self.assertEqual(self._code(state, ["generated"]), flow.EXIT_FAILED)


class _Client:
    started_here = False

    def __init__(
        self,
        *,
        preflight_error: EngineError | None = None,
        surfaces_error: EngineError | None = None,
        events_interrupt: bool = False,
        events_script: list[TaskEvent] | None = None,
    ):
        self.preflight_error = preflight_error
        self.surfaces_error = surfaces_error
        self.events_interrupt = events_interrupt
        self.events_script = list(events_script or [])
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def close(self) -> None:
        pass

    def surfaces(self) -> list[dict]:
        if self.surfaces_error:
            raise self.surfaces_error
        return [EXCEL]

    def preflight(self, request: dict) -> dict:
        if self.preflight_error:
            raise self.preflight_error
        return {"requires_confirmation": False}

    def start(self, request: dict) -> dict:
        return {"task_id": "t1", "state": "running"}

    def events(self, task_id: str):
        yield from self.events_script
        if self.events_interrupt:
            # Ctrl+C 落在事件流里：GeneratorExit 不是 KeyboardInterrupt，
            # 所以这里显式抛出，与 ``client.events`` 停住等待时一样。
            raise KeyboardInterrupt

    def results(self, task_id: str) -> dict:
        return _status("done", ["generated"])


class _CliCase(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.source = Path(folder.name) / "a.xlsx"
        self.source.write_bytes(b"")

    def _run(self, argv: list[str], client: _Client | Exception) -> tuple[int, str, str]:
        out, err = StringIO(), StringIO()
        connect = {"side_effect": client} if isinstance(client, Exception) else {"return_value": client}
        with patch("cli.main.connect", **connect), redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()


class MainExitCodeTests(_CliCase):

    def test_no_engine_returns_three(self) -> None:
        code, _out, err = self._run(["types"], EngineUnavailable("翻译引擎没有启动"))
        self.assertEqual(code, flow.EXIT_ENGINE)
        self.assertIn("翻译引擎没有启动", err)

    def test_refused_input_returns_two(self) -> None:
        refusal = EngineError(422, "未找到可翻译的文件。", reason="invalid_input")
        code, out, _err = self._run(["translate", str(self.source), "--json"], _Client(preflight_error=refusal))
        self.assertEqual(code, flow.EXIT_INPUT)
        self.assertIn("invalid_input", out)

    def test_a_confirmable_stop_returns_four(self) -> None:
        stop = EngineError(
            422, "需要确认。", reason="needs_confirmation", payload={"accept": {"allow_xls_fallback": True}}
        )
        code, out, err = self._run(["translate", str(self.source), "--json"], _Client(preflight_error=stop))
        self.assertEqual(code, flow.EXIT_CONFIRM)
        self.assertIn("allow_xls_fallback", out)
        self.assertIn("--yes", err)

    def test_bad_arguments_return_two(self) -> None:
        with redirect_stderr(StringIO()):
            self.assertEqual(main([]), flow.EXIT_INPUT)
            with self.assertRaises(SystemExit) as parsed:
                main(["translate", "--wait"])
        self.assertEqual(parsed.exception.code, flow.EXIT_INPUT)
        code, _out, _err = self._run(["translate", str(self.source), "--option", "nope"], _Client())
        self.assertEqual(code, flow.EXIT_INPUT)


class InterruptTests(_CliCase):
    """Ctrl+C 停在 ``_follow`` 时（cli/main.py:200 → 退出码 130）。

    审计 C2 线把「CLI 中断路径只有阅读代码、没有复跑证据」列在未查完清单里。
    分叉点只有 ``client.started_here``：引擎是本命令拉起来的，中断就等于任务
    本身被掐断；接的是已经开着的应用，中断只是不再看进度，任务还在跑。
    """

    def _interrupted(self, *, started_here: bool) -> tuple[int, str, str]:
        client = _Client(events_interrupt=True)
        client.started_here = started_here
        return self._run(["translate", str(self.source), "--wait"], client)

    def test_interrupting_a_self_started_engine_returns_130(self) -> None:
        code, _out, err = self._interrupted(started_here=True)
        self.assertEqual(code, 130)
        self.assertIn("已中断：任务随翻译引擎停止。", err)

    def test_interrupting_a_wait_against_an_open_app_returns_130(self) -> None:
        code, out, err = self._interrupted(started_here=False)
        self.assertEqual(code, 130)
        # 任务没被掐断，所以既不能报「任务随引擎停止」，也不能直接返回上一次的
        # 终态摘要冒充结果。
        self.assertIn("已停止等待；任务仍在应用中运行，可用 stop t1 停止。", err)
        self.assertNotIn("已中断：任务随翻译引擎停止。", err)
        self.assertNotIn("已完成", out)

    def test_an_interrupt_during_events_keeps_the_task_and_closes_the_client(self) -> None:
        """中断只能结束「等待」，不能顺带把引擎侧的任务停掉。"""
        client = _Client(events_interrupt=True)
        client.stop = MagicMock()
        code, _out, _err = self._run(["translate", str(self.source), "--wait"], client)
        self.assertEqual(code, 130)
        client.stop.assert_not_called()

    def test_the_result_summary_is_printed_when_the_stream_finishes(self) -> None:
        """对照组：不中断时照常打印摘要，证明上面测的是中断分支而不是常态。"""
        client = _Client(events_script=[TaskEvent(id=1, type="done", data={})])
        code, out, _err = self._run(["translate", str(self.source), "--wait"], client)
        self.assertEqual(code, flow.EXIT_OK)
        self.assertIn("已完成", out)
        self.assertIn("输出目录：/out", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
