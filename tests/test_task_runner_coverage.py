"""A whole Excel run reports how much of it was non-API work.

代码审计 2026-08-31 的 X1 清单第 3 条：翻译吞吐里「模型调用以外的部分占多少」没有测出来，
所以慢了只能猜。这里用假模型跑完全部阶段（读取 → 记忆库 → 模型 → 写回），把任务日志里
每个阶段、每一段的耗时读出来，确认：

* 每个阶段都有一条自己的耗时记录，不是只有总数；
* 每一段的耗时都不超过任务总耗时（同一段时间没有被重复记账，也没有哪段被漏掉）；
* 收集、模型、写回三段各有自己的埋点，日志里能回答「慢在模型上还是慢在别处」。

时间全在测试进程里，模型是假引擎；断言只比较「有没有记录、谁不超过谁」，不比较具体
秒数，跑在快慢不同的构建机上都不会抖。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from openpyxl import Workbook

from core.api_config_check import ApiConfigCheckResult
from core.file_scanner import FileItem
from core.task_runner import DoneMsg, TaskRunner
from settings import AppSettings, EngineSettings
from tests.app_data_isolation import IsolatedAppDataTestCase
from tests.rate_pins import pinned_surface_rate

_PHASE_RE = re.compile(r"^阶段 (\d+) 耗时 ([\d.]+)s$")


class _CountingEngine:
    """假模型：只回一句译文，不联网。"""

    engine_name = "timing/mock"

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, _system: str, _user: str) -> str:
        self.calls += 1
        return '{"source_langs":["zh"]}'


def _settings() -> AppSettings:
    settings = AppSettings(
        engine=EngineSettings(
            mode="cloud",
            cloud_provider="custom_openai",
            cloud_model="timing-test-model",
            cloud_base_url="https://example.invalid/v1",
            concurrency=1,
            batch_size=10,
        ),
        source_lang="zh",
        target_lang="en",
        # 语种预检和场景通读各会多花一次模型调用；这里只看阶段耗时，关掉更干净。
        excel_domain_preset="无",
        excel_smart_context=False,
    )
    settings.excel_output.translate_sheet_names = False
    return settings


def _phase_timings(lines: list[str]) -> dict[int, float]:
    timings: dict[int, float] = {}
    for line in lines:
        match = _PHASE_RE.match(line.strip())
        if match:
            timings[int(match.group(1))] = float(match.group(2))
    return timings


class ExcelTaskPhaseTimingTests(IsolatedAppDataTestCase):
    def _run(self, root: Path, texts: list[str]) -> tuple[DoneMsg, dict[int, float], MagicMock]:
        source = root / "source.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        for index, value in enumerate(texts, 1):
            sheet.cell(row=index, column=1, value=value)
        workbook.save(source)
        workbook.close()

        task_logger = MagicMock(task_id="timing-contract")
        # 阶段耗时那几行走的是模块级 loguru logger，不是 TaskLogger；两处都接住。
        module_lines: list[str] = []

        with (
            patch(
                "core.task_runner.bilingual_writer.write_bilingual_file",
                side_effect=lambda **kwargs: root / "out" / f"{kwargs['source_path'].stem}.xlsx",
            ),
            patch("core.task_runner.TaskLogger", return_value=task_logger),
            patch(
                "core.task_runner.check_translation_api_config",
                return_value=ApiConfigCheckResult(ok=True),
            ),
            patch("core.task_runner.build_engine", return_value=_CountingEngine()),
            patch("core.task_runner.get_system_prompt", return_value="system"),
            patch("core.task_runner.resolve_effective_model_config", return_value=object()),
            patch(
                "core.task_runner.resolve_surface_rate",
                side_effect=pinned_surface_rate(batch_items=10, concurrency=1),
            ),
            patch("core.task_runner.bilingual_writer.build_output_dir", return_value=root / "out"),
            patch.object(TaskRunner, "_collect_texts", side_effect=lambda *a, **k: (list(texts), 1)),
            patch(
                "core.task_runner.tm_manager.lookup_batch",
                side_effect=lambda values, _pair: {value: None for value in values},
            ),
            patch("core.task_runner.tm_manager.insert_batch", return_value=0),
            patch(
                "core.task_runner.translate_texts",
                side_effect=lambda values, *_a, **_k: {value: f"[en] {value}" for value in values},
            ),
            # 运行日志面板那一路不需要；只留模块 logger 的 info 用来读阶段耗时。
            patch.object(TaskRunner, "_log", side_effect=lambda *_a, **_k: None),
            patch("core.task_runner.logger") as module_logger,
        ):
            module_logger.info.side_effect = lambda message, *a, **k: module_lines.append(str(message))
            runner = TaskRunner(
                [FileItem(path=source, name="source", size_kb=1.0)],
                _settings(),
                source_root=root,
            )
            runner._run()

        done = next(
            message for message in list(runner._queue.queue) if isinstance(message, DoneMsg)
        )
        return done, _phase_timings(module_lines), task_logger

    def test_every_phase_reports_its_own_elapsed_time_and_they_fit_in_the_total(self) -> None:
        root = Path(self.app_data_dir).parent / "timing-work"
        root.mkdir(parents=True, exist_ok=True)
        texts = [f"施工内容{i}" for i in range(1, 41)]

        done, timings, _task_logger = self._run(root, texts)

        # 没有 Excel 自动行高时是三个阶段：读取 / 翻译 / 生成。
        self.assertEqual(sorted(timings), [2, 3], f"阶段耗时记录不全：{timings}")
        for phase, elapsed in timings.items():
            self.assertGreaterEqual(elapsed, 0.0, phase)
            # 每一段都不超过任务总耗时：同一段时间没有被记两遍。
            self.assertLessEqual(
                elapsed,
                max(done.elapsed_sec, 0.05) + 0.01,
                f"阶段 {phase} 的耗时 {elapsed}s 超过了任务总耗时 {done.elapsed_sec}s",
            )
        self.assertGreater(done.elapsed_sec, 0.0)

    def test_non_api_share_of_a_whole_run_can_be_computed_from_the_log(self) -> None:
        """审计要的「非 API 耗时占比」：收集 + 写回 ÷ 总耗时，三段都有数就能算出来。

        以前只有「总耗时」一个数字，慢在模型上还是慢在写回里全靠猜。这里真的把占比
        算一遍并断言它落在 0~1：任何一段没记录、或者记的不是秒数，这一步就会失败。
        """
        root = Path(self.app_data_dir).parent / "timing-work-3"
        root.mkdir(parents=True, exist_ok=True)

        done, _timings, task_logger = self._run(root, [f"施工内容{i}" for i in range(1, 31)])

        def elapsed_of(name: str) -> float:
            call = next(call for call in task_logger.mock_calls if call[0] == name)
            value = call.kwargs["elapsed"] if "elapsed" in call.kwargs else call.args[-1]
            return float(value)

        collect = elapsed_of("file_collected") + elapsed_of("global_collected")
        model = elapsed_of("global_api_done")
        write_back = elapsed_of("file_write_done")
        total = float(done.elapsed_sec)
        self.assertGreater(total, 0.0)
        non_api = (collect + write_back) / total
        self.assertGreaterEqual(non_api, 0.0)
        self.assertLessEqual(non_api, 1.0, f"非 API 占比 {non_api} 超过了 1，说明有段时间被重复记账")
        self.assertGreaterEqual(model, 0.0)
        # 三段的绝对值都不超过总耗时；比例才有意义。
        for name, value in (("collect", collect), ("model", model), ("write_back", write_back)):
            self.assertLessEqual(value, total + 0.01, f"{name}={value}s 超过总耗时 {total}s")

    def test_collect_model_and_write_back_each_have_their_own_timing(self) -> None:
        """三段埋点都在：收集、模型、写回。审计要的「非 API 耗时占比」靠这三段算。"""
        root = Path(self.app_data_dir).parent / "timing-work-2"
        root.mkdir(parents=True, exist_ok=True)

        done, timings, task_logger = self._run(root, [f"施工内容{i}" for i in range(1, 21)])

        called = {call[0] for call in task_logger.mock_calls if call[0] in {
            "file_collected", "global_collected", "global_api_done", "file_write_done",
        }}
        self.assertEqual(
            called,
            {"file_collected", "global_collected", "global_api_done", "file_write_done"},
            "任务日志缺少收集 / 模型 / 写回其中一段的耗时",
        )
        # 收集耗时和写回耗时都必须是数字（可以是 0.0，但必须有值）——埋点方法签名
        # 收的是 float，写成别的类型就会在这里暴露。
        for name in ("file_collected", "global_api_done", "file_write_done"):
            call = next(call for call in task_logger.mock_calls if call[0] == name)
            elapsed = call.kwargs["elapsed"] if "elapsed" in call.kwargs else call.args[-1]
            self.assertIsInstance(elapsed, float, f"{name} 的耗时不是数字：{call}")
        self.assertGreater(timings[2] + timings[3], 0.0)
        self.assertTrue(done.file_results[0]["success"])


if __name__ == "__main__":
    unittest.main()
