"""外观验收项「深浅主题切换 + 跟随系统；配置面板折叠状态记忆」的 DOM 级回归入口。

断言本身写在 `tests/appearance_theme_dom.mjs`：它用最小 DOM 垫片执行 `ui/src/app.ts`、
`ui/src/views/settings.ts`、`ui/src/components.ts` 里的真实函数，检查的是元素树和
`document.documentElement.dataset.theme` 的实际状态，而不是源码里有没有某个字符串。

这里只负责让它进 pytest：`node` 不在就跳过（Python 侧的用例不依赖 Node），Node 太老、
没有 `stripTypeScriptTypes` 也跳过并说明原因，其余情况一律以子进程退出码为准——
脚本里任何一个断言失败都会让这条用例红。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests" / "appearance_theme_dom.mjs"
# stripTypeScriptTypes 自 Node 22.6 起才有（仍是 experimental）；更老的 Node 只会在
# import 阶段报「不是 node:module 的导出」，那不是这条用例要验的东西。
_MIN_NODE = (22, 6)


def _node_binary() -> str | None:
    return shutil.which("node")


def _node_version(node: str) -> tuple[int, int]:
    result = subprocess.run(
        [node, "--version"], capture_output=True, text=True, check=False
    )
    match = re.match(r"v(\d+)\.(\d+)", result.stdout.strip())
    return (int(match.group(1)), int(match.group(2))) if match else (0, 0)


class AppearanceThemeDomTests(unittest.TestCase):
    def test_theme_switching_and_fold_memory_in_a_real_dom(self) -> None:
        node = _node_binary()
        if node is None:
            self.skipTest("没有 node，跳过 DOM 级外观断言（Python 侧用例不受影响）")
        version = _node_version(node)
        if version < _MIN_NODE:
            self.skipTest(
                f"Node {version[0]}.{version[1]} 没有 stripTypeScriptTypes，需要 "
                f"{_MIN_NODE[0]}.{_MIN_NODE[1]}+"
            )

        result = subprocess.run(
            [node, str(SCRIPT)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

        self.assertEqual(
            result.returncode,
            0,
            "外观 DOM 断言失败：\n"
            + (result.stdout or "")[-4000:]
            + (result.stderr or "")[-2000:],
        )
        self.assertIn("DOM 组断言通过", result.stdout or "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
