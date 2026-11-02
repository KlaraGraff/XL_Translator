"""Excel / Word 共用步骤（core/text_runner_shared.py）的接线检查。

共用代码按 `运行器模块.名字` 取辅助函数；哪个运行器模块少导入一个名字，
要等跑到那一步才会 AttributeError。这里提前钉住。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import core.task_runner as excel_runner
import core.word_task_runner as word_runner
from core.text_runner_shared import TextRunnerSharedMixin


class SharedStepWiringTests(unittest.TestCase):
    def test_every_helper_the_shared_steps_use_exists_in_both_runner_modules(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "core" / "text_runner_shared.py").read_text(
            encoding="utf-8"
        )
        names = set(re.findall(r"\bm\.([A-Za-z_]\w*)", source))
        self.assertTrue(names)
        for module in (excel_runner, word_runner):
            with self.subTest(module=module.__name__):
                missing = sorted(name for name in names if not hasattr(module, name))
                self.assertEqual(missing, [])

    def test_each_runner_points_at_its_own_module(self) -> None:
        self.assertTrue(issubclass(excel_runner.TaskRunner, TextRunnerSharedMixin))
        self.assertTrue(issubclass(word_runner.WordTaskRunner, TextRunnerSharedMixin))
        self.assertEqual(excel_runner.TaskRunner._RUNNER_MODULE, "core.task_runner")
        self.assertEqual(word_runner.WordTaskRunner._RUNNER_MODULE, "core.word_task_runner")
        self.assertEqual(excel_runner.TaskRunner._COVERAGE_UNIT_NOUN, "格")
        self.assertEqual(word_runner.WordTaskRunner._COVERAGE_UNIT_NOUN, "处")


if __name__ == "__main__":
    unittest.main()
