"""The command line: detection, options, confirmations, exit codes, end to end.

The end-to-end cases run ``cli.main`` in this process against a real engine
started from ``tests/fixtures/cli_engine.py`` (``api.launcher`` with
``ext:sample`` registered and an echo engine in place of a model), with an
isolated ``TRANSLATOR_APP_DATA_DIR``.  Started first, that engine plays the
open app, and the command attaches to it; with no engine around, the command
starts one itself and it ends with the command.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from openpyxl import Workbook, load_workbook

from cli import flow
from cli.main import main as cli_main
from core import engine_client, engine_file
from core.engine_client import EngineError
from tests.fixtures.cli_engine import FIXTURE_ENGINE, AppEngine

ROOT = Path(__file__).resolve().parents[1]

DESCRIPTORS: list[dict[str, Any]] = [
    {"key": "excel", "builtin": True, "label": {"zh": "Excel 表格"}, "extensions": ["xls", "xlsx"],
     "capabilities": {"requires_source": True}, "options": []},
    {"key": "word", "builtin": True, "label": {"zh": "Word 文档"}, "extensions": ["doc", "docx"],
     "capabilities": {"requires_source": True}, "options": []},
    {"key": "pdf", "builtin": True, "label": {"zh": "PDF 与图片"}, "extensions": ["pdf", "png"],
     "capabilities": {"requires_source": True}, "options": []},
    {"key": "tm_clean", "builtin": True, "label": {"zh": "记忆库清洗"}, "extensions": [],
     "capabilities": {"requires_source": False}, "options": []},
    {"key": "ext:sample", "builtin": False, "label": {"zh": "示例文本"}, "extensions": ["txt", "png"],
     "capabilities": {"requires_source": True},
     "options": [{"key": "uppercase", "kind": "toggle", "default": False, "label": {"zh": "转为大写"}}]},
]


def _touch(path: Path, body: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


# ── detection ────────────────────────────────────────────────────────────


def test_a_file_goes_to_the_first_type_declaring_its_extension(tmp_path: Path):
    detection = flow.detect(DESCRIPTORS, [str(_touch(tmp_path / "a.PNG"))])

    assert detection.key == "pdf"  # built-in pages register first
    assert detection.source_path == str(tmp_path / "a.PNG")
    assert detection.selected_paths == []


def test_a_folder_goes_to_the_type_with_most_files(tmp_path: Path):
    for name in ("a.txt", "b.txt", "sub/c.txt", "d.docx"):
        _touch(tmp_path / name)
    _touch(tmp_path / "x_翻译输出_20260101" / "old.docx")
    _touch(tmp_path / ".hidden" / "e.docx")

    detection = flow.detect(DESCRIPTORS, [str(tmp_path)])

    assert detection.key == "ext:sample"
    assert detection.counts["ext:sample"] == 3
    assert detection.counts["word"] == 1  # output folders and hidden folders are not sources
    assert "Word 文档 1 个" in detection.note and "--type" in detection.note


def test_ties_go_to_registration_order(tmp_path: Path):
    _touch(tmp_path / "a.xlsx")
    _touch(tmp_path / "b.docx")

    assert flow.detect(DESCRIPTORS, [str(tmp_path)]).key == "excel"


def test_several_files_in_one_folder(tmp_path: Path):
    files = [str(_touch(tmp_path / name)) for name in ("a.docx", "b.docx", "c.xlsx")]

    detection = flow.detect(DESCRIPTORS, files)

    assert detection.key == "word"
    assert detection.source_path == str(tmp_path)
    assert detection.selected_paths == files[:3]


def test_type_given_explicitly(tmp_path: Path):
    image = str(_touch(tmp_path / "a.png"))

    assert flow.detect(DESCRIPTORS, [image], type_key="EXT:SAMPLE").key == "ext:sample"
    with pytest.raises(flow.InputError, match="只处理"):
        flow.detect(DESCRIPTORS, [image], type_key="word")
    with pytest.raises(flow.InputError, match="可用的类型"):
        flow.detect(DESCRIPTORS, [image], type_key="tm_clean")


@pytest.mark.parametrize(
    "case",
    ["missing", "nothing", "two-folders", "folder-and-file"],
)
def test_detection_refusals(tmp_path: Path, case: str):
    if case == "missing":
        paths = [str(tmp_path / "nope.docx")]
    elif case == "nothing":
        paths = [str(_touch(tmp_path / "a.zip"))]
    elif case == "two-folders":
        paths = [str(_touch(tmp_path / "x" / "a.docx")), str(_touch(tmp_path / "y" / "b.docx"))]
    else:
        paths = [str(tmp_path), str(_touch(tmp_path / "a.docx"))]
    with pytest.raises(flow.InputError):
        flow.detect(DESCRIPTORS, paths)


# ── options ──────────────────────────────────────────────────────────────


def test_builtin_options_take_the_request_field_types():
    values = flow.parse_option_pairs(
        ["untranslated_only=yes", "cad_fit_default_scale=0.6", "cad_display_mode=source_first", "include_images=0"]
    )

    fields = flow.request_fields(DESCRIPTORS[0], values)

    assert fields == {
        "untranslated_only": True,
        "cad_fit_default_scale": 0.6,
        "cad_display_mode": "source_first",
        "include_images": False,
    }


@pytest.mark.parametrize("pair", ["nosuch=1", "surface=word", "source_path=/x", "untranslated_only=maybe", "noequals"])
def test_bad_builtin_options(pair: str):
    with pytest.raises(flow.InputError):
        flow.request_fields(DESCRIPTORS[0], flow.parse_option_pairs([pair]))


def test_run_time_type_options_follow_its_descriptor():
    sample = DESCRIPTORS[-1]

    assert flow.request_fields(sample, {"uppercase": "on"}) == {"options": {"uppercase": True}}
    with pytest.raises(flow.InputError, match="开关"):
        flow.request_fields(sample, {"uppercase": "loud"})
    with pytest.raises(flow.InputError, match="uppercase"):
        flow.request_fields(sample, {"untranslated_only": "true"})


def test_a_choice_option_takes_one_of_its_values():
    sample = {
        **DESCRIPTORS[-1],
        "options": [
            {
                "key": "layout",
                "kind": "choice",
                "default": "grow",
                "label": {"zh": "版式"},
                "choices": [{"value": "grow", "label": {"zh": "加高"}}, {"value": "shrink", "label": {"zh": "缩小"}}],
            }
        ],
    }
    assert flow.request_fields(sample, {"layout": " shrink "}) == {"options": {"layout": "shrink"}}
    with pytest.raises(flow.InputError, match="grow、shrink"):
        flow.request_fields(sample, {"layout": "sideways"})
    with pytest.raises(flow.InputError, match="grow、shrink"):
        flow.request_fields(sample, {"layout": True})


def test_a_number_option_takes_a_number_and_one_of_its_units():
    gap = {
        "key": "gap",
        "kind": "number",
        "default": 2,
        "min": 0,
        "max": 50,
        "label": {"zh": "间距"},
        "units": [{"value": "pt", "label": {"zh": "磅"}}, {"value": "mm", "label": {"zh": "毫米"}}],
        "unit": "pt",
    }
    sample = {**DESCRIPTORS[-1], "options": [gap, {**gap, "key": "plain", "units": [], "unit": ""}]}
    assert flow.request_fields(sample, {"gap": "3"}) == {"options": {"gap": "3pt"}}
    assert flow.request_fields(sample, {"gap": "1,5MM"}) == {"options": {"gap": "1.5mm"}}
    assert flow.request_fields(sample, {"plain": "4.25"}) == {"options": {"plain": 4.25}}
    with pytest.raises(flow.InputError, match="pt、mm"):
        flow.request_fields(sample, {"gap": "3cm"})
    with pytest.raises(flow.InputError, match="不带单位"):
        flow.request_fields(sample, {"plain": "3pt"})
    with pytest.raises(flow.InputError, match="不小于 0 的数字"):
        flow.request_fields(sample, {"gap": "-1"})
    with pytest.raises(flow.InputError, match="不能大于 50"):
        flow.request_fields(sample, {"gap": "80mm"})


def test_request_asks_for_the_apps_saved_values(tmp_path: Path):
    detection = flow.detect(DESCRIPTORS, [str(_touch(tmp_path / "a.txt"))])

    request = flow.build_request(detection, target_lang="fr", options={"uppercase": "true"})

    assert request == {
        "surface": "ext:sample",
        "source_path": str(tmp_path / "a.txt"),
        "selected_paths": [],
        "apply_saved_defaults": True,
        "target_lang": "fr",
        "options": {"uppercase": True},
    }


# ── confirmations ────────────────────────────────────────────────────────


class _Client:
    """Answers preflight from a script: an exception, or a response."""

    def __init__(self, answers: list[Any]) -> None:
        self.answers = list(answers)
        self.preflights: list[dict] = []
        self.started: list[dict] = []

    def preflight(self, request: dict) -> dict:
        self.preflights.append(dict(request))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def start(self, request: dict) -> dict:
        self.started.append(dict(request))
        return {"task_id": "t1", "state": "running"}


def _stop(field: str) -> EngineError:
    return EngineError(422, f"需要确认 {field}", reason="needs_confirmation", payload={"accept": {field: True}})


def test_a_stop_is_reported_with_what_continues_past_it():
    client = _Client([_stop("allow_xls_fallback")])

    with pytest.raises(flow.NeedsConfirmation) as stopped:
        flow.start_task(client, {"surface": "excel"})

    assert stopped.value.exit_code == flow.EXIT_CONFIRM
    assert stopped.value.as_dict() == {
        "needs_confirmation": True,
        "reason": "needs_confirmation",
        "message": "需要确认 allow_xls_fallback",
        "accept": {"allow_xls_fallback": True},
    }
    assert client.started == []


def test_yes_accepts_each_stop_in_turn_and_keeps_them():
    shared = {"requires_confirmation": True, "confirmation_token": "tok", "risk": {"warnings": ["共用连接"]}}
    client = _Client([_stop("allow_doc_fallback"), _stop("allow_known_review_failure"), shared])

    started = flow.start_task(client, {"surface": "word"}, accept_all=True)

    assert client.preflights[-1]["allow_doc_fallback"] is True
    assert client.preflights[-1]["allow_known_review_failure"] is True
    assert client.started == [
        {"surface": "word", "allow_doc_fallback": True, "allow_known_review_failure": True, "confirmation_token": "tok"}
    ]
    assert started.accepted == {"allow_doc_fallback": True, "allow_known_review_failure": True, "confirmation_token": True}
    assert "confirmation_token" not in started.request


def test_answering_with_accept_continues_without_yes():
    # The MCP way: the agent got ``accept`` back and sends it along.
    shared = {"requires_confirmation": True, "confirmation_token": "fresh", "risk": {}}
    client = _Client([shared])

    flow.start_task(client, {"surface": "word", "confirmation_token": "stale"})

    assert client.started[0]["confirmation_token"] == "fresh"


def test_a_stop_that_comes_back_after_accepting_is_not_looped():
    client = _Client([_stop("allow_xls_fallback"), _stop("allow_xls_fallback")])

    with pytest.raises(flow.NeedsConfirmation):
        flow.start_task(client, {"surface": "excel"}, accept_all=True)
    assert len(client.preflights) == 2


def test_other_refusals_pass_through():
    client = _Client([EngineError(409, "同类翻译已有任务正在进行", reason="surface_busy")])

    with pytest.raises(EngineError):
        flow.start_task(client, {"surface": "excel"}, accept_all=True)


# ── results ──────────────────────────────────────────────────────────────


def test_review_count_follows_the_task_center():
    rows = {
        "issues": [{"type": "api_unavailable", "severity": "needs_action"}],
        "review": {"items": [
            {"file": "a.xlsx", "worksheet": "S", "cell": "A1", "category": "unresolved"},
            {"file": "a.xlsx", "worksheet": "S", "cell": "A1", "category": "unresolved"},
            {"file": "a.xlsx", "worksheet": "S", "cell": "A2", "severity": "resolved"},
            {"file": "a.xlsx", "worksheet": "S", "cell": "A3", "severity": "info"},
        ]},
    }
    assert flow.review_count(rows) == 2  # one issue row + A1 once; resolved and info are not to-dos
    assert flow.review_count({"review": {"counts": {"needs_review": 3, "info": 1}}}) == 3
    assert flow.review_count({"review": {"total_count": 5}}) == 5
    assert flow.review_count({"kpi": {"review_count": 7}}) == 7
    assert flow.review_count({}) is None


# ── end to end ───────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def app_data(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("app-data")


@pytest.fixture(scope="module")
def app(app_data: Path):
    engine = AppEngine(app_data, delay=0.2)
    yield engine
    engine.close()


@pytest.fixture
def run(app_data: Path, monkeypatch, capsys):
    monkeypatch.setenv("TRANSLATOR_APP_DATA_DIR", str(app_data))

    def invoke(*argv: str) -> tuple[int, str, str]:
        code = cli_main(list(argv))
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return invoke


def _notes(root: Path, count: int = 2) -> Path:
    for index in range(count):
        _touch(root / f"n{index}.txt", f"note {index}")
    return root


def _wait_terminal(app: AppEngine, task_id: str) -> dict:
    return app.wait_terminal(task_id)


def test_types_lists_built_in_and_run_time_types(app, run):
    code, out, _err = run("types", "--json")

    assert code == 0
    keys = [item["key"] for item in json.loads(out)["types"]]
    # Types shipped in adapters/ (「文本」) are registered at startup too.
    assert keys[:4] == ["excel", "word", "pdf", "cad"]
    assert "ext:sample" in keys and "ext:text" in keys
    code, out, _err = run("types")
    assert "示例文本（扩展）" in out and "--option uppercase" in out
    # 数字选项列出可用的单位；从属的选项写明在什么时候有效。
    assert "--option spacing=<数字>[pt|mm|cm]  与周围内容的间距（layout_mode=fit 时有效）" in out


def test_scan_lists_what_would_be_translated(app, run, tmp_path: Path):
    code, out, _err = run("scan", str(_notes(tmp_path)), "--json")

    assert code == 0
    body = json.loads(out)
    assert body["type"] == "ext:sample"
    assert sorted(item["name"] for item in body["items"]) == ["n0.txt", "n1.txt"]


def test_translate_and_wait_streams_events_and_lands_in_the_task_center(app, run, tmp_path: Path):
    code, out, err = run("translate", str(_notes(tmp_path)), "--to", "fr", "--option", "uppercase=true", "--wait", "--json-lines")

    assert code == 0, err
    lines = [json.loads(line) for line in out.splitlines()]
    assert lines[0]["event"] == "started"
    assert lines[0]["request"]["options"] == {"uppercase": True}
    assert lines[0]["options"]["options"] == {"uppercase": True}
    assert any(line["event"] == "progress" for line in lines)
    result = lines[-1]
    assert result["event"] == "result"
    summary = result["result"]
    assert summary["state"] == "done"
    assert summary["generated_count"] == 2
    outputs = [Path(item["output"]) for item in summary["files"]]
    assert all(path.read_text(encoding="utf-8").startswith("NOTE") for path in outputs)
    # The app's task center lists it like any task started from the window.
    recent = app.http.get("/api/tasks").json()["recent"]
    assert any(item["task_id"] == lines[0]["task_id"] and item["surface"] == "ext:sample" for item in recent)


def test_translate_a_workbook_with_the_echo_engine(app, run, tmp_path: Path):
    workbook = Workbook()
    workbook.active["A1"] = "钢筋混凝土"
    source = tmp_path / "清单.xlsx"
    workbook.save(source)

    code, out, err = run("translate", str(source), "--to", "fr", "--wait", "--json")

    assert code == 0, err
    body = json.loads(out)
    assert body["type"] == "excel"
    output = Path(body["result"]["files"][0]["output"])
    assert "[fr] 钢筋混凝土" in str(load_workbook(output).active["A1"].value)


def test_translate_without_wait_then_status_and_stop(app, run, tmp_path: Path):
    code, out, err = run("translate", str(_notes(tmp_path, 40)), "--json")
    assert code == 0, err
    task_id = json.loads(out)["task_id"]

    code, out, _err = run("status", "--json")
    assert code == 0
    assert task_id in [item["task_id"] for item in json.loads(out)["active"]]

    code, out, _err = run("stop", task_id, "--json")
    assert code == 0
    assert json.loads(out)["task_id"] == task_id
    assert _wait_terminal(app, task_id)["state"] == "stopped"

    code, out, _err = run("status", task_id, "--json")
    body = json.loads(out)
    assert code == 0
    assert body["state"] == "stopped" and body["terminal"] is True


def test_a_shared_connection_needs_yes(app, run, tmp_path: Path):
    code, out, _err = run("translate", str(_notes(tmp_path / "slow", 40)), "--json")
    assert code == 0
    running = json.loads(out)["task_id"]
    workbook = Workbook()
    workbook.active["A1"] = "模板"
    workbook.save(tmp_path / "a.xlsx")
    try:
        code, out, err = run("translate", str(tmp_path / "a.xlsx"), "--json")
        assert code == flow.EXIT_CONFIRM
        body = json.loads(out)["error"]
        assert body["needs_confirmation"] is True and "confirmation_token" in body["accept"]
        assert "--yes" in err

        code, out, err = run("translate", str(tmp_path / "a.xlsx"), "--dry-run")
        assert code == flow.EXIT_CONFIRM
        assert json.loads(out)["confirmation"]["reason"] == "confirmation_required"

        code, out, err = run("translate", str(tmp_path / "a.xlsx"), "--yes", "--wait", "--json")
        assert code == 0, err
        assert json.loads(out)["accepted"] == {"confirmation_token": True}
    finally:
        app.http.post(f"/api/tasks/{running}/stop")
        _wait_terminal(app, running)


def test_dry_run_reports_files_and_effective_options(app, run, tmp_path: Path):
    code, out, err = run("translate", str(_notes(tmp_path)), "--dry-run", "--option", "uppercase=yes")

    assert code == 0, err
    body = json.loads(out)
    assert body["ready"] is True
    assert sorted(body["files"]) == ["n0.txt", "n1.txt"]
    assert body["options"]["options"] == {"uppercase": True}
    assert app.http.get("/api/tasks").json()["active"] == []


def test_exit_code_one_when_nothing_comes_out(app, run, tmp_path: Path):
    (tmp_path / "bad.txt").write_bytes(b"\xff\xfe\xfa")

    code, out, _err = run("translate", str(tmp_path), "--wait", "--json")

    assert code == flow.EXIT_FAILED
    assert json.loads(out)["result"]["state"] == "error"


def test_exit_code_two_for_bad_input(app, run, tmp_path: Path):
    assert run("translate", str(tmp_path / "missing.txt"))[0] == flow.EXIT_INPUT
    code, out, err = run("translate", str(_notes(tmp_path)), "--option", "loud=yes", "--json")
    assert code == flow.EXIT_INPUT
    assert "loud" in json.loads(out)["error"]["message"]
    with pytest.raises(SystemExit) as parsed:
        run("translate")
    assert parsed.value.code == flow.EXIT_INPUT


def test_exit_code_three_without_any_engine(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("TRANSLATOR_APP_DATA_DIR", str(tmp_path / "empty"))
    broken = ([sys.executable, "-c", "raise SystemExit(5)"], None)
    with patch.object(engine_client, "engine_command", return_value=broken):
        code = cli_main(["types"])
    assert code == flow.EXIT_ENGINE
    assert "翻译引擎" in capsys.readouterr().err
    assert cli_main(["stop", "abc"]) == flow.EXIT_ENGINE


def test_without_the_app_the_command_starts_its_own_engine(tmp_path: Path, monkeypatch, capsys):
    data = tmp_path / "closed-app"
    monkeypatch.setenv("TRANSLATOR_APP_DATA_DIR", str(data))
    with patch.object(engine_client, "engine_command", return_value=(FIXTURE_ENGINE, str(ROOT))):
        # No --wait: the engine ends with the command, so it waits anyway.
        code = cli_main(["translate", str(_notes(tmp_path / "src")), "--json"])
    captured = capsys.readouterr()

    assert code == 0, captured.err
    assert "已改为等待任务完成" in captured.err
    assert json.loads(captured.out)["result"]["generated_count"] == 2
    # The engine it started has gone with it.
    assert engine_file.read_engine_file(data) is None


def test_the_sidecar_entry_hands_subcommands_to_the_cli(tmp_path: Path):
    env = {**os.environ, "TRANSLATOR_APP_DATA_DIR": str(tmp_path)}
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "launch_sidecar.py"), "translate", str(tmp_path / "missing.docx")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == flow.EXIT_INPUT
    assert "找不到" in completed.stderr
    assert completed.stdout == ""


def test_the_frozen_smoke_runs_types():
    from scripts import frozen_smoke

    frozen_smoke._check_cli_types()  # from source: python -m cli, with its own engine

    three = json.dumps({"types": [{"key": key} for key in ("excel", "word", "pdf")]})
    with patch.object(frozen_smoke, "_cli_command", return_value=([sys.executable, "-c", f"print({three!r})"], None)):
        with pytest.raises(RuntimeError, match="cad"):
            frozen_smoke._check_cli_types()
    with patch.object(frozen_smoke, "_cli_command", return_value=([sys.executable, "-c", "raise SystemExit(3)"], None)):
        with pytest.raises(RuntimeError, match="exited 3"):
            frozen_smoke._check_cli_types()
