"""DWG conversion happens inside the task, never in the scan.

The external converter takes seconds to minutes per drawing.  The scan page
used to convert every DWG in one HTTP request with no progress (and the task
start converted them all again), so a folder of drawings looked hung.  Now the
scan and the task start only list DWG files; the runner converts each drawing
with run-log lines and progress that moves when a conversion has finished.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import core.process_artifacts as process_artifacts
import settings as app_settings
from api.app import create_app
from api.task_manager import TaskOptions, TranslationTaskManager
from core.cad_task_runner import CadTaskRunner, _BATCH_PROGRESS_SCALE
from core.cad_translation import (
    CAD_DWG_STAGE_WEIGHTS,
    CAD_STAGE_WEIGHTS,
    CadPipelineOptions,
    CadProgress,
    CadTranslationPipeline,
    SubprocessCadConverter,
)
from core.task_runner import LogMsg, ProgressMsg
from settings import AppSettings


def _dxf_text(word: str = "Door") -> str:
    return f"0\nSECTION\n2\nENTITIES\n0\nTEXT\n1\n{word}\n0\nENDSEC\n0\nEOF\n"


class _CountingConverter:
    """Copies the DXF text through, like the fakes elsewhere, and records each call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def dwg_to_dxf(self, source: Path, destination: Path) -> Path:
        self.calls.append(("dwg_to_dxf", Path(source).name))
        destination.write_bytes(Path(source).read_bytes())
        return destination

    def dxf_to_dwg(self, source: Path, destination: Path) -> Path:
        self.calls.append(("dxf_to_dwg", Path(source).name))
        destination.write_bytes(Path(source).read_bytes())
        return destination


@pytest.fixture()
def no_conversion(monkeypatch):
    """Fail loudly if anything runs the real converter adapter."""
    calls: list[str] = []

    def forbidden(self, source, destination, output_type):
        calls.append(f"{Path(source).name}->{output_type}")
        raise AssertionError("the converter must not run here")

    monkeypatch.setattr(SubprocessCadConverter, "_run", forbidden)
    return calls


_CONNECTED = {"plugin": "enabled", "oda": "connected", "converter": "/opt/oda/ODAFileConverter", "enabled": True}
_MISSING = {"plugin": "enabled", "oda": "missing", "converter": None, "enabled": False}


def _drawings(root: Path) -> None:
    (root / "a.dxf").write_text(_dxf_text("Door"), encoding="utf-8")
    (root / "b.dwg").write_text(_dxf_text("Window"), encoding="utf-8")
    (root / "c.dwg").write_text(_dxf_text("Wall"), encoding="utf-8")


def test_scan_lists_dwg_without_converting_and_sums_only_known_counts(tmp_path, monkeypatch, no_conversion):
    monkeypatch.setattr(app_settings, "APP_DATA_DIR", tmp_path / "app-data")
    source = tmp_path / "drawings"
    source.mkdir()
    _drawings(source)
    with patch("api.surfaces.cad.probe_status", return_value=_CONNECTED):
        with TestClient(create_app()) as client:
            response = client.post("/api/sources/scan", json={"surface": "cad", "path": str(source)})

    assert response.status_code == 200, response.text
    assert no_conversion == []
    payload = response.json()
    items = {item["name"]: item for item in payload["items"]}
    assert items["a.dxf"]["counts_pending"] is False
    assert items["a.dxf"]["text_count"] == 1 and items["a.dxf"]["candidate_count"] == 1
    for name in ("b.dwg", "c.dwg"):
        assert items[name]["counts_pending"] is True
        assert items[name]["text_count"] is None
        assert items[name]["candidate_count"] is None
        assert items[name]["uncertain_count"] is None
        # 转换工具已连接：不是「需转换工具」。
        assert items[name]["needs_conversion"] is False
    assert payload["summary"] == {
        "file_count": 3,
        "text_entity_count": 1,
        "candidate_count": 1,
        "counts_pending_file_count": 2,
    }
    assert payload["risk"]["message"] == ""


def test_scan_without_converter_still_flags_dwg_as_needing_it(tmp_path, monkeypatch, no_conversion):
    monkeypatch.setattr(app_settings, "APP_DATA_DIR", tmp_path / "app-data")
    _drawings(tmp_path)
    with patch("api.surfaces.cad.probe_status", return_value=_MISSING):
        with TestClient(create_app()) as client:
            response = client.post("/api/sources/scan", json={"surface": "cad", "path": str(tmp_path)})
    assert response.status_code == 200, response.text
    items = {item["name"]: item for item in response.json()["items"]}
    assert items["b.dwg"]["needs_conversion"] is True
    assert items["a.dxf"]["needs_conversion"] is False
    assert "尚未连接图纸转换工具" in response.json()["risk"]["message"]


