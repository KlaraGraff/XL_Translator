from __future__ import annotations

import time
from types import SimpleNamespace
from pathlib import Path

from core.cad_task_runner import CAD_PAUSED_IN_FILE_STATUS, CadTaskRunner
from core.cad_translation import CadPipelineOptions
import core.process_artifacts as process_artifacts


class _Converter:
    def dwg_to_dxf(self, source: Path, destination: Path) -> Path:
        destination.write_bytes(source.read_bytes())
        return destination

    def dxf_to_dwg(self, source: Path, destination: Path) -> Path:
        destination.write_bytes(source.read_bytes())
        return destination


def _run(runner: CadTaskRunner) -> list[object]:
    runner.start()
    messages: list[object] = []
    while runner.needs_poll():
        message = runner.get_message(timeout=1)
        if message is not None:
            messages.append(message)
    return messages


def test_cad_runner_uses_fresh_output_root_and_checkpoint_resume(tmp_path, monkeypatch):
    monkeypatch.setattr(process_artifacts, "APP_DATA_DIR", tmp_path / "app-data")
    source = tmp_path / "drawing.dxf"
    source.write_text("0\nTEXT\n1\nDoor\n", encoding="utf-8")
    item = SimpleNamespace(path=source, name=source.name, format="dxf")
    kwargs = dict(
        files=[item],
        source_root=tmp_path,
        output_dir=tmp_path / "results",
        converter=_Converter(),
        translator=lambda texts, glossary: {"Door": "门"},
        options=CadPipelineOptions(),
    )

    first = CadTaskRunner(**kwargs)
    first_messages = _run(first)
    first_done = next(message for message in first_messages if hasattr(message, "file_results"))
    first_output = Path(first_done.file_results[0]["output_path"])
    first_root = first_output.parent

    second = CadTaskRunner(**kwargs, resume_output_dir=first_root)
    second_messages = _run(second)
    second_done = next(message for message in second_messages if hasattr(message, "file_results"))
    second_output = Path(second_done.file_results[0]["output_path"])

    assert second_done.file_results[0]["resumed"] is True
    assert second_output.exists()
    assert second_output.parent != first_root
    assert first_output.read_bytes() == second_output.read_bytes()
    # The deliverable folder contains CAD output only; diagnostics and the
    # checkpoint are owned by the shared process store.
    assert not list(first_root.rglob(".cad-checkpoint.json"))
    assert not (first_root / "manifest.json").exists()
    assert Path(first_done.report_path).is_file()
    assert str(first_done.report_path).startswith(str(tmp_path / "app-data"))
    # A run without CAD work files records an empty evidence path. Resuming must not read
    # that as "." and copy the current directory (in the app that can be /).
    evidence = second_done.file_results[0].get("work_evidence_dir") or ""
    assert not evidence or Path(evidence).name.endswith(".cad-work") or ".cad-work_" in Path(evidence).name
    assert not any(path.name == "tests" for path in (tmp_path / "app-data").rglob("tests"))


def test_cad_runner_preserves_failed_process_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(process_artifacts, "APP_DATA_DIR", tmp_path / "app-data")
    source = tmp_path / "broken.dxf"
    source.write_text("0\nTEXT\n1\n", encoding="utf-8")  # incomplete pair: converter/pipeline must fail safely
    item = SimpleNamespace(path=source, name=source.name, format="dxf")
    runner = CadTaskRunner(
        files=[item], source_root=tmp_path, output_dir=tmp_path / "results",
        converter=_Converter(), translator=lambda texts, glossary: {}, options=CadPipelineOptions(),
    )
    messages = _run(runner)
    done = next(message for message in messages if hasattr(message, "file_results"))
    assert done.file_results[0]["status"] == "failed"
    assert not list((tmp_path / "results").glob("*.dxf"))
    process_dirs = process_artifacts.find_process_dirs(tmp_path / "results", kind="cad")
    assert process_dirs
    assert (process_dirs[0] / ".process-run.json").is_file()
    assert any(path.name == "source.dxf" for path in process_dirs[0].rglob("source.dxf"))


