"""Excel 中途停止：已翻好的部分照常写进文件，停止口径与续译检测一致。

真跑 ``TaskRunner._run()`` + 真实写盘 + 真实续译检测，只把翻译引擎和记忆库换成桩：
  - 停止后输出文件里一半是双语、一半保留原文，逐文件结果是「已生成」并带
    unfinished_text_count，横幅用的 kpi.unstarted_text_count 与重新扫描后续译弹窗
    的「补 N 格」是同一个数；
  - 停止时没翻的格子不标黄、不计「需复核」；
  - 停止打断的在途请求不算「接口异常」（不出 api_unavailable 那条「请检查 API 密钥」）。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

from openpyxl import Workbook, load_workbook

from core.api_config_check import ApiConfigCheckResult
from core.file_scanner import FileItem
from core.model_throughput import EffectiveModelThroughput
from core.resume_detection import detect_previous_output
from core.task_runner import StoppedMsg, TaskRunner
from settings import AppSettings, EngineSettings

_SOURCES = [f"第{index}号施工段混凝土浇筑" for index in range(1, 31)]


class _StoppingEngine:
    """第 ``stop_on_call`` 次请求时按下停止；``fail_after_stop`` 时这一次请求被打断。"""

    engine_name = "excel-stop-test/mock"

    def __init__(self, *, stop_on_call: int, fail_after_stop: bool) -> None:
        self.runner: TaskRunner | None = None
        self.calls = 0
        self._stop_on_call = stop_on_call
        self._fail_after_stop = fail_after_stop

    def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
        self.calls += 1
        if self.calls == self._stop_on_call:
            assert self.runner is not None
            self.runner.stop()
            if self._fail_after_stop:
                raise ConnectionError("connection aborted while stopping")
        return {text: f"Concrete pouring section {_SOURCES.index(text) + 1}" for text in texts}

    def chat(self, system: str, user: str) -> str:  # pragma: no cover - 不该走到
        raise NotImplementedError


def _settings() -> AppSettings:
    settings = AppSettings(
        engine=EngineSettings(
            mode="cloud",
            cloud_provider="custom_openai",
            cloud_model="excel-stop-test",
            cloud_base_url="https://example.invalid/v1",
            concurrency=1,
            batch_size=10,
        ),
        source_lang="zh",
        target_lang="en",
    )
    settings.excel_output.enable_excel_autofit = False
    return settings


class ExcelStopWritesTranslatedPart(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "docs"
        self.root.mkdir()
        self._env = patch.dict(os.environ, {"TRANSLATOR_APP_DATA_DIR": str(Path(self._tmp.name) / "appdata")})
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def _run(self, *, fail_after_stop: bool) -> tuple[StoppedMsg, Path]:
        source = self.root / "工程量清单.xlsx"
        wb = Workbook()
        ws = wb.active
        for row, text in enumerate(_SOURCES, start=1):
            ws.cell(row=row, column=1, value=text)
        wb.save(source)

        settings = _settings()
        runner = TaskRunner(
            [FileItem(path=source, name=source.stem, size_kb=1.0)],
            settings,
            source_root=self.root,
        )
        engine = _StoppingEngine(stop_on_call=1 if not fail_after_stop else 2, fail_after_stop=fail_after_stop)
        engine.runner = runner
        with ExitStack() as stack:
            stack.enter_context(
                patch("core.task_runner.TaskLogger", return_value=MagicMock(task_id="excel-stop-test"))
            )
            stack.enter_context(
                patch("core.task_runner.check_translation_api_config", return_value=ApiConfigCheckResult(ok=True))
            )
            stack.enter_context(patch("core.task_runner.build_engine", return_value=engine))
            stack.enter_context(patch("core.task_runner.get_system_prompt", return_value="system"))
            stack.enter_context(patch("core.task_runner.resolve_effective_model_config", return_value=object()))
            stack.enter_context(
                patch(
                    "core.task_runner.get_model_throughput",
                    return_value=EffectiveModelThroughput(profile_key="t", batch_size=10, concurrency=1),
                )
            )
            stack.enter_context(patch("core.task_runner.tm_manager.lookup_batch", return_value={}))
            stack.enter_context(patch("core.task_runner.tm_manager.insert_batch", return_value=0))
            runner._run()

        messages = [m for m in list(runner._queue.queue) if isinstance(m, StoppedMsg)]
        self.assertEqual(len(messages), 1)
        return messages[0], source

    def _assert_partial_output(self, stopped: StoppedMsg, source: Path, *, translated: int) -> None:
        untranslated = len(_SOURCES) - translated
        entry = stopped.files[0]
        self.assertTrue(entry["success"], entry)
        self.assertEqual(entry["status"], "succeeded")
        self.assertEqual(entry["unfinished_text_count"], untranslated)
        self.assertEqual(stopped.kpi["unstarted_text_count"], untranslated)
        # 停止时没翻的格子不是「需复核」。
        self.assertEqual(stopped.review["total_count"], 0)
        self.assertIn(f"还有 {untranslated} 格没翻", stopped.message)

        output = Path(entry["output_path"])
        cells = [
            str(row[0].value or "")
            for row in load_workbook(output).active.iter_rows(max_col=1)
        ]
        bilingual = [value for value in cells if "\n" in value]
        self.assertEqual(len(bilingual), translated)
        self.assertEqual(len([value for value in cells if value in _SOURCES]), untranslated)

        # 重新扫描后的续译弹窗说的是同一个数。
        detected = detect_previous_output(
            surface="excel",
            items=[{"path": str(source)}],
            scan_roots=[str(self.root)],
            custom_output_root=None,
            target_lang="en",
            source_lang="zh",
        )
        self.assertIsNotNone(detected)
        self.assertEqual(detected["files"][0]["status"], "partial")
        self.assertEqual(detected["files"][0]["untranslated_count"], untranslated)

    def test_stop_between_batches_writes_translated_part(self) -> None:
        stopped, source = self._run(fail_after_stop=False)
        self._assert_partial_output(stopped, source, translated=10)

    def test_request_cut_off_by_stop_is_not_reported_as_api_failure(self) -> None:
        stopped, source = self._run(fail_after_stop=True)
        self._assert_partial_output(stopped, source, translated=10)
        types = {str(issue.get("type")) for issue in stopped.issues}
        self.assertNotIn("api_unavailable", types)
        self.assertFalse(
            any("API 密钥" in str(issue.get("message")) for issue in stopped.issues),
            stopped.issues,
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
