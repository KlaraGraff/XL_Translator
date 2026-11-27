"""文件里有没有要翻的字：结束消息逐文件带 source_text_count（审核第二批第 3 条）。

空表、只有数字的表照样写出文件，界面以前只写「已生成」，看不出里面本来就没有要翻的字。
真跑 ``TaskRunner._run()`` + 真实写盘，只把翻译引擎和记忆库换成桩。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

from openpyxl import Workbook

from core.api_config_check import ApiConfigCheckResult
from core.file_scanner import FileItem
from core.task_runner import DoneMsg, TaskRunner
from tests.rate_pins import pinned_surface_rate
from tests.test_excel_stop_partial_output import _settings


class _Engine:
    engine_name = "source-text-count/mock"

    def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
        return {text: f"Item {index}" for index, text in enumerate(texts)}

    def chat(self, system: str, user: str) -> str:
        raise NotImplementedError


class SourceTextCountTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "docs"
        self.root.mkdir()
        self._env = patch.dict(os.environ, {"TRANSLATOR_APP_DATA_DIR": str(Path(self._tmp.name) / "appdata")})
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def _book(self, name: str, values: list[object]) -> Path:
        path = self.root / f"{name}.xlsx"
        wb = Workbook()
        for row, value in enumerate(values, start=1):
            wb.active.cell(row=row, column=1, value=value)
        wb.save(path)
        return path

    def test_done_message_tells_empty_files_apart(self) -> None:
        numbers = self._book("只有数字", [10, 20, 3.5])
        texts = self._book("清单", ["混凝土浇筑", "钢筋绑扎"])
        runner = TaskRunner(
            [FileItem(path=p, name=p.stem, size_kb=1.0) for p in (numbers, texts)],
            _settings(),
            source_root=self.root,
        )
        with ExitStack() as stack:
            stack.enter_context(patch("core.task_runner.TaskLogger", return_value=MagicMock(task_id="t")))
            stack.enter_context(
                patch("core.task_runner.check_translation_api_config", return_value=ApiConfigCheckResult(ok=True))
            )
            stack.enter_context(patch("core.task_runner.build_engine", return_value=_Engine()))
            stack.enter_context(patch("core.task_runner.get_system_prompt", return_value="system"))
            stack.enter_context(patch("core.task_runner.resolve_effective_model_config", return_value=object()))
            stack.enter_context(
                patch("core.task_runner.resolve_surface_rate", side_effect=pinned_surface_rate(batch_items=10, concurrency=1))
            )
            stack.enter_context(patch("core.task_runner.tm_manager.lookup_batch", return_value={}))
            stack.enter_context(patch("core.task_runner.tm_manager.insert_batch", return_value=0))
            runner._run()

        done = [m for m in list(runner._queue.queue) if isinstance(m, DoneMsg)]
        self.assertEqual(len(done), 1)
        # 界面先读 file_results（workspace.ts terminalFileResults），files 也要一样。
        for entries in (done[0].file_results, done[0].files):
            counts = {str(e["name"]): e.get("source_text_count") for e in entries}
            self.assertEqual(counts, {"只有数字": 0, "清单": 2}, entries)


if __name__ == "__main__":
    unittest.main()
