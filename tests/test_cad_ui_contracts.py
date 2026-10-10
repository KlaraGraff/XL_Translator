from pathlib import Path


CAD_VIEW = Path(__file__).parents[1] / "ui" / "src" / "views" / "cad.ts"
WORKSPACE_VIEW = Path(__file__).parents[1] / "ui" / "src" / "views" / "workspace.ts"


def test_cad_task_list_uses_checkbox_only_and_rescan_keeps_sources_available():
    source = CAD_VIEW.read_text(encoding="utf-8")
    # The checkbox is the only per-file selection control. There must be no
    # row action that mutates paths or hides an item from the scan result.
    assert '["选择", "文件", "格式", "文字", "状态"].forEach' in source
    assert 'action("移除"' not in source
    assert 'paths = paths.filter((p) => p !== item.path)' not in source
    # A fresh scan replaces the displayed list from the source paths.
    assert 'scanItems = response.items; skipped = response.skipped;' in source


def test_cad_scan_flow_matches_shared_workspace_feedback():
    """CAD should expose the same scan lifecycle as the document workspaces.

    Keep this as a source contract because the scan request is initiated from
    the Tauri dialog and is awkward to exercise reliably in a headless browser.
    The assertions protect the user-visible transitions: browsing starts a
    scan, stale rows disappear before a slow scan, and the topbar uses the
    shared status wording.
    """
    source = CAD_VIEW.read_text(encoding="utf-8")
    assert 'if (scanning) return "扫描中…"' in source
    assert 'return "已扫描 · 待开始"' in source
    # Selecting a source from the native picker must start scanning without a
    # second click on the Scan button.
    assert "render(); await startOrScan();" in source
    # Clear the previous result before the request begins; this is essential
    # for long CAD scans so the old list cannot look like a frozen UI.
    assert "scanning = true; scanItems = []; skipped = []; selected.clear();" in source
    assert "finally { scanning = false; render(); }" in source


def test_document_workspaces_show_scan_state_in_topbar_and_task_list():
    """The shared Excel/Word/PDF renderer must not look idle during a scan."""
    source = WORKSPACE_VIEW.read_text(encoding="utf-8")
    # The source picker already invokes runScan; retain a contract for that
    # behaviour while the scan feedback is being shared with CAD.
    assert "await runScan(surface);" in source
    assert "scanBusy[surface] = true;" in source
    # Both the topbar and the list need an explicit in-flight branch. Checking
    # for the label and scanBusy branch prevents a slow scan from presenting
    # the previous result as if it were still current.
    assert '扫描中…' in source
    update_topbar = source[source.index('function updateTopbar'):source.index('function runningStatusMeta')]
    assert 'scanBusy[surface]' in update_topbar
    table_card = source[source.index('function buildTableCard'):source.index('function buildSkipNoticeRow')]
    assert 'scanBusy[surface]' in table_card
