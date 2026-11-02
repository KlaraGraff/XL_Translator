"""界面异步流程的行为测试入口（CODE_AUDIT_2026-08-31.md 第 1323-1326 行三项缺口）。

审计留下的三条「预算内没查完」都要求造出前端故障才能验：

1. ``runPdfBatchRerun`` 单页重跑抛错后跳过 ``waitForRerunSlot``，直接发下一页；
2. ``watchTask`` 在事件流持续失败时跨轮无限重连（``streamTask`` 内部 7 次封顶只管一轮）；
3. ``saveBinaryDownload`` 把服务端 ``Content-Disposition`` 的 filename 原样当保存框默认名。

真跑一遍要起 sidecar、造后端 409/503 故障，代价远大于这几条断言本身；所以实际断言写在
``tests/ui_async_flows.mjs`` 里：它用 node 自带的类型擦除直接加载 ``ui/src`` 的 TypeScript，
给 Tauri IPC、原生保存框、``fetch`` 和定时器全部打桩，然后驱动真实函数、检查真实请求序列。
这里只负责在 pytest 里把它按场景跑一遍，让缺口条目能用 ``::`` 名字指认具体是哪一条。

node 版本要求：``module.registerHooks`` / ``module.stripTypeScriptTypes`` 是 node 22.15+
的能力（PR 检查固定 node 24）。本机 node 更旧时跳过，并给出原因。
"""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests" / "ui_async_flows.mjs"
# module.registerHooks 从 22.15.0 起才存在（stripTypeScriptTypes 更早些）。
MIN_NODE = (22, 15)


def _node() -> str | None:
    return shutil.which("node")


def _node_version(node: str) -> tuple[int, int]:
    result = subprocess.run([node, "--version"], capture_output=True, text=True, check=False)
    parts = result.stdout.strip().lstrip("v").split(".")
    try:
        return int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        return 0, 0


def _run_scenario(name: str) -> None:
    node = _node()
    if node is None:
        raise unittest.SkipTest("未找到 node，无法运行 ui/src 的 TypeScript 行为测试")
    version = _node_version(node)
    if version < MIN_NODE:
        raise unittest.SkipTest(
            f"node {version[0]}.{version[1]} 太旧：加载钩子要 {MIN_NODE[0]}.{MIN_NODE[1]}+"
        )
    result = subprocess.run(
        [node, str(SCRIPT), name],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"{name} 失败：\n{result.stdout[-4000:]}\n{result.stderr[-2000:]}")


class SaveBinaryDownloadFilenameTests(unittest.TestCase):
    """③ 保存框默认名：用服务端 Content-Disposition 的 filename，缺失才退回兜底名。"""

    def test_server_filename_is_used_and_fallback_only_fills_the_gap(self) -> None:
        _run_scenario("save_binary_download_prefers_server_filename")


class PdfBatchRerunFailureTests(unittest.TestCase):
    """① 批量重跑：单页失败只弹 toast，不等槽位就发下一页（当前已知的缺口行为）。"""

    def test_failed_page_does_not_wait_for_the_rerun_slot(self) -> None:
        _run_scenario("batch_rerun_skips_the_slot_wait_after_a_failure")


class WatchTaskReconnectTests(unittest.TestCase):
    """② 任务事件流：单轮 7 次封顶之后，catch 里还会再开一轮，整场会话不封顶。"""

    def test_stream_failure_reconnects_across_rounds(self) -> None:
        _run_scenario("watch_task_reconnects_across_rounds_without_a_cap")


if __name__ == "__main__":
    unittest.main(verbosity=2)