def test_legacy_cad_scan_route_does_not_convert_either(tmp_path, monkeypatch, no_conversion):
    monkeypatch.setattr(app_settings, "APP_DATA_DIR", tmp_path / "app-data")
    _drawings(tmp_path)
    with patch("api.routes.cad.probe_status", return_value=_CONNECTED):
        with TestClient(create_app()) as client:
            response = client.post("/api/cad/scan", json={"paths": [str(tmp_path)], "target_language": "zh"})
    assert response.status_code == 200, response.text
    assert no_conversion == []
    items = {item["name"]: item for item in response.json()["items"]}
    assert items["b.dwg"]["counts_pending"] is True and items["b.dwg"]["text_count"] is None


def test_task_start_lists_drawings_without_converting(tmp_path, monkeypatch, no_conversion):
    monkeypatch.setattr(app_settings, "APP_DATA_DIR", tmp_path / "app-data")
    _drawings(tmp_path)
    manager = TranslationTaskManager(settings_loader=AppSettings)
    with patch("api.surfaces.cad.probe_status", return_value=_CONNECTED):
        files = manager._scan(tmp_path, "cad", TaskOptions())
        selected = manager._scan(
            tmp_path, "cad", TaskOptions(), selected_paths={str((tmp_path / "c.dwg").resolve())}
        )
    assert no_conversion == []
    assert [Path(item.path).name for item in files] == ["a.dxf", "b.dwg", "c.dwg"]
    assert [item.format for item in files] == ["dxf", "dwg", "dwg"]
    assert [Path(item.path).name for item in selected] == ["c.dwg"]


def _run(runner: CadTaskRunner) -> list[object]:
    runner.start()
    messages: list[object] = []
    while runner.needs_poll():
        message = runner.get_message(timeout=1)
        if message is not None:
            messages.append(message)
    return messages


def test_runner_logs_each_conversion_with_its_duration(tmp_path, monkeypatch):
    monkeypatch.setattr(process_artifacts, "APP_DATA_DIR", tmp_path / "app-data")
    source = tmp_path / "plan.dwg"
    source.write_text(_dxf_text("Door"), encoding="utf-8")
    converter = _CountingConverter()
    runner = CadTaskRunner(
        files=[SimpleNamespace(path=source, name=source.name, format="dwg")],
        source_root=tmp_path,
        output_dir=tmp_path / "results",
        converter=converter,
        translator=lambda texts, glossary: {"Door": "门"},
        options=CadPipelineOptions(),
    )
    messages = _run(runner)
    logs = [message.message for message in messages if isinstance(message, LogMsg)]

    assert converter.calls == [("dwg_to_dxf", "plan.dwg"), ("dxf_to_dwg", "plan_translated.dxf"), ("dwg_to_dxf", "plan_staged.dwg")]
    expected = [
        r"plan\.dwg：正在转换图纸格式（DWG → DXF）",
        r"plan\.dwg：图纸格式转换完成，用时 \d+\.\d 秒",
        r"plan\.dwg：正在将译文转换回 DWG 图纸",
        r"plan\.dwg：已转换回 DWG 图纸，用时 \d+\.\d 秒",
        r"plan\.dwg：正在读回译文图纸以便校验",
        r"plan\.dwg：译文图纸读回完成，用时 \d+\.\d 秒",
    ]
    positions = []
    for pattern in expected:
        index = next((i for i, line in enumerate(logs) if re.fullmatch(pattern, line)), None)
        assert index is not None, (pattern, logs)
        positions.append(index)
    assert positions == sorted(positions)

    steps = [(message.step_done, message.step_total) for message in messages if isinstance(message, ProgressMsg)]
    assert all(message.phase_index == 0 for message in messages if isinstance(message, ProgressMsg))
    assert [done for done, _ in steps] == sorted(done for done, _ in steps)
    assert steps[-1] == (_BATCH_PROGRESS_SCALE, _BATCH_PROGRESS_SCALE)


def test_dxf_is_read_without_conversion_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(process_artifacts, "APP_DATA_DIR", tmp_path / "app-data")
    source = tmp_path / "plan.dxf"
    source.write_text(_dxf_text("Door"), encoding="utf-8")
    converter = _CountingConverter()
    runner = CadTaskRunner(
        files=[SimpleNamespace(path=source, name=source.name, format="dxf")],
        source_root=tmp_path,
        output_dir=tmp_path / "results",
        converter=converter,
        translator=lambda texts, glossary: {"Door": "门"},
        options=CadPipelineOptions(),
    )
    logs = [message.message for message in _run(runner) if isinstance(message, LogMsg)]
    assert converter.calls == []
    assert not any("转换" in line for line in logs), logs


