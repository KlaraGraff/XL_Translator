"""Phase 4 的 Excel 控件有一份可自动跑的界面断言。

docs/validation/2026-07-24_phase-04-excel_acceptance.md:52 记的是「未取得手工 UI DOM/截图断言，
原因是当前 macOS 锁屏」；后来 2026-10-02 的 workspace-status-regression.mjs 也只跑了扫描 →
开始 → 完成 → 重扫 → 任务中心。脚本在 ``tests/excel_controls_ui.mjs``：在 Node 里用最小 DOM
直接跑 ``ui/src/views/workspace.ts`` 与 ``ui/src/components.ts`` 的真代码，渲染「高级」折叠块，
按用户的方式点开关，断言 DOM 状态（role="switch" / aria-checked / .on）、互斥落盘补丁、
老脏数据的水合结果和保存失败的回退。

不需要浏览器、不需要 Tauri、不碰 Office。脚本需要 Node ≥ 22.13（``module.stripTypeScriptTypes``，
CI 的 UI job 用 Node 24）；装的是更老的 Node 时跳过，而不是报一个看不懂的语法错误。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests" / "excel_controls_ui.mjs"


def _node_supports_typescript_stripping() -> bool:
    node = shutil.which("node")
    if node is None:
        return False
    result = subprocess.run(
        [node, "-e", "process.exit(typeof require('node:module').stripTypeScriptTypes === 'function' ? 0 : 1)"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


@pytest.mark.skipif(
    not _node_supports_typescript_stripping(),
    reason="需要 Node ≥ 22.13（module.stripTypeScriptTypes）",
)
def test_excel_phase4_controls_have_dom_state_assertions() -> None:
    result = subprocess.run(
        [shutil.which("node"), str(SCRIPT)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    # 脚本自己打印断言的条数；少于 15 条说明有一段被删掉了。
    assert "Excel 控件界面回归：" in result.stdout, result.stdout[-2000:]
    count = int(result.stdout.split("Excel 控件界面回归：", 1)[1].split("项断言通过", 1)[0])
    assert count >= 15, f"断言条数变少了：{count}"


def test_the_source_contracts_still_hold_for_the_excel_controls() -> None:
    """控件与文案还在 workspace.ts 里，脚本断言的就是这两个文件里的真代码。

    控件被搬走或改名时，界面脚本会一起失效；这条用一个便宜的源码检查先把话说清楚，
    免得下一个人只看到 node 脚本报「找不到这一行」。
    """
    workspace = (ROOT / "ui" / "src" / "views" / "workspace.ts").read_text(encoding="utf-8")
    for key, label in {
        "formulaBackfill": "翻译公式单元格",
        "excelAutofit": "用 Excel 调整行高",
        "lockRowHeight": "保持行高，缩小字号",
        "untranslated": "仅补译未翻译内容",
    }.items():
        assert f'key: "{key}"' in workspace, key
        assert label in workspace, label
    # 互斥对必须两半都在，且各自指着对方。
    assert 'exclusiveWith: "lockRowHeight"' in workspace
    assert 'exclusiveWith: "excelAutofit"' in workspace


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
