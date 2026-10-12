"""切换界面语言不再重新加载页面（ui/src/i18n/index.ts setLocale）。

模块加载时就调用 t() 算好的常量会停留在旧语言，所以模块级文案常量必须用
localized(() => ...) 包起来，或者写成函数。这里按行粗查最常见的写法。
"""

from __future__ import annotations

import re
from pathlib import Path

UI_SRC = Path(__file__).resolve().parents[1] / "ui" / "src"
TOP_LEVEL_T = re.compile(r"^(?:export\s+)?(?:const|let|var)\s+\w+[^=]*=\s*(?:t|tx)\(")


def test_no_module_level_constant_is_translated_at_load_time():
    offenders = []
    for path in sorted(UI_SRC.rglob("*.ts")):
        if path.name.endswith(".test.ts") or "i18n" in path.relative_to(UI_SRC).parts:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if TOP_LEVEL_T.match(line):
                offenders.append(f"{path.relative_to(UI_SRC)}:{number}: {line.strip()}")
    assert offenders == [], "模块级常量要用 localized(() => ...) 或函数：\n" + "\n".join(offenders)


def test_language_switch_does_not_reload_the_page():
    settings = (UI_SRC / "views" / "settings.ts").read_text(encoding="utf-8")
    app = (UI_SRC / "app.ts").read_text(encoding="utf-8")
    assert "location.reload" not in settings and "location.reload" not in app
    assert "setLocale(option.value)" in settings
    assert "remountCurrentView()" in app and "relocalizeShell()" in app