def test_dwg_stage_weights_give_conversions_their_share(tmp_path):
    assert sum(CAD_DWG_STAGE_WEIGHTS.values()) == 100
    assert sum(CAD_STAGE_WEIGHTS.values()) == 100
    assert CAD_DWG_STAGE_WEIGHTS == {"scan": 20, "match": 2, "translate": 55, "write": 15, "verify": 8}
    assert CAD_STAGE_WEIGHTS == {"scan": 8, "match": 2, "translate": 80, "write": 8, "verify": 2}

    source = tmp_path / "plan.dwg"
    source.write_text(_dxf_text("Door"), encoding="utf-8")
    progress: list[CadProgress] = []
    CadTranslationPipeline(
        converter=_CountingConverter(), translator=lambda texts, glossary: {"Door": "门"}, progress=progress.append
    ).translate_file(source, tmp_path / "plan_zh.dwg")

    percents = [item.percent for item in progress]
    assert percents == sorted(percents)
    by_message = {item.message: item.percent for item in progress if item.message}
    assert by_message["正在转换图纸格式（DWG → DXF）"] == 0
    converted = next(value for key, value in by_message.items() if key.startswith("图纸格式转换完成"))
    assert converted == 20
    assert by_message["正在将译文转换回 DWG 图纸"] == 77
    back = next(value for key, value in by_message.items() if key.startswith("已转换回 DWG 图纸"))
    assert back == 92
    assert by_message["正在读回译文图纸以便校验"] == 92
    read_back = next(value for key, value in by_message.items() if key.startswith("译文图纸读回完成"))
    assert read_back == 96
    assert percents[-1] == 100

    dxf = tmp_path / "plan.dxf"
    dxf.write_text(_dxf_text("Door"), encoding="utf-8")
    dxf_progress: list[CadProgress] = []
    CadTranslationPipeline(
        converter=None, translator=lambda texts, glossary: {"Door": "门"}, progress=dxf_progress.append
    ).translate_file(dxf, tmp_path / "plan_zh.dxf")
    scan_end = [item.percent for item in dxf_progress if item.stage == "scan"][-1]
    assert scan_end == 8
    assert dxf_progress[-1].percent == 100


def test_batch_progress_is_weighted_by_size_and_monotonic(tmp_path, monkeypatch):
    monkeypatch.setattr(process_artifacts, "APP_DATA_DIR", tmp_path / "app-data")
    big = tmp_path / "big.dwg"
    # A 1 MB drawing: a DXF text with a long comment record (code 999) in front.
    big.write_text("999\n" + "x" * (1024 * 1024 - 4) + "\n" + _dxf_text("Door"), encoding="utf-8")
    small = tmp_path / "small.dxf"
    small.write_text(_dxf_text("Window"), encoding="utf-8")
    same_size_dxf = tmp_path / "same.dxf"
    same_size_dxf.write_bytes(big.read_bytes())
    files = [SimpleNamespace(path=path, name=path.name, format=path.suffix[1:]) for path in (big, small, same_size_dxf)]
    runner = CadTaskRunner(
        files=files,
        source_root=tmp_path,
        output_dir=tmp_path / "results",
        converter=_CountingConverter(),
        translator=lambda texts, glossary: {text: f"{text}-译" for text in texts},
        options=CadPipelineOptions(skip_target_language=False),
    )
    big_mb = big.stat().st_size / (1024 * 1024)
    small_mb = small.stat().st_size / (1024 * 1024)
    raw = [0.2 + big_mb, 0.2 + small_mb / 5, 0.2 + big_mb / 5]
    assert runner._file_weights == pytest.approx([value / sum(raw) for value in raw])
    # The DWG takes far more of the bar than a small DXF, and more than a DXF of the same size.
    assert runner._file_weights[0] > 5 * runner._file_weights[1]
    assert runner._file_weights[0] > 2 * runner._file_weights[2]

    messages = _run(runner)
    progress = [message for message in messages if isinstance(message, ProgressMsg)]
    done = [message.step_done for message in progress]
    assert done == sorted(done), "进度不能倒退"
    assert {message.step_total for message in progress} == {_BATCH_PROGRESS_SCALE}
    assert done[-1] == _BATCH_PROGRESS_SCALE
    # When the second drawing starts, the bar stands at the first drawing's share.
    first_of_second = next(message for message in progress if message.phase_name.endswith("第 2 / 3 个文件"))
    assert first_of_second.step_done == pytest.approx(runner._file_weights[0] * _BATCH_PROGRESS_SCALE, abs=1)


