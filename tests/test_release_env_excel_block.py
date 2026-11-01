"""scripts/release_env_tests.py 挡真 Excel 的规则：

真 xlwings 一律拒绝（发布构建机没有 Office）；用例换上假 xlwings 时照常调用原函数，
否则专门测 create_excel_app 包装的用例在发布环境里必然失败（10.5.0 发版前踩到）。
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]


def _load_harness():
    spec = importlib.util.spec_from_file_location(
        "release_env_tests_under_test", ROOT / "scripts" / "release_env_tests.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ReleaseEnvExcelBlockTests(unittest.TestCase):
    def setUp(self) -> None:
        from core import excel_automation, task_runner

        self.excel_automation = excel_automation
        patcher_a = patch.object(
            excel_automation, "create_excel_app", excel_automation.create_excel_app
        )
        patcher_b = patch.object(
            task_runner, "create_excel_app", task_runner.create_excel_app
        )
        patcher_a.start()
        patcher_b.start()
        self.addCleanup(patcher_a.stop)
        self.addCleanup(patcher_b.stop)
        _load_harness()._block_real_excel()

    def test_a_fake_xlwings_goes_through_the_real_wrapper(self) -> None:
        fake = SimpleNamespace(App=MagicMock(return_value="app"))
        with patch.dict("sys.modules", {"xlwings": fake}):
            self.assertEqual(self.excel_automation.create_excel_app(), "app")
        fake.App.assert_called_once_with(visible=False, add_book=False)

    def test_the_real_xlwings_is_refused(self) -> None:
        real_like = ModuleType("xlwings")
        real_like.__file__ = "/site-packages/xlwings/__init__.py"
        real_like.App = MagicMock()
        with (
            patch.dict("sys.modules", {"xlwings": real_like}),
            self.assertRaises(RuntimeError),
        ):
            self.excel_automation.create_excel_app()
        real_like.App.assert_not_called()

    def test_an_unloaded_xlwings_is_refused(self) -> None:
        saved = sys.modules.pop("xlwings", None)
        try:
            with self.assertRaises(RuntimeError):
                self.excel_automation.create_excel_app()
        finally:
            if saved is not None:
                sys.modules["xlwings"] = saved


if __name__ == "__main__":
    unittest.main()
