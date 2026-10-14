"""Guards that keep the translation pages built from one template.

New translation pages kept drifting from the existing ones (layout, copy,
flow) because each was written by hand. The rule now is: every page listed
under 「翻译」in the sidebar is a thin wrapper over ``workspace.ts`` and
differs only through the per-surface tables there. See
docs/ADDING_A_TRANSLATION_PAGE.md.

These checks cover the four built-in pages.  Pages registered at run time
(``ext:<id>``) join the sidebar after the descriptors load and are drawn by
the same template from their descriptor.
"""

import re
from pathlib import Path

UI = Path(__file__).parents[1] / "ui" / "src"
VIEWS = UI / "views"
WORKSPACE = VIEWS / "workspace.ts"


def _translate_items() -> list[str]:
    shell = (UI / "shell.ts").read_text(encoding="utf-8")
    block = shell[shell.index("const TRANSLATE_ITEMS"):shell.index("const RESOURCE_ITEMS")]
    return re.findall(r'id: "(\w+)"', block)


def test_every_translation_page_is_a_workspace_surface():
    items = _translate_items()
    assert items, "sidebar translation items not found"
    source = WORKSPACE.read_text(encoding="utf-8")
    declared = re.search(r"export type BuiltinSurface = ([^;]+);", source)
    assert declared, "Surface type not found"
    surfaces = re.findall(r'"(\w+)"', declared.group(1))
    assert sorted(items) == sorted(surfaces), (
        "每个侧栏「翻译」入口都必须是 workspace.ts 的一个 Surface，不要另写页面"
    )


def test_translation_views_are_thin_wrappers():
    for surface in _translate_items():
        view = VIEWS / f"{surface}.ts"
        source = view.read_text(encoding="utf-8")
        assert f'mountWorkspace(container, params, "{surface}")' in source, view.name
        assert f'unmountWorkspace("{surface}")' in source, view.name
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("//"))
        # 薄封装不渲染、不设样式、不自己起计时器。
        for forbidden in ("createButton", "createElement", "replaceChildren", "setInterval", "className", ".css"):
            assert forbidden not in code, f"{view.name} uses {forbidden}"
        assert len(code.strip().splitlines()) <= 12, f"{view.name} is no longer a thin wrapper"
        assert not (VIEWS / f"{surface}.css").exists(), f"{surface}.css: 样式写进 workspace.css 或公共样式"


def test_per_surface_tables_cover_every_surface():
    """Record<BuiltinSurface, …> makes TypeScript list every table a new built-in page must fill.

    A Partial<Record<…>> or a plain object keyed by surface names would let a
    new page silently fall back to another page's copy.  Run-time pages have
    no entry in these tables: each table is read through one accessor that
    asks the descriptor for an ``ext:`` key, never indexed directly.
    """
    source = WORKSPACE.read_text(encoding="utf-8")
    assert "Partial<Record<Surface" not in source and "Partial<Record<BuiltinSurface" not in source
    assert "Record<Surface," not in source, "运行时注册的类型进不了编译期的表，按 surface 存的运行时状态用 Map"
    tables = re.findall(r"^const (\w+): Record<BuiltinSurface, ", source, flags=re.M)
    assert len(tables) >= 9, tables
    for table in tables:
        reads = re.findall(rf"\b{table}\[", source)
        assert len(reads) == 1, f"{table} 只能经它的访问函数读取，找到 {len(reads)} 处直接下标"


def test_per_surface_runtime_state_is_created_on_demand():
    source = WORKSPACE.read_text(encoding="utf-8")
    assert "const states = new Map<Surface, SurfaceState>();" in source
    assert "export function stateOf(surface: Surface): SurfaceState" in source
    assert not re.search(r"\bstates\[", source)
    pdf_pages = (VIEWS / "workspace-pdf-pages.ts").read_text(encoding="utf-8")
    assert not re.search(r"\bstates\[", pdf_pages)


def test_extension_pages_reuse_the_workspace_template():
    view = (VIEWS / "extension.ts").read_text(encoding="utf-8")
    assert "mountWorkspace(container, params, surface)" in view
    assert "unmountWorkspace(surface)" in view
    app = (UI / "app.ts").read_text(encoding="utf-8")
    assert "registerView(item.key, extensionView(item.key))" in app


def test_task_center_knows_every_translation_page():
    """CAD tasks once showed up in 任务中心 as「TM 清洗」with no way back to the page."""
    source = (VIEWS / "tasks.ts").read_text(encoding="utf-8")
    assert "const SURFACE_ICON: Record<TaskSurface, IconName>" in source
    assert "const SURFACE_TASK_LABEL: Record<TaskSurface, string>" in source
    block = source[source.index("const WORKSPACE_SURFACES"):]
    block = block[:block.index(";")]
    for surface in _translate_items():
        assert f'"{surface}"' in block, f"任务中心不能从 {surface} 任务打开工作区"


def test_auto_detect_and_shape_switches_live_only_in_settings():
    """maple 2026-10-13：「自动识别场景」只在设置页的提示词设置里出现，各翻译页右栏不再放；
    Excel 的「翻译文本框和形状」去掉，文本框和形状里的字始终翻译。"""
    source = WORKSPACE.read_text(encoding="utf-8")
    toggles = source[source.index("const EXCEL_TOGGLES"):source.index("const TOGGLES:")]
    assert "smart_context" not in toggles
    assert "自动识别场景" not in toggles
    assert "translate_shapes" not in toggles
    assert "translateShapes" not in source
    settings_view = (VIEWS / "settings.ts").read_text(encoding="utf-8")
    assert 'saveSettingPath("pdf.smart_context", checked)' in settings_view
    assert "smart_context: checked" in settings_view


def test_prompt_settings_explain_composition_in_a_dialog():
    """maple 2026-10-13：提示词设置标题旁的「?」单独弹窗说明组成，四个页签都有；
    内容跟着已上线的流程走：Excel/Word 带「本批涉及的词条」，PDF 按翻译方式分组。"""
    settings_view = (VIEWS / "settings.ts").read_text(encoding="utf-8")
    assert 'card.append(promptCardHead("pdf"), body);' in settings_view
    assert "card.append(promptCardHead(surface, tools), body);" in settings_view
    assert 'tcHead(t("提示词设置")' not in settings_view.replace('const head = tcHead(t("提示词设置"), tools);', "")
    anatomy = (VIEWS / "prompt-anatomy.ts").read_text(encoding="utf-8")
    assert 'el("div", "modal prompt-anatomy-modal")' in anatomy
    assert 't("本批涉及的词条")' in anatomy
    for route in ("整页图像翻译", "转成 Word", "原位替换"):
        assert f't("{route} · 发送给' in anatomy
