"""Guards that keep the translation pages built from one template.

New translation pages kept drifting from the existing ones (layout, copy,
flow) because each was written by hand. The rule now is: every page listed
under 「翻译」in the sidebar is a thin wrapper over ``workspace.ts`` and
differs only through the per-surface tables there. See
docs/ADDING_A_TRANSLATION_PAGE.md.
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
    declared = re.search(r"export type Surface = ([^;]+);", source)
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
    """Record<Surface, …> makes TypeScript list every table a new surface must fill.

    A Partial<Record<Surface, …>> or a plain object keyed by surface names
    would let a new page silently fall back to another page's copy.
    """
    source = WORKSPACE.read_text(encoding="utf-8")
    assert "Partial<Record<Surface" not in source
    tables = re.findall(r"^const (\w+): Record<Surface, ", source, flags=re.M)
    assert len(tables) >= 10, tables


def test_task_center_knows_every_translation_page():
    """CAD tasks once showed up in 任务中心 as「TM 清洗」with no way back to the page."""
    source = (VIEWS / "tasks.ts").read_text(encoding="utf-8")
    assert "const SURFACE_ICON: Record<TaskSurface, IconName>" in source
    assert "const SURFACE_TASK_LABEL: Record<TaskSurface, string>" in source
    block = source[source.index("const WORKSPACE_SURFACES"):]
    block = block[:block.index(";")]
    for surface in _translate_items():
        assert f'"{surface}"' in block, f"任务中心不能从 {surface} 任务打开工作区"
