from pathlib import Path


CAD_VIEW = Path(__file__).parents[1] / "ui" / "src" / "views" / "cad.ts"


def test_cad_task_list_uses_checkbox_only_and_rescan_keeps_sources_available():
    source = CAD_VIEW.read_text(encoding="utf-8")
    # The checkbox is the only per-file selection control. There must be no
    # row action that mutates paths or hides an item from the scan result.
    assert '["选择", "文件", "格式", "文字", "状态"].forEach' in source
    assert 'action("移除"' not in source
    assert 'paths = paths.filter((p) => p !== item.path)' not in source
    # A fresh scan replaces the displayed list from the source paths.
    assert 'scanItems = response.items; skipped = response.skipped;' in source
