"""``translator-sidecar mcp``: the seven tools, end to end.

The tools run in this process through the SDK's in-memory client, against
the fixture engine (tests/fixtures/cli_engine.py) playing the open app in an
isolated data directory; one case talks to ``python -m cli mcp`` over stdio
the way an MCP client does.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import anyio
import pytest
from mcp import Client, StdioServerParameters
from openpyxl import Workbook

from cli.mcp_server import TOOL_NAMES, EngineSession, build_server
from core import engine_file
from core.engine_client import EngineUnavailable
from tests.fixtures.cli_engine import ROOT, AppEngine


@pytest.fixture(scope="module")
def app_data(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("mcp-app-data")


@pytest.fixture(scope="module")
def app(app_data: Path):
    engine = AppEngine(app_data, delay=0.2)
    yield engine
    engine.close()


@pytest.fixture
def tools(app, app_data: Path, monkeypatch):
    """Call tools as an MCP client would: ``tools(name, **arguments)``."""
    monkeypatch.setenv("TRANSLATOR_APP_DATA_DIR", str(app_data))
    session = EngineSession()

    def call(name: str, **arguments: Any) -> tuple[bool, Any]:
        async def go() -> tuple[bool, Any]:
            async with Client(build_server(session)) as client:
                result = await client.call_tool(name, arguments)
                if result.is_error:
                    return True, result.content[0].text
                return False, result.structured_content

        return anyio.run(go)

    yield call
    session.close()


def _notes(root: Path, count: int = 2) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (root / f"n{index}.txt").write_text(f"note {index}", encoding="utf-8")
    return root


def _workbook(path: Path) -> Path:
    workbook = Workbook()
    workbook.active["A1"] = "钢筋"
    workbook.save(path)
    return path


def _until_done(tools, task_id: str) -> dict:
    for _ in range(30):
        failed, body = tools("wait_task", task_id=task_id, timeout_seconds=5)
        assert not failed, body
        if body["done"]:
            return body
    raise TimeoutError(task_id)


def test_the_seven_tools_are_listed_with_descriptions():
    async def go() -> list:
        async with Client(build_server(EngineSession())) as client:
            return (await client.list_tools()).tools

    listed = anyio.run(go)

    assert [tool.name for tool in listed] == list(TOOL_NAMES)
    assert all(tool.description for tool in listed)
    start = next(tool for tool in listed if tool.name == "start_translation")
    assert start.input_schema["required"] == ["paths"]


def test_list_types_and_scan(tools, tmp_path: Path):
    failed, body = tools("list_types")
    assert not failed
    keys = [item["key"] for item in body["types"]]
    # Types shipped in adapters/ (「文本」) are registered at startup too.
    assert keys[:4] == ["excel", "word", "pdf", "cad"]
    assert "ext:sample" in keys and "ext:text" in keys

    failed, body = tools("scan_sources", paths=[str(_notes(tmp_path))])
    assert not failed
    assert body["type"] == "ext:sample"
    assert sorted(body["files"]) == ["n0.txt", "n1.txt"] and body["file_count"] == 2


def test_translate_wait_and_collect_the_result(app, tools, tmp_path: Path):
    failed, started = tools(
        "start_translation", paths=[str(_notes(tmp_path))], target_lang="fr", options={"uppercase": True}
    )
    assert not failed, started
    assert started["type"] == "ext:sample"
    assert started["in_app_task_center"] is True
    assert started["options"]["options"] == {"uppercase": True}

    done = _until_done(tools, started["task_id"])
    assert done["state"] == "done"

    failed, result = tools("get_task_result", task_id=started["task_id"])
    assert not failed
    assert result["generated_count"] == 2
    assert all(Path(item["output"]).read_text(encoding="utf-8").startswith("NOTE") for item in result["files"])
    recent = app.http.get("/api/tasks").json()["recent"]
    assert any(item["task_id"] == started["task_id"] for item in recent)


def test_get_wait_and_stop_a_running_task(app, tools, tmp_path: Path):
    failed, started = tools("start_translation", paths=[str(_notes(tmp_path, 40))])
    assert not failed, started
    task_id = started["task_id"]

    failed, waited = tools("wait_task", task_id=task_id, timeout_seconds=0.2)
    assert not failed
    assert waited["done"] is False and waited["file_count"] == 40
    failed, status = tools("get_task", task_id=task_id)
    assert not failed
    assert status["terminal"] is False and "files" not in status

    failed, stopped = tools("stop_task", task_id=task_id)
    assert not failed and stopped["task_id"] == task_id
    assert _until_done(tools, task_id)["state"] == "stopped"


def test_a_confirmation_is_returned_not_taken(app, tools, tmp_path: Path):
    failed, running = tools("start_translation", paths=[str(_notes(tmp_path / "slow", 40))])
    assert not failed
    source = str(_workbook(tmp_path / "a.xlsx"))
    try:
        failed, answer = tools("start_translation", paths=[source])
        assert not failed
        assert answer["needs_confirmation"] is True
        assert set(answer["accept"]) == {"confirmation_token"}
        assert "task_id" not in answer

        failed, started = tools("start_translation", paths=[source], accept=answer["accept"])
        assert not failed, started
        assert started["accepted"] == {"confirmation_token": True}
        # The echo engine leaves review marks, so the workbook finishes "with issues".
        assert _until_done(tools, started["task_id"])["generated_count"] == 1
        failed, result = tools("get_task_result", task_id=started["task_id"])
        assert not failed
        assert result["review_count"] and result["review_items"], result
        assert any(item.get("location") == "A1" for item in result["review_items"]), result["review_items"]

        failed, started = tools("start_translation", paths=[source], accept_all=True)
        assert not failed, started
        _until_done(tools, started["task_id"])
    finally:
        tools("stop_task", task_id=running["task_id"])
        app.wait_terminal(running["task_id"])


@pytest.mark.parametrize(
    "arguments, message",
    [
        ({"paths": ["/nowhere/a.docx"]}, "找不到"),
        ({"options": {"loud": True}}, "loud"),
        ({"accept": {"surface": "word"}}, "surface"),
        ({"file_type": "nosuch"}, "nosuch"),
    ],
)
def test_refusals_come_back_as_tool_errors(tools, tmp_path: Path, arguments: dict, message: str):
    arguments = {"paths": [str(_notes(tmp_path))], **arguments}

    failed, text = tools("start_translation", **arguments)

    assert failed
    assert message in text


def test_unknown_tasks_are_tool_errors(tools):
    failed, text = tools("get_task", task_id="no-such-task")
    assert failed and text


class _Client:
    started_here = False

    def __init__(self, name: str, *, broken: bool = False) -> None:
        self.name, self.broken, self.closed = name, broken, False

    def surfaces(self) -> str:
        if self.broken:
            raise EngineUnavailable("gone")
        return self.name

    def close(self, timeout: float = 0) -> None:
        self.closed = True


def test_a_lost_app_is_replaced_for_reads_but_a_start_is_not_resent():
    made = [_Client("app", broken=True), _Client("own")]
    session = EngineSession(connector=lambda **_: made.pop(0))

    assert session.call(lambda client: client.surfaces()) == "own"

    lost = [_Client("app", broken=True)]
    session = EngineSession(connector=lambda **_: lost.pop(0))
    with pytest.raises(EngineUnavailable):
        session.call(lambda client: client.surfaces(), retry=False)


def test_stdio_lists_tools_without_an_engine_and_starts_one_on_use(tmp_path: Path):
    data = tmp_path / "closed-app"
    env = {**os.environ, "TRANSLATOR_APP_DATA_DIR": str(data)}
    env.pop("TRANSLATOR_SIDECAR_PARENT_PID", None)
    params = StdioServerParameters(command=sys.executable, args=["-m", "cli", "mcp"], cwd=str(ROOT), env=env)

    async def go() -> tuple[list[str], bool, dict]:
        with anyio.fail_after(120):
            async with Client(params) as client:
                names = [tool.name for tool in (await client.list_tools()).tools]
                engine_before = (data / "run" / "cli-engine.log").exists()
                result = await client.call_tool("list_types", {})
                return names, engine_before, result.structured_content or {}

    names, engine_before, types = anyio.run(go)

    assert names == list(TOOL_NAMES)
    assert engine_before is False
    assert {item["key"] for item in types["types"]} >= {"excel", "word", "pdf", "cad"}
    assert engine_file.read_engine_file(data) is None  # the engine went with the session


def test_the_frozen_smoke_shakes_hands():
    from scripts import frozen_smoke

    frozen_smoke._check_mcp_handshake()


def test_the_command_line_hands_mcp_to_the_server():
    from cli import main as cli_main

    with patch("cli.mcp_server.serve", return_value=0) as serve:
        assert cli_main.main(["mcp"]) == 0
    serve.assert_called_once_with()
    assert "mcp" in cli_main.COMMANDS

