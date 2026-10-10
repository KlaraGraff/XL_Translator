from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from api.app import create_app
from core import diagnostics, tm_manager
import settings as settings_module


class CanonicalPromptResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        app_data = root / "app-data"
        for patcher in (
            patch.multiple(
                settings_module,
                APP_DATA_DIR=app_data,
                SETTINGS_PATH=app_data / "settings.json",
                KEYS_PATH=app_data / "keys.json",
            ),
            patch.object(tm_manager, "DB_PATH", app_data / "tm.db"),
            patch.object(diagnostics, "DIAGNOSTIC_RECORDS_DIR", root / "diagnostics"),
            patch.object(diagnostics, "LOG_PATH", app_data / "app.log"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(create_app())

    def test_builtin_base_prompt_is_used_for_every_target_language(self) -> None:
        from config import DOMAIN_PRESETS
        from core.engine_dispatcher import get_system_prompt
        from settings import AppSettings

        settings = AppSettings(domain_preset="同步工程场景", target_lang="fr")
        prompt = get_system_prompt(settings, "fr")
        self.assertIn(DOMAIN_PRESETS["同步工程场景"]["_base"], prompt)
        self.assertNotIn("Tu es un assistant", prompt)

    def test_canonical_override_wins_over_legacy_language_entries(self) -> None:
        from core.engine_dispatcher import get_system_prompt
        from settings import AppSettings

        settings = AppSettings(
            domain_preset="同步工程场景",
            target_lang="fr",
            domain_prompt_overrides={
                "同步工程场景": {"_base": "统一主提示词", "fr": "旧法语版本"}
            },
        )
        prompt = get_system_prompt(settings, "fr")
        self.assertIn("统一主提示词", prompt)
        self.assertNotIn("旧法语版本", prompt)

class CanonicalPromptPutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        app_data = root / "app-data"
        for patcher in (
            patch.multiple(settings_module, APP_DATA_DIR=app_data, SETTINGS_PATH=app_data / "settings.json", KEYS_PATH=app_data / "keys.json"),
            patch.object(tm_manager, "DB_PATH", app_data / "tm.db"),
            patch.object(diagnostics, "DIAGNOSTIC_RECORDS_DIR", root / "diagnostics"),
            patch.object(diagnostics, "LOG_PATH", app_data / "app.log"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(create_app())

    def test_put_collapses_legacy_language_maps(self) -> None:
        response = self.client.put("/api/domains/excel", json={
            "preset": "同步工程场景",
            "prompt_overrides": {"同步工程场景": {"fr": "法语旧版本", "en": "英语旧版本"}},
            "custom_prompts": {"我的预设": {"fr": "自定义旧版本"}},
        })
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["prompt_overrides"], {"同步工程场景": {"_base": "法语旧版本"}})
        self.assertEqual(body["custom_prompts"], {"我的预设": {"_base": "自定义旧版本"}})
