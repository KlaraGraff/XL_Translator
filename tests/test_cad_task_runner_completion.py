from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

from core.cad_task_runner import CadTaskRunner
from core.cad_translation import CadPipelineOptions


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


def test_cad_runner_uses_fresh_output_root_and_checkpoint_resume(tmp_path):
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
