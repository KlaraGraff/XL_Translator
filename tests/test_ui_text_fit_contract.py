"""界面文字放不下时只有两种排法（docs/UI_TEXT_FIT.md）。

名称（按钮、标签、标题、菜单、下拉选项、提示、报错）一律写全；内容（文件名、路径、任务名……）
才可以单行截断，而且统一走 app.css 的 ``.fit-clip``：text-fit.ts 在鼠标停上去时补上全文提示。
以前全软件有 40 处各写各的 ``text-overflow: ellipsis``，名称和内容不分，不少没有悬停提示。
这里守住「只有 .fit-clip 一处」：新加的截断要么是内容（加 FIT_CLIP），要么改成折行。
界面上实际放不放得下由 PR 检查里的 scripts/ui_text_fit_check.mjs 逐页去看。
"""

import re
from pathlib import Path

UI = Path(__file__).parents[1] / "ui" / "src"
ELLIPSIS = re.compile(r"text-overflow\s*:\s*ellipsis|textOverflow\s*=\s*[\"']ellipsis", re.IGNORECASE)
LINE_CLAMP = re.compile(r"line-clamp|lineClamp", re.IGNORECASE)


def _sources():
    for path in sorted(UI.rglob("*")):
        if path.suffix in {".css", ".ts"} and path.is_file():
            yield path.relative_to(UI).as_posix(), path.read_text(encoding="utf-8")


def _code_lines(source: str):
    """去掉 CSS / TS 注释后的行（注释里提到 ellipsis 不算）。"""
    without_blocks = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), source, flags=re.DOTALL)
    for number, line in enumerate(without_blocks.splitlines(), 1):
        yield number, re.sub(r"(?<![:\"'])//.*$", "", line)


def test_ellipsis_only_in_fit_clip():
    hits = []
    for rel, source in _sources():
        for number, line in _code_lines(source):
            if ELLIPSIS.search(line):
                hits.append(f"{rel}:{number}: {line.strip()}")
    allowed = [hit for hit in hits if hit.startswith("styles/app.css:") and ".fit-clip {" in hit]
    offenders = [hit for hit in hits if hit not in allowed]
    assert len(allowed) == 1, "app.css 里应当正好有一条 .fit-clip 规则写省略号：\n" + "\n".join(hits)
    assert not offenders, (
        "名称不截断（放不下就折行）；内容要截断请给元素加 FIT_CLIP（text-fit.ts），不要另写省略号：\n"
        + "\n".join(offenders)
    )


def test_no_multi_line_clamp():
    offenders = [
        f"{rel}:{number}: {line.strip()}"
        for rel, source in _sources()
        for number, line in _code_lines(source)
        if LINE_CLAMP.search(line)
    ]
    assert not offenders, "多行截断会把名称和说明藏起来，改成写全：\n" + "\n".join(offenders)


def test_hover_full_text_is_installed():
    app = (UI / "app.ts").read_text(encoding="utf-8")
    assert "installTextFit();" in app, "app.ts 启动时要装上 installTextFit()，.fit-clip 才有悬停全文"
    text_fit = (UI / "text-fit.ts").read_text(encoding="utf-8")
    assert 'export const FIT_CLIP = "fit-clip";' in text_fit


def test_selected_dropdown_option_is_written_in_full():
    """可输入下拉框的选中项不能被输入框截断：两种下拉都要套 createComboFace。"""
    components = (UI / "components.ts").read_text(encoding="utf-8")
    assert components.count("createComboFace(input)") == 2
