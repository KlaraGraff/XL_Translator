"""更新流程的验收测试（docs/refactor/2026-07-16_route_b_tauri_implementation.md:173）。

验收清单里那一行「更新检查、忽略更新、单实例激活可用」一直空着：更新检查只有
Mock/合同测试，忽略更新只有「后端抑制通知」的单测，谁也没把前端这条链路从头
走一遍。前端那两个 harness 是 `tests/` 下的 .cjs，pytest 不会自己捡起来，所以
这里把它们接进 Python 测试套件——PR 的 `pytest tests` 和本机全量跑都会带上。

单实例激活不在这里：它要两个真实进程 + 一个真实的 Tauri 窗口，本机跑不出来，
计入外部验收（见 gaps.jsonl 第 136 项）。
"""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

NODE_HARNESSES = (
    "test_update_acceptance.cjs",
    "test_update_activity.cjs",
)


def _node() -> str | None:
    return shutil.which("node")


@unittest.skipUnless(_node(), "需要 node 才能跑前端 harness")
class UpdateFlowAcceptanceTests(unittest.TestCase):
    def _run(self, harness: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [_node(), str(ROOT / "tests" / harness)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )

    def test_check_and_ignore_version_work_end_to_end(self) -> None:
        # 后台检查 → 读到忽略版本 → 不点亮红点 → 点「忽略」→ 立刻不再提示。
        result = self._run("test_update_acceptance.cjs")
        self.assertEqual(result.returncode, 0, result.stdout[-4000:] + result.stderr[-2000:])
        self.assertIn("update acceptance", result.stdout)

    def test_install_and_restart_guards_hold(self) -> None:
        # 有活动任务时先确认、确认不了就不装、Windows 装完之后又冒出任务时再问一次。
        result = self._run("test_update_activity.cjs")
        self.assertEqual(result.returncode, 0, result.stdout[-4000:] + result.stderr[-2000:])
        self.assertIn("passed", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
