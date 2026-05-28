from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from config import (
    CLAUDE_BASE_URL,
    CLOUD_ENGINES,
    DISABLED_BASE_URL_PLACEHOLDER,
    DOMAIN_PRESETS,
    LOCAL_MODEL_PROVIDERS,
    OPENAI_BASE_URL,
)
from core.model_catalog import ModelCatalogResult
from core.model_roles import ROLE_CLEANER, ROLE_IMAGE, ROLE_TRANSLATION, SOURCE_INDEPENDENT
from core.update_checker import UpdateCheckResult
from native_app.main_window import Sidebar
from settings import AppSettings, get_cloud_provider_config


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

    def test_sidebar_model_role_selector_switches_to_image_role(self) -> None:
        settings = AppSettings()
        sidebar = self._make_sidebar(settings)

        self.assertEqual(sidebar.model_role_combo.currentData(), ROLE_TRANSLATION)

        sidebar.model_role_combo.setCurrentIndex(sidebar.model_role_combo.findData(ROLE_IMAGE))
        self.app.processEvents()

        self.assertEqual(sidebar.model_role_combo.currentData(), ROLE_IMAGE)
        self.assertFalse(sidebar.source_role_combo.isHidden())
        self.assertTrue(sidebar.mode_combo.isHidden())
        self.assertTrue(sidebar.model_catalog_status.text())
        self.assertFalse(sidebar.pdf_review_frame.isHidden())
        self.assertIn("未启用 PDF 翻译审核时可留空", sidebar.review_model_status.text())

    def test_sidebar_update_ignore_button_reflects_global_ignore_state(self) -> None:
        settings = AppSettings()
        sidebar = self._make_sidebar(settings)

        self.assertEqual(sidebar.update_ignore_button.text(), "忽略更新")
        self.assertFalse(sidebar.update_ignore_button.property("updateIgnored"))

        settings.update.ignore_updates = True
        sidebar.sync_update_ignore_button()

        self.assertEqual(sidebar.update_ignore_button.text(), "已忽略更新")
        self.assertTrue(sidebar.update_ignore_button.property("updateIgnored"))

    def test_sidebar_update_notice_buttons_are_hidden_until_update_available(self) -> None:
        sidebar = self._make_sidebar(AppSettings())

        self.assertTrue(sidebar.update_notice_button.isHidden())
        self.assertTrue(sidebar.ignore_notice_button.isHidden())

        sidebar.set_update_notice(
            UpdateCheckResult(
                ok=True,
                status="available",
                message="发现新版 V5.1。",
                current_version="5.0",
                latest_version="5.1",
                latest_tag="v5.1",
                release_url="https://example.test/release",
            )
        )

        self.assertFalse(sidebar.update_notice_button.isHidden())
        self.assertFalse(sidebar.ignore_notice_button.isHidden())

    def test_sidebar_update_footer_lives_inside_scroll_form(self) -> None:
        sidebar = self._make_sidebar(AppSettings())

        self.assertIs(sidebar.update_check_button.parentWidget(), sidebar._form.parentWidget())
        self.assertIs(sidebar.update_ignore_button.parentWidget(), sidebar._form.parentWidget())

    def test_empty_model_catalog_status_does_not_reserve_blank_row(self) -> None:
        sidebar = self._make_sidebar(AppSettings())

        self.assertEqual(sidebar.model_catalog_status.text(), "")
        self.assertTrue(sidebar.model_catalog_status.isHidden())

        sidebar._set_model_catalog_status("API 配置已变化，请重新获取模型列表。")
        self.assertFalse(sidebar.model_catalog_status.isHidden())

        sidebar._set_model_catalog_status("")
        self.assertTrue(sidebar.model_catalog_status.isHidden())

    def test_sidebar_model_config_import_export_buttons_exist(self) -> None:
        sidebar = self._make_sidebar(AppSettings())

        self.assertEqual(sidebar.export_model_config_button.text(), "导出配置")
        self.assertEqual(sidebar.import_model_config_button.text(), "导入配置")

    def test_export_model_config_writes_json_with_keys(self) -> None:
        settings = AppSettings()
        settings.engine.cloud_provider = "custom_openai"
        settings.engine.cloud_model = "mimo-v2-pro"
        settings.engine.cloud_base_url = "https://token-plan-cn.xiaomimimo.com/v1"
        sidebar = self._make_sidebar(settings)

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "model-config.json"
            with (
                patch(
                    "native_app.main_window.QFileDialog.getSaveFileName",
                    return_value=(str(target), "JSON 文件 (*.json)"),
                ),
                patch(
                    "native_app.main_window.get_key",
                    side_effect=lambda provider: {
                        "custom_openai": "secret",
                        "openai": "unused-cloud-secret-should-not-export",
                        "ollama": "local-secret-should-not-export",
                    }.get(provider, ""),
                ),
                patch("native_app.main_window.QMessageBox.information"),
            ):
                sidebar._export_model_config()

            payload = json.loads(target.read_text(encoding="utf-8"))

        self.assertEqual(payload["type"], "translator_cloud_model_config")
        self.assertEqual(
            payload["model_config"]["engine"]["cloud_provider"],
            "custom_openai",
        )
        self.assertEqual(payload["model_config"]["engine"]["cloud_model"], "mimo-v2-pro")
        self.assertNotIn("local_provider", payload["model_config"]["engine"])
        self.assertNotIn("local_model", payload["model_config"]["engine"])
        self.assertNotIn("local_base_url", payload["model_config"]["engine"])
        self.assertNotIn("ollama_concurrency", payload["model_config"]["engine"])
        self.assertEqual(payload["api_keys"], {"custom_openai": "secret"})

    def test_export_model_config_resolves_legacy_custom_openai_key(self) -> None:
        settings = AppSettings()
        settings.engine.cloud_provider = "custom_openai"
        settings.engine.cloud_model = "mimo-v2-pro"
        sidebar = self._make_sidebar(settings)

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "model-config.json"
            with (
                patch(
                    "native_app.main_window.QFileDialog.getSaveFileName",
                    return_value=(str(target), "JSON 文件 (*.json)"),
                ),
                patch(
                    "native_app.main_window.get_key",
                    side_effect=lambda provider: "legacy-secret"
                    if provider == "custom_openai"
                    else "",
                ),
                patch("native_app.main_window.QMessageBox.information"),
            ):
                sidebar._export_model_config()

            payload = json.loads(target.read_text(encoding="utf-8"))

        self.assertEqual(payload["api_keys"], {"custom_openai": "legacy-secret"})

    def test_import_model_config_updates_model_settings_and_keys(self) -> None:
        settings = AppSettings()
        settings.engine.mode = "local"
        settings.engine.local_provider = "lm_studio"
        settings.engine.local_model = "do-not-overwrite"
        settings.engine.local_base_url = "http://localhost:1234/v1"
        sidebar = self._make_sidebar(settings)
        payload = {
            "type": "translator_cloud_model_config",
            "version": 1,
            "model_config": {
                "engine": {
                    "cloud_provider": "custom_openai",
                    "cloud_model": "imported-model",
                    "cloud_base_url": "https://import.example/v1",
                    "local_provider": "ollama",
                    "local_model": "should-be-ignored",
                    "local_base_url": "http://localhost:11434",
                }
            },
            "api_keys": {
                "custom_openai": "imported-secret",
                "ollama": "local-secret-should-be-ignored",
            },
        }

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "model-config.json"
            source.write_text(json.dumps(payload), encoding="utf-8")
            with (
                patch(
                    "native_app.main_window.QFileDialog.getOpenFileName",
                    return_value=(str(source), "JSON 文件 (*.json)"),
                ),
                patch("native_app.main_window.save_key") as save_key_mock,
                patch("native_app.main_window.QMessageBox.information"),
            ):
                sidebar._import_model_config()

        self.assertEqual(settings.engine.cloud_provider, "custom_openai")
        self.assertEqual(settings.engine.cloud_model, "imported-model")
        self.assertEqual(settings.engine.cloud_base_url, "https://import.example/v1")
        self.assertEqual(settings.engine.mode, "cloud")
        self.assertEqual(settings.engine.local_provider, "lm_studio")
        self.assertEqual(settings.engine.local_model, "do-not-overwrite")
        self.assertEqual(settings.engine.local_base_url, "http://localhost:1234/v1")
        self.assertEqual(sidebar.provider_combo.currentText(), "OpenAI 兼容")
        self.assertEqual(sidebar.model_combo.currentText(), "imported-model")
        save_key_mock.assert_called_once_with("custom_openai", "imported-secret")

    def test_translation_local_mode_shows_only_local_provider_fields(self) -> None:
        settings = AppSettings()
        settings.engine.mode = "local"
        settings.engine.local_provider = "lm_studio"
        settings.engine.local_model = "qwen-local"
        settings.engine.local_base_url = "http://localhost:1234/v1"
        sidebar = self._make_sidebar(settings)

        providers = [sidebar.provider_combo.itemText(i) for i in range(sidebar.provider_combo.count())]

        self.assertFalse(sidebar.mode_combo.isHidden())
        self.assertEqual(providers, list(LOCAL_MODEL_PROVIDERS.keys()))
        self.assertEqual(sidebar.provider_combo.currentText(), "LM Studio")
        self.assertTrue(sidebar.api_key_input.isHidden())
        self.assertFalse(sidebar.base_url_input.isHidden())
        self.assertFalse(sidebar.model_combo.isHidden())
        self.assertFalse(hasattr(sidebar, "ollama_combo"))
        self.assertEqual(sidebar.model_combo.currentText(), "qwen-local")

    def test_cloud_role_provider_lists_do_not_include_local_or_hermes_entries(self) -> None:
        sidebar = self._make_sidebar(AppSettings())

        translation_providers = [
            sidebar.provider_combo.itemText(i) for i in range(sidebar.provider_combo.count())
        ]
        self.assertEqual(translation_providers, list(CLOUD_ENGINES.keys()))
        self.assertNotIn("Hermes 内置", translation_providers)

        sidebar.model_role_combo.setCurrentIndex(sidebar.model_role_combo.findData(ROLE_IMAGE))
        self.app.processEvents()
        image_providers = [
            sidebar.provider_combo.itemText(i) for i in range(sidebar.provider_combo.count())
        ]
        self.assertNotIn("Ollama", image_providers)
        self.assertNotIn("LM Studio", image_providers)
        self.assertNotIn("自定义", image_providers)
        self.assertNotIn("Hermes 内置", image_providers)

        sidebar.model_role_combo.setCurrentIndex(sidebar.model_role_combo.findData(ROLE_CLEANER))
        self.app.processEvents()
        cleaner_providers = [
            sidebar.provider_combo.itemText(i) for i in range(sidebar.provider_combo.count())
        ]
        self.assertEqual(cleaner_providers, list(CLOUD_ENGINES.keys()))
        self.assertNotIn("Hermes 内置", cleaner_providers)

    def test_sidebar_model_role_selector_keeps_form_width_alignment(self) -> None:
        sidebar = self._make_sidebar(AppSettings())
        sidebar.model_role_combo.setCurrentIndex(sidebar.model_role_combo.findData(ROLE_IMAGE))
        sidebar.resize(330, 980)
        sidebar.show()
        self.app.processEvents()

        self.assertEqual(sidebar.model_role_combo.objectName(), "ModelRoleCombo")
        self.assertEqual(sidebar.model_role_combo.width(), sidebar.source_role_combo.width())
        self.assertEqual(sidebar.model_role_combo.width(), sidebar.provider_combo.width())

    def test_sidebar_rejects_chained_follow_selection(self) -> None:
        settings = AppSettings()
        settings.cleaner_model_role.source_role = ROLE_TRANSLATION
        sidebar = self._make_sidebar(settings)
        sidebar.model_role_combo.setCurrentIndex(sidebar.model_role_combo.findData(ROLE_IMAGE))
        self.app.processEvents()

        with patch("native_app.main_window.QMessageBox.warning") as warning:
            sidebar.source_role_combo.setCurrentIndex(
                sidebar.source_role_combo.findData(ROLE_CLEANER)
            )
            self.app.processEvents()

        self.assertTrue(warning.called)
        self.assertNotEqual(settings.image_model_role.source_role, ROLE_CLEANER)

    def test_sidebar_rejects_cloud_only_role_following_local_translation(self) -> None:
        settings = AppSettings()
        settings.engine.mode = "local"
        settings.engine.local_model = "qwen2.5:14b"
        settings.cleaner_model_role.source_role = "independent"
        sidebar = self._make_sidebar(settings)
        sidebar.model_role_combo.setCurrentIndex(sidebar.model_role_combo.findData(ROLE_CLEANER))
        self.app.processEvents()

        with patch("native_app.main_window._show_local_follow_warning") as warning:
            sidebar.source_role_combo.setCurrentIndex(
                sidebar.source_role_combo.findData(ROLE_TRANSLATION)
            )
            self.app.processEvents()

        self.assertTrue(warning.called)
        self.assertEqual(settings.cleaner_model_role.source_role, "independent")

    def test_sidebar_concurrency_text_tracks_translation_mode(self) -> None:
        settings = AppSettings()
        settings.engine.mode = "cloud"
        settings.engine.concurrency = 6
        settings.engine.ollama_concurrency = 2
        sidebar = self._make_sidebar(settings)

        self.assertEqual(sidebar.concurrency_input.text(), "6")
        sidebar.mode_combo.setCurrentIndex(sidebar.mode_combo.findData("local"))
        self.app.processEvents()

        self.assertEqual(settings.engine.mode, "local")
        self.assertEqual(sidebar.concurrency_input.text(), "2")

    def test_fetch_image_models_prefers_gpt_image_2_when_available(self) -> None:
        settings = AppSettings()
        settings.image_model_role.source_role = "independent"
        settings.image_model_role.cloud_provider = "custom_openai"
        settings.image_model_role.cloud_model = "older-image-model"
        settings.image_model_role.cloud_base_url = "https://images.example/v1"
        sidebar = self._make_sidebar(settings)
        sidebar.model_role_combo.setCurrentIndex(sidebar.model_role_combo.findData(ROLE_IMAGE))
        self.app.processEvents()

        with (
            patch("native_app.main_window.fetch_openai_compatible_models") as fetch_models,
            patch("native_app.main_window.resolve_effective_model_config") as resolve_config,
            patch("native_app.main_window.select_combo_text_match"),
        ):
            from core.model_roles import resolve_effective_model_config as real_resolve

            resolve_config.side_effect = lambda app_settings, role: real_resolve(
                app_settings,
                role,
            )
            fetch_models.return_value = ModelCatalogResult(
                ok=True,
                models=["other-image-model", "gpt-image-2"],
                message="已获取 2 个模型。",
            )
            sidebar._fetch_models()

        self.assertEqual(settings.image_model_role.cloud_model, "gpt-image-2")
        self.assertEqual(sidebar.model_combo.currentText(), "gpt-image-2")

    def test_pdf_review_model_can_follow_image_model_in_sidebar(self) -> None:
        settings = AppSettings()
        settings.image_model_role.source_role = SOURCE_INDEPENDENT
        sidebar = self._make_sidebar(settings)
        sidebar.model_role_combo.setCurrentIndex(sidebar.model_role_combo.findData(ROLE_IMAGE))
        self.app.processEvents()

        sources = [
            sidebar.review_source_role_combo.itemData(i)
            for i in range(sidebar.review_source_role_combo.count())
        ]

        self.assertIn(ROLE_IMAGE, sources)
        sidebar.review_source_role_combo.setCurrentIndex(
            sidebar.review_source_role_combo.findData(ROLE_IMAGE)
        )
        self.app.processEvents()

        self.assertEqual(settings.pdf_review_model_role.source_role, ROLE_IMAGE)
        self.assertEqual(
            sidebar.review_source_role_combo.currentData(),
            ROLE_IMAGE,
        )

    def test_cloud_provider_switches_restore_isolated_base_url_and_model(self) -> None:
        settings = AppSettings()
        settings.engine.cloud_provider = "custom_openai"
        settings.engine.cloud_model = "mimo-v2-pro"
        settings.engine.cloud_base_url = "https://token-plan-cn.xiaomimimo.com/v1"
        sidebar = self._make_sidebar(settings)

        sidebar.provider_combo.setCurrentText("Claude (Anthropic)")
        self.app.processEvents()

        self.assertEqual(settings.engine.cloud_provider, "claude")
        self.assertEqual(sidebar.base_url_input.text(), CLAUDE_BASE_URL)
        self.assertEqual(sidebar.model_combo.currentText(), "")
        custom_config = get_cloud_provider_config(settings.engine, "custom_openai")
        self.assertEqual(custom_config.cloud_model, "mimo-v2-pro")
        self.assertEqual(
            custom_config.cloud_base_url,
            "https://token-plan-cn.xiaomimimo.com/v1",
        )

        sidebar.model_combo.setCurrentText("claude-sonnet")
        sidebar.base_url_input.setText("https://api.anthropic.com")
        sidebar._on_base_url_changed()
        self.assertEqual(sidebar.base_url_input.text(), CLAUDE_BASE_URL)

        sidebar.provider_combo.setCurrentText("OpenAI 兼容")
        self.app.processEvents()

        self.assertEqual(settings.engine.cloud_provider, "custom_openai")
        self.assertEqual(sidebar.model_combo.currentText(), "mimo-v2-pro")
        self.assertEqual(
            sidebar.base_url_input.text(),
            "https://token-plan-cn.xiaomimimo.com/v1",
        )
        claude_config = get_cloud_provider_config(settings.engine, "claude")
        self.assertEqual(claude_config.cloud_model, "claude-sonnet")
        self.assertEqual(claude_config.cloud_base_url, CLAUDE_BASE_URL)

    def test_official_blank_base_url_restores_default(self) -> None:
        settings = AppSettings()
        settings.engine.cloud_provider = "openai"
        sidebar = self._make_sidebar(settings)

        sidebar.base_url_input.setText("   ")
        sidebar._on_base_url_changed()

        self.assertEqual(sidebar.base_url_input.text(), OPENAI_BASE_URL)
        self.assertEqual(settings.engine.cloud_base_url, OPENAI_BASE_URL)

    def test_custom_openai_blank_base_url_restores_previous_non_empty_value(self) -> None:
        settings = AppSettings()
        settings.engine.cloud_provider = "custom_openai"
        settings.engine.cloud_base_url = "https://api.example.test/v1"
        sidebar = self._make_sidebar(settings)

        sidebar.base_url_input.setText("")
        sidebar._on_base_url_changed()

        self.assertEqual(sidebar.base_url_input.text(), "https://api.example.test/v1")

    def test_cloud_provider_without_base_url_keeps_disabled_placeholder(self) -> None:
        sidebar = self._make_sidebar(AppSettings())

        sidebar.provider_combo.setCurrentText("智谱 GLM")
        self.app.processEvents()

        self.assertFalse(sidebar.base_url_input.isEnabled())
        self.assertEqual(sidebar.base_url_input.text(), "")
        self.assertEqual(
            sidebar.base_url_input.placeholderText(),
            DISABLED_BASE_URL_PLACEHOLDER,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