def test_cad_runner_uses_target_language_in_output_name(tmp_path):
    source = tmp_path / "drawing.dxf"
    source.write_text("0\nTEXT\n1\n门\n", encoding="utf-8")
    item = SimpleNamespace(path=source, name=source.name, format="dxf")
    runner = CadTaskRunner(
        files=[item],
        source_root=tmp_path,
        output_dir=tmp_path / "results",
        converter=_Converter(),
        translator=lambda texts, glossary: {"门": "Door"},
        options=CadPipelineOptions(source_lang="zh", target_lang="en"),
    )
    messages = _run(runner)
    done = next(message for message in messages if hasattr(message, "file_results"))
    assert Path(done.file_results[0]["output_path"]).name == "drawing_英文.dxf"


def _stopping_runner(tmp_path, sources: list[Path], stop_on: str) -> CadTaskRunner:
    holder: dict[str, CadTaskRunner] = {}

    def translator(texts, glossary):
        if stop_on in texts:
            holder["runner"].stop()
        return {text: f"{text}-译" for text in texts}

    runner = CadTaskRunner(
        files=[SimpleNamespace(path=path, name=path.name, format="dxf") for path in sources],
        source_root=tmp_path,
        output_dir=tmp_path / "results",
        converter=_Converter(),
        translator=translator,
        options=CadPipelineOptions(),
    )
    holder["runner"] = runner
    return runner


def test_cad_stop_before_any_drawing_does_not_promise_a_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(process_artifacts, "APP_DATA_DIR", tmp_path / "app-data")
    source = tmp_path / "a.dxf"
    source.write_text("0\nTEXT\n1\nDoor\n", encoding="utf-8")
    messages = _run(_stopping_runner(tmp_path, [source], stop_on="Door"))
    stopped = next(message for message in messages if type(message).__name__ == "StoppedMsg")
    assert not any(item.get("status") in {"succeeded", "needs_review"} for item in stopped.files)
    assert "断点" not in stopped.message
    assert "尚无图纸完成翻译" in stopped.message


def test_cad_stop_after_a_finished_drawing_keeps_the_checkpoint_message(tmp_path, monkeypatch):
    monkeypatch.setattr(process_artifacts, "APP_DATA_DIR", tmp_path / "app-data")
    first = tmp_path / "a.dxf"
    first.write_text("0\nTEXT\n1\nDoor\n", encoding="utf-8")
    second = tmp_path / "b.dxf"
    second.write_text("0\nTEXT\n1\nWindow\n", encoding="utf-8")
    messages = _run(_stopping_runner(tmp_path, [first, second], stop_on="Window"))
    stopped = next(message for message in messages if type(message).__name__ == "StoppedMsg")
    assert any(item.get("status") in {"succeeded", "needs_review"} for item in stopped.files)
    assert "已翻译的图纸均已保留" in stopped.message


def _batched_runner(tmp_path) -> tuple[CadTaskRunner, list[str]]:
    """One drawing whose translation arrives in two batches.

    The translator presses 暂停 while the first batch is in flight, the way a
    user would on a single big drawing.  Before the in-file pause existed the
    runner only checked for a pause between drawings, so with one drawing the
    task simply ran to completion.
    """
    holder: dict[str, CadTaskRunner] = {}
    sent: list[str] = []

    def translator(texts, glossary, progress=None, should_stop=None):
        sent.append("batch-1")
        holder["runner"].pause()
        progress(1, 2)  # the pause gate blocks here until resume / stop
        if should_stop is not None and should_stop():
            return {}
        sent.append("batch-2")
        progress(2, 2)
        return {text: f"{text}-译" for text in texts}

    source = tmp_path / "single.dxf"
    source.write_text("0\nTEXT\n1\nDoor\n", encoding="utf-8")
    runner = CadTaskRunner(
        files=[SimpleNamespace(path=source, name=source.name, format="dxf")],
        source_root=tmp_path,
        output_dir=tmp_path / "results",
        converter=_Converter(),
        translator=translator,
        options=CadPipelineOptions(),
    )
    holder["runner"] = runner
    return runner, sent


def _run_until_paused(runner: CadTaskRunner, on_paused) -> list[object]:
    runner.start()
    messages: list[object] = []
    handled = False
    while runner.needs_poll():
        message = runner.get_message(timeout=1)
        if message is None:
            continue
        messages.append(message)
        if not handled and getattr(message, "phase_desc", "") == CAD_PAUSED_IN_FILE_STATUS:
            handled = True
            on_paused()
    assert handled, "single-drawing pause never took effect"
    return messages