def test_failed_drawing_advances_to_its_end_and_never_goes_back(tmp_path):
    good = tmp_path / "good.dxf"
    good.write_text(_dxf_text("Door"), encoding="utf-8")
    runner = CadTaskRunner(
        files=[SimpleNamespace(path=tmp_path / "x.dwg", name="x.dwg", format="dwg"), SimpleNamespace(path=good, name=good.name, format="dxf")],
        source_root=tmp_path,
        output_dir=tmp_path / "results",
        converter=_CountingConverter(),
        translator=lambda texts, glossary: {},
        options=CadPipelineOptions(),
    )
    runner._put_batch_progress(0, 0.5, "读取文件")
    runner._put_batch_progress(0, 1.0)
    runner._put_batch_progress(0, 0.2)  # a late, smaller report must not pull the bar back
    values = [message.step_done for message in list(runner._queue.queue) if isinstance(message, ProgressMsg)]
    assert values == sorted(values)
    assert values[-1] == values[-2] == round(runner._file_weights[0] * _BATCH_PROGRESS_SCALE)


# ---------------------------------------------------------------------------
# Evidence DXF copies: kept only for a drawing that fails.
# ---------------------------------------------------------------------------


class _LossyReadBackConverter(_CountingConverter):
    """The read-back after writing loses a text, so verification fails."""

    def dwg_to_dxf(self, source: Path, destination: Path) -> Path:
        super().dwg_to_dxf(source, destination)
        if "_staged" in Path(source).name:
            destination.write_text(_dxf_text("Door").replace("0\nTEXT\n1\nDoor\n", ""), encoding="utf-8")
        return destination


def test_verified_drawing_drops_its_evidence_dxf_copies(tmp_path, monkeypatch):
    monkeypatch.setattr(process_artifacts, "APP_DATA_DIR", tmp_path / "app-data")
    source = tmp_path / "plan.dwg"
    source.write_text(_dxf_text("Door"), encoding="utf-8")
    runner = CadTaskRunner(
        files=[SimpleNamespace(path=source, name=source.name, format="dwg")],
        source_root=tmp_path,
        output_dir=tmp_path / "results",
        converter=_CountingConverter(),
        translator=lambda texts, glossary: {"Door": "门"},
        options=CadPipelineOptions(),
    )
    messages = _run(runner)
    done = next(message for message in messages if hasattr(message, "file_results"))
    entry = done.file_results[0]
    assert entry["status"] == "succeeded"
    assert entry["work_evidence_dir"] == ""
    for root in (tmp_path / "results", tmp_path / "app-data"):
        assert not list(root.rglob("*.cad-work")), list(root.rglob("*.cad-work"))
        assert not list(root.rglob("source.dxf")) and not list(root.rglob("translated.dxf"))
    for key in ("manifest_path", "report_path", "summary_path"):
        assert Path(entry[key]).is_file(), key
    # 「另存一份 DXF」照旧保留。
    assert Path(entry["work_dxf"]).is_file()


def test_drawing_that_fails_verification_keeps_its_evidence(tmp_path):
    source = tmp_path / "plan.dwg"
    source.write_text(_dxf_text("Door"), encoding="utf-8")
    output = tmp_path / "out" / "plan_zh.dwg"
    with pytest.raises(Exception, match="文字数量不一致"):
        CadTranslationPipeline(
            converter=_LossyReadBackConverter(), translator=lambda texts, glossary: {"Door": "门"}
        ).translate_file(source, output)
    evidence = output.parent / "plan_zh.cad-work"
    assert (evidence / "source.dxf").is_file()
    assert (evidence / "translated.dxf").is_file()
    assert not output.exists()


def test_empty_evidence_folder_is_not_moved_into_review(tmp_path):
    runner = CadTaskRunner(
        files=[], source_root=tmp_path, output_dir=tmp_path / "results",
        converter=None, translator=lambda texts, glossary: {}, options=CadPipelineOptions(),
    )
    output = runner._publication_dir / "plan_zh.dxf"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_dxf_text(), encoding="utf-8")
    (output.parent / "plan_zh.cad-work").mkdir()
    manifest = output.with_suffix(".dxf.manifest.json")
    manifest.write_text("{}", encoding="utf-8")
    result = SimpleNamespace(output=output, manifest=manifest, report=manifest, summary=None, work_dxf=None)
    runner._relocate_file_artifacts(result)
    assert result.work_evidence_dir is None
    assert not (output.parent / "plan_zh.cad-work").exists()
    assert not list(runner._review_dir.rglob("*.cad-work"))
