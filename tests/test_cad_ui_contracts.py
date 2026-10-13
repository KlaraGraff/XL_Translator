"""Source contracts for the CAD page.

CAD used to hand-build its own page next to the shared Excel/Word/PDF
workspace, and kept drifting from it in layout, copy and flow. It is now one
more surface of ``workspace.ts``; these checks keep it that way. The page
behaviour itself is exercised in a browser walkthrough (see the PR notes).
"""

import re
from pathlib import Path


UI = Path(__file__).parents[1] / "ui" / "src"
CAD_VIEW = UI / "views" / "cad.ts"
WORKSPACE_VIEW = UI / "views" / "workspace.ts"


def test_cad_view_is_a_thin_wrapper_over_the_shared_workspace():
    source = CAD_VIEW.read_text(encoding="utf-8")
    assert 'mountWorkspace(container, params, "cad")' in source
    assert 'unmountWorkspace("cad")' in source
    # No hand-built page: the wrapper must not render or style anything itself.
    for forbidden in ("createButton", "replaceChildren", "setInterval", "className", "./cad.css"):
        assert forbidden not in source
    assert not (UI / "views" / "cad.css").exists()


def test_cad_is_a_workspace_surface():
    source = WORKSPACE_VIEW.read_text(encoding="utf-8")
    assert 'export type Surface = "excel" | "word" | "pdf" | "cad";' in source
    assert 'cad: t("CAD 图纸翻译")' in source


def test_every_cad_switch_is_persisted_and_reaches_the_task_payload():
    """A switch on screen must both save its setting and change the task.

    The old page showed switches that were hard-coded to ``true`` in the
    payload, so flipping them did nothing.
    """
    source = WORKSPACE_VIEW.read_text(encoding="utf-8")
    block = source[source.index("const CAD_TOGGLES"):source.index("const TOGGLES:")]
    keys = []
    for line in block.splitlines():
        if "key:" not in line:
            continue
        key = re.search(r'key: "(\w+)"', line).group(1)
        assert 'pathKind: "output"' in line, line
        keys.append(key)
    assert keys, "CAD toggles not found"
    payload = source[source.index("function buildPayload"):source.index("async function startTask")]
    for key in keys:
        assert f'st.toggles.get("{key}")' in payload, key


def test_document_workspaces_show_scan_state_in_topbar_and_task_list():
    """The shared renderer must not look idle during a scan."""
    source = WORKSPACE_VIEW.read_text(encoding="utf-8")
    # Choosing a source from the picker scans straight away, on every surface.
    assert "await runScan(surface);" in source
    assert "scanBusy[surface] = true;" in source
    assert '扫描中…' in source
    update_topbar = source[source.index('function updateTopbar'):source.index('function runningStatusMeta')]
    assert 'scanBusy[surface]' in update_topbar
    table_card = source[source.index('function buildTableCard'):source.index('function buildSkipNoticeRow')]
    assert 'scanBusy[surface]' in table_card


def test_only_pausable_surfaces_get_a_pause_button():
    source = WORKSPACE_VIEW.read_text(encoding="utf-8")
    foot = source[source.index("function buildRightFoot"):source.index("// 续译（接着上次继续）")]
    assert 'const pausable = surface === "pdf" || surface === "cad";' in foot
    assert "onPause: pausable ?" in foot


def test_cad_fit_scales_follow_the_target_language():
    """字号比例按目标语言分两套存：中文一套、其他语言一套，发给后端的是当前目标语言那套。"""
    source = WORKSPACE_VIEW.read_text(encoding="utf-8")
    helper = source[source.index("function cadFitKeys"):source.index("function cadChoice")]
    assert '"fit_default_scale_zh"' in helper and '"fit_min_scale_zh"' in helper
    assert "defaultFallback: 0.9" in helper and "defaultFallback: 0.7" in helper
    payload = source[source.index("function buildPayload"):source.index("async function startTask")]
    assert "cadFitScales(st.targetLang)" in payload
    fields = source[source.index("function buildCadScaleFields"):source.index("function buildCadLockFields")]
    assert "cadFitKeys(st.targetLang)" in fields
    assert '"fit_default_scale"' not in fields and '"fit_min_scale"' not in fields
