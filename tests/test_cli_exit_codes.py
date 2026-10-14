"""Exit codes of the command line, as documented in cli/main.py.

0 finished (some files may have failed), 1 nothing came out, 2 bad arguments
or refused input, 3 no engine, 4 needs a confirmation.  The end-to-end cases
with a real engine are in tests/test_cli.py; these need none.
"""

from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from cli import flow
from cli.main import main
from core.engine_client import EngineError, EngineUnavailable

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

    def __init__(self, *, preflight_error: EngineError | None = None, surfaces_error: EngineError | None = None):
        self.preflight_error = preflight_error
        self.surfaces_error = surfaces_error

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


class MainExitCodeTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
