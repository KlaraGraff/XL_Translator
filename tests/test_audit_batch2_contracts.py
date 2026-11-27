"""整体审核第二批小问题的界面约定（2026-11-27）。"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_path_bar_hands_other_types_to_their_own_page() -> None:
    source = _read("ui/src/views/workspace.ts")
    body = source.split("function submitSourcePath(surface: Surface): void {", 1)[1].split("\n}\n", 1)[0]
    assert "ownerSurfaceOf(path)" in body
    assert "owner !== surface" in body and "dispatchPaths([path])" in body
    assert "void runScan(surface);" in body
    # 回车和「重新读取」走同一个入口。
    assert source.count("submitSourcePath(surface)") >= 2


def test_dispatch_reports_files_of_unknown_types() -> None:
    source = _read("ui/src/drop-dispatch.ts")
    assert "function unrecognizedFiles(" in source
    assert "没有放入：{1}。" in source
    # 文件夹里的杂文件不算，只说单独放进来的。
    assert 'item.kind === "file"' in source.split("function unrecognizedFiles(", 1)[1].split("\n}\n", 1)[0]


def test_scan_toast_does_not_follow_the_user_to_another_page() -> None:
    source = _read("ui/src/views/workspace.ts")
    assert "if (here) scanToasts.set(surface, showToast({ message: toastMessage }));" in source
    # 离开这一页时收掉这一页自己弹的扫描提示。
    unmount = source.split("export function unmountWorkspace(surface: Surface): void {", 1)[1].split("\n}\n", 1)[0]
    assert "scanToasts.get(surface)?.remove();" in unmount
    assert "if (!background || here) showToast({ message: toastMessage });" not in source


def test_word_ready_note_counts_what_will_be_translated() -> None:
    source = _read("ui/src/views/workspace.ts")
    assert "f.translatable_count ?? f.paragraph_count" in source


def test_empty_files_say_there_was_nothing_to_translate() -> None:
    ui = _read("ui/src/views/workspace.ts")
    assert "item.source_text_count === 0" in ui
    assert "已生成 · 没有需要翻译的文字" in ui
    # 跑完后文件表上的状态先读逐文件进度（file-progress.ts），两处都要说。
    assert "result.source_text_count === 0" in _read("ui/src/file-progress.ts")
    for runner in ("core/task_runner.py", "core/word_task_runner.py"):
        assert '"source_text_count"' in _read(runner), runner


def test_stopped_excel_task_does_not_show_the_planned_model_count() -> None:
    source = _read("ui/src/views/tasks.ts")
    excel = source.split('if (task.surface === "excel") {', 1)[1].split('if (task.surface === "word") {', 1)[0]
    assert '!["stopped", "error", "interrupted"].includes(task.state)' in excel
    assert '"unstarted_text_count"' in excel
