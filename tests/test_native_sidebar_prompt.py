from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from config import DOMAIN_PRESETS
from native_app.main_window import Sidebar
from settings import AppSettings


class NativeSidebarPromptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._save_patch = patch("native_app.main_window.save_settings")
        self.mock_save = self._save_patch.start()

    def tearDown(self) -> None:
        self._save_patch.stop()

    def _make_sidebar(self, settings: AppSettings) -> Sidebar:
        sidebar = Sidebar(settings)
        self.addCleanup(sidebar.close)
        self.addCleanup(sidebar.deleteLater)
        return sidebar

    def test_preset_prompt_uses_default_and_saves_preset_override(self) -> None:
        settings = AppSettings(
            domain_preset="资料管理场景",
            custom_prompt="自定义提示词不应覆盖预设",
        )
        sidebar = self._make_sidebar(settings)

        self.assertEqual(
            sidebar.prompt_edit.toPlainText(),
            DOMAIN_PRESETS["资料管理场景"]["_base"],
        )

        edited_prompt = "资料管理场景的临时覆盖提示词"
        sidebar.prompt_edit.setPlainText(edited_prompt)
        self.app.processEvents()

        self.assertEqual(
            settings.domain_prompt_overrides["资料管理场景"],
            edited_prompt,
        )
        self.assertEqual(settings.custom_prompt, "自定义提示词不应覆盖预设")

    def test_switching_presets_refreshes_prompt_without_creating_override(self) -> None:
        settings = AppSettings(
            domain_preset="资料管理场景",
            domain_prompt_overrides={"资料管理场景": "已保存的资料覆盖"},
        )
        sidebar = self._make_sidebar(settings)

        sidebar.domain_combo.setCurrentText("同步工程场景")
        self.app.processEvents()

        self.assertEqual(settings.domain_preset, "同步工程场景")
        self.assertEqual(
            sidebar.prompt_edit.toPlainText(),
            DOMAIN_PRESETS["同步工程场景"]["_base"],
        )
        self.assertNotIn("同步工程场景", settings.domain_prompt_overrides)
        self.assertEqual(
            settings.domain_prompt_overrides["资料管理场景"],
            "已保存的资料覆盖",
        )

    def test_blank_preset_prompt_removes_override(self) -> None:
        settings = AppSettings(
            domain_preset="资料管理场景",
            domain_prompt_overrides={"资料管理场景": "已保存的资料覆盖"},
        )
        sidebar = self._make_sidebar(settings)

        sidebar.prompt_edit.setPlainText("   ")
        self.app.processEvents()

        self.assertNotIn("资料管理场景", settings.domain_prompt_overrides)

    def test_custom_domain_prompt_stays_in_custom_prompt(self) -> None:
        settings = AppSettings(
            domain_preset="自定义",
            custom_prompt="旧自定义提示词",
        )
        sidebar = self._make_sidebar(settings)

        self.assertEqual(sidebar.prompt_edit.toPlainText(), "旧自定义提示词")

        sidebar.prompt_edit.setPlainText("新的自定义提示词")
        self.app.processEvents()

        self.assertEqual(settings.custom_prompt, "新的自定义提示词")
        self.assertEqual(settings.domain_prompt_overrides, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