def test_cad_pause_takes_effect_inside_a_single_drawing(tmp_path, monkeypatch):
    monkeypatch.setattr(process_artifacts, "APP_DATA_DIR", tmp_path / "app-data")
    runner, sent = _batched_runner(tmp_path)
    seen_while_paused: list[list[str]] = []

    def on_paused() -> None:
        # Give the runner thread a moment: a gate that does not hold would race ahead here.
        time.sleep(0.3)
        seen_while_paused.append(list(sent))
        runner.resume()

    messages = _run_until_paused(runner, on_paused)
    assert seen_while_paused == [["batch-1"]]
    assert sent == ["batch-1", "batch-2"]
    done = next(message for message in messages if hasattr(message, "file_results"))
    assert done.file_results[0]["status"] in {"succeeded", "needs_review"}
    assert Path(done.file_results[0]["output_path"]).is_file()


def test_cad_stop_while_paused_inside_a_drawing_sends_no_new_requests(tmp_path, monkeypatch):
    monkeypatch.setattr(process_artifacts, "APP_DATA_DIR", tmp_path / "app-data")
    runner, sent = _batched_runner(tmp_path)
    messages = _run_until_paused(runner, runner.stop)
    assert sent == ["batch-1"]
    stopped = next(message for message in messages if type(message).__name__ == "StoppedMsg")
    assert all(item.get("status") == "stopped" for item in stopped.files)


def test_cad_issues_say_needs_review_like_the_file_status(tmp_path, monkeypatch):
    """每条 CAD 问题都让图纸状态变成 needs_review；问题本身也得明说 severity。

    任务中心只认 severity 判断待办。宣传视频回放里，排版放不下的 3 处没带它，
    任务徽章写「已完成」，同一页的文件状态却是「已生成 · 需复核」。
    """
    import core.cad_task_runner as cad_task_runner

    monkeypatch.setattr(process_artifacts, "APP_DATA_DIR", tmp_path / "app-data")
    original = cad_task_runner.CadTranslationPipeline.translate_file

    def crowded(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        result.stats.layout_unresolved = 3
        return result

    monkeypatch.setattr(cad_task_runner.CadTranslationPipeline, "translate_file", crowded)
    source = tmp_path / "drawing.dxf"
    source.write_text("0\nTEXT\n1\nDoor\n", encoding="utf-8")
    runner = CadTaskRunner(
        files=[SimpleNamespace(path=source, name=source.name, format="dxf")],
        source_root=tmp_path,
        output_dir=tmp_path / "results",
        converter=_Converter(),
        translator=lambda texts, glossary: {"Door": "门"},
        options=CadPipelineOptions(),
    )
    done = next(message for message in _run(runner) if hasattr(message, "file_results"))

    assert done.file_results[0]["status"] == "needs_review"
    overflow = [issue for issue in done.issues if issue.get("reason") == "layout_overflow"]
    assert overflow and all(issue["severity"] == "needs_review" for issue in done.issues)


def test_cad_runner_reports_kept_texts_next_to_translated(tmp_path, monkeypatch):
    # 任务记录「已翻译文字」比页面「待翻译」少的那些（模型确认已是目标语言）单独记成「保留原文」。
    monkeypatch.setattr(process_artifacts, "APP_DATA_DIR", tmp_path / "app-data")
    source = tmp_path / "plan.dxf"
    source.write_text("0\nTEXT\n1\nPLAN DE MASSE\n0\nTEXT\n1\nSCALE 1:100\n", encoding="utf-8")
    item = SimpleNamespace(path=source, name=source.name, format="dxf")
    runner = CadTaskRunner(
        files=[item], source_root=tmp_path, output_dir=tmp_path / "results", converter=_Converter(),
        translator=lambda texts, glossary: {"PLAN DE MASSE": "SITE PLAN"},
        language_translator=lambda texts, glossary: {"SCALE 1:100": ("SCALE 1:100", "en")},
        options=CadPipelineOptions(target_lang="en"),
    )
    done = next(message for message in _run(runner) if hasattr(message, "file_results"))
    assert done.kpi["translated_entities"] == 1
    assert done.kpi["kept_entities"] == 1
    assert done.file_results[0]["stats"]["kept_units"] == 1
