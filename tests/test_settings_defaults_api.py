"""GET /api/settings/defaults: the factory defaults each translation page compares
its own settings against, to say 「采用应用默认值」 or 「采用上次使用的设置」."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

import settings as settings_module
from api.app import create_app
from settings import AppSettings, ExtensionSettings
from tests.app_data_isolation import IsolatedAppDataTestCase


class SettingsDefaultsApiTests(IsolatedAppDataTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.client = TestClient(create_app())

    def test_fresh_install_matches_the_defaults(self) -> None:
        defaults = self.client.get("/api/settings/defaults").json()
        current = self.client.get("/api/settings").json()
        self.assertEqual(defaults["app"], AppSettings().model_dump(mode="json"))
        self.assertEqual(defaults["extension"], ExtensionSettings().model_dump(mode="json"))
        for key in ("excel_output", "excel_target_lang", "word_review", "pdf", "pdf_output", "cad_output"):
            self.assertEqual(current[key], defaults["app"][key], key)

    def test_defaults_ignore_saved_settings_and_leave_the_file_alone(self) -> None:
        self.client.put("/api/settings", json={"excel_target_lang": "de", "cad_output": {"display_mode": "bilingual"}})
        before = settings_module.SETTINGS_PATH.read_text(encoding="utf-8")
        defaults = self.client.get("/api/settings/defaults").json()["app"]
        current = self.client.get("/api/settings").json()
        self.assertEqual(current["excel_target_lang"], "de")
        self.assertEqual(defaults["excel_target_lang"], AppSettings().excel_target_lang)
        self.assertEqual(defaults["cad_output"]["display_mode"], AppSettings().cad_output.display_mode)
        self.assertEqual(settings_module.SETTINGS_PATH.read_text(encoding="utf-8"), before)
        json.loads(before)
