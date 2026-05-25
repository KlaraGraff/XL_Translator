from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core import excel_automation


class ExcelAutomationTests(unittest.TestCase):
    def test_windows_is_supported_for_local_excel_automation(self) -> None:
        with patch.object(excel_automation.platform, "system", return_value="Windows"):
            self.assertTrue(excel_automation.supports_local_excel_automation())

    def test_windows_thread_initialization_uses_com_lifecycle(self) -> None:
        fake_pythoncom = SimpleNamespace(
            CoInitialize=MagicMock(),
            CoUninitialize=MagicMock(),
        )
        with (
            patch.object(excel_automation.platform, "system", return_value="Windows"),
            patch.dict("sys.modules", {"pythoncom": fake_pythoncom}),
        ):
            state = excel_automation.initialize_excel_thread()
            fake_pythoncom.CoInitialize.assert_called_once_with()

            excel_automation.finalize_excel_thread(state)
            fake_pythoncom.CoUninitialize.assert_called_once_with()

    def test_non_windows_thread_initialization_is_noop(self) -> None:
        with patch.object(excel_automation.platform, "system", return_value="Darwin"):
            self.assertIsNone(excel_automation.initialize_excel_thread())

    def test_probe_reports_windows_com_initialization_failure(self) -> None:
        with (
            patch.object(excel_automation.platform, "system", return_value="Windows"),
            patch.object(
                excel_automation,
                "initialize_excel_thread",
                side_effect=RuntimeError("COM missing"),
            ),
        ):
            ok, reason = excel_automation.probe_local_excel_automation()

        self.assertFalse(ok)
        self.assertIn("无法初始化本地 Excel 自动化", reason)
        self.assertIn("COM missing", reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
