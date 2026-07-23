from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from api.app import create_app
from core.language_preflight import parse_preflight_response, preflight_files_source_languages
from core.language_registry import (
    AUTO_SOURCE_LANG,
    append_custom_target_lang,
    get_language_catalog,
    get_source_language_options,
    get_target_language_options,
)
from settings import AppSettings


class Phase1LanguageTests(unittest.TestCase):
    def test_catalog_contains_all_builtin_languages_and_auto_source(self) -> None:
        catalog = get_language_catalog()
        self.assertEqual(len(catalog), 59)
        self.assertEqual(len({item["code"] for item in catalog}), 59)
        self.assertEqual(get_source_language_options()[0]["code"], AUTO_SOURCE_LANG)
        self.assertEqual(len(get_source_language_options()), 60)
        self.assertEqual(len(get_target_language_options()), 59)

    def test_custom_language_is_target_only_with_stable_code(self) -> None:
        languages, code = append_custom_target_lang([], "工程方言", "项目术语")
        self.assertTrue(code.startswith("x-custom-"))
        self.assertEqual(languages[0].code, code)
        self.assertFalse(any(item["code"] == code for item in get_source_language_options(languages)))
        target_codes = {item["code"] for item in get_target_language_options(languages)}
        self.assertIn(code, target_codes)

    def test_preflight_is_once_per_file_and_omits_empty_files(self) -> None:
        calls: list[list[str]] = []

        def detector(_system: str, user: str) -> object:
            calls.append([user])
            return {"source_langs": ["zh"]}

        result = preflight_files_source_languages(
            {
                "one.xlsx": ["=SUM(A1:A2)", "123", "工程进度说明", "工程进度说明"],
                "empty.xlsx": ["", "2026-01-01", "=A1"],
                "two.xlsx": ["Bonjour le monde"],
            },
            target_lang="en",
            request=detector,
        )
        self.assertEqual(len(calls), 2)
        self.assertTrue(result["one.xlsx"].requested)
        self.assertFalse(result["empty.xlsx"].requested)
        self.assertEqual(result["one.xlsx"].source_langs, ("zh",))
        self.assertIn("工程进度说明", calls[0][0])
        self.assertNotIn("=SUM", calls[0][0])

    def test_preflight_parser_returns_at_most_two_real_codes(self) -> None:
        codes, uncertain, error = parse_preflight_response(
            json.dumps({"source_langs": ["fr", "en", "mixed", "xx"]})
        )
        self.assertEqual(codes, ("fr", "en"))
        self.assertTrue(uncertain)
        self.assertEqual(error, "")

    def test_language_api_exposes_catalog_and_custom_target(self) -> None:
        settings = AppSettings()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch("api.app.load_settings", return_value=settings), patch(
                "api.app.save_settings"
            ) as save_settings:
                client = TestClient(create_app())
                payload = client.get("/api/languages").json()
                self.assertEqual(len(payload["languages"]), 59)
                self.assertEqual(payload["source_options"][0]["code"], "auto")
                self.assertEqual(client.post("/api/languages/custom", json={"name": "项目语", "description": "内部"}).status_code, 201)
                save_settings.assert_called()
            (root / "language-api.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )


if __name__ == "__main__":
    unittest.main()
