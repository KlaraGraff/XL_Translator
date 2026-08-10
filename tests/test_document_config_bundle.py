"""The document-translation bundle: one file, three pages, no secrets.

The previous design exported one model at a time and had nothing at all for the
translation parameters, so moving a setup to another machine meant repeating the
same dialog per section.  These tests pin the two properties that make a single
bundle safe to hand to someone else: it carries no machine-local path, and an
import only writes the fields the file actually names.
"""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from api.app import create_app
from core.document_config import (
    DOCUMENT_CONFIG_EXPORT_TYPE,
    apply_document_config_import,
    build_document_config_export_payload,
    parse_document_config_import,
    summarize_document_config_import,
)
from core.language_registry import CustomTargetLang
from settings import AppSettings
from tests.app_data_isolation import IsolatedAppDataTestCase


def _configured_settings() -> AppSettings:
    app = AppSettings()
    app.word_batch.max_paragraphs_per_batch = 13
    app.excel_review.existing_fill_policy = "overwrite"
    app.pdf.page_retry_attempts = 5
    app.excel_domain_preset = "无"
    app.word_custom_prompt = "照原样直译。"
    app.output.use_custom_output_dir = True
    app.output.custom_output_dir = "/Users/someone/译文"
    return AppSettings(**app.model_dump())


class DocumentConfigExportTests(unittest.TestCase):
    def test_export_carries_every_page_in_one_bundle(self) -> None:
        payload = build_document_config_export_payload(_configured_settings())
        document = payload["document"]

        self.assertEqual(payload["type"], DOCUMENT_CONFIG_EXPORT_TYPE)
        self.assertEqual(document["word_batch"]["max_paragraphs_per_batch"], 13)
        self.assertEqual(document["excel_review"]["existing_fill_policy"], "overwrite")
        self.assertEqual(document["pdf"]["page_retry_attempts"], 5)
        self.assertEqual(document["excel_domain_preset"], "无")
        self.assertEqual(document["word_custom_prompt"], "照原样直译。")

    def test_export_leaves_out_this_machines_output_directory(self) -> None:
        document = build_document_config_export_payload(_configured_settings())["document"]

        for section in ("output", "excel_output", "word_output", "pdf_output"):
            self.assertNotIn("custom_output_dir", document[section])
            self.assertNotIn("use_custom_output_dir", document[section])
        self.assertNotIn(
            "/Users/someone/译文",
            str(document),
            "导出的包里不能带上导出者机器上的输出目录。",
        )

    def test_export_never_carries_a_model_or_a_key(self) -> None:
        document = build_document_config_export_payload(_configured_settings())["document"]

        for forbidden in ("engine", "cleaner_model_role", "image_model_role", "api_key"):
            self.assertNotIn(forbidden, document)


class DocumentConfigImportTests(unittest.TestCase):
    def test_round_trip_restores_every_exported_value(self) -> None:
        exported = build_document_config_export_payload(_configured_settings())

        restored = apply_document_config_import(
            AppSettings(), parse_document_config_import(exported)
        )

        self.assertEqual(restored.word_batch.max_paragraphs_per_batch, 13)
        self.assertEqual(restored.excel_review.existing_fill_policy, "overwrite")
        self.assertEqual(restored.pdf.page_retry_attempts, 5)
        self.assertEqual(restored.excel_domain_preset, "无")
        self.assertEqual(restored.word_custom_prompt, "照原样直译。")

    def test_a_sparse_file_leaves_unmentioned_settings_alone(self) -> None:
        current = _configured_settings()
        sparse = {
            "type": DOCUMENT_CONFIG_EXPORT_TYPE,
            "version": 1,
            "document": {"pdf": {"page_retry_attempts": 1}},
        }

        merged = apply_document_config_import(
            current, parse_document_config_import(sparse)
        )

        self.assertEqual(merged.pdf.page_retry_attempts, 1)
        self.assertEqual(merged.word_batch.max_paragraphs_per_batch, 13)
        self.assertEqual(merged.excel_domain_preset, "无")

    def test_import_keeps_the_importing_machines_output_directory(self) -> None:
        current = _configured_settings()
        hostile = {
            "type": DOCUMENT_CONFIG_EXPORT_TYPE,
            "version": 1,
            "document": {
                "output": {
                    "enable_task_log": True,
                    "custom_output_dir": "/Volumes/别人的盘/输出",
                    "use_custom_output_dir": True,
                }
            },
        }

        merged = apply_document_config_import(
            current, parse_document_config_import(hostile)
        )

        self.assertTrue(merged.output.enable_task_log)
        self.assertEqual(merged.output.custom_output_dir, "/Users/someone/译文")

    def test_the_model_bundle_is_rejected_with_a_pointer_to_the_right_page(self) -> None:
        with self.assertRaises(ValueError) as raised:
            parse_document_config_import(
                {"type": "translator_model_config", "version": 3, "model_profiles": {}}
            )

        self.assertIn("模型服务", str(raised.exception))

    def test_a_newer_bundle_is_refused_rather_than_half_applied(self) -> None:
        with self.assertRaises(ValueError):
            parse_document_config_import(
                {"type": DOCUMENT_CONFIG_EXPORT_TYPE, "version": 99, "document": {}}
            )

    def test_a_file_without_a_version_says_the_file_is_wrong(self) -> None:
        """缺版本号不等于「你的应用太旧」，提示不能把用户支去升级。"""
        with self.assertRaises(ValueError) as raised:
            parse_document_config_import(
                {"type": DOCUMENT_CONFIG_EXPORT_TYPE, "document": {"pdf": {}}}
            )

        message = str(raised.exception)
        self.assertIn("版本号", message)
        self.assertNotIn("更高版本", message)

    def test_import_never_removes_a_custom_language(self) -> None:
        """TM 的译文按语言存，导入把本机语言删了等于把那批译文弄丢。"""
        current = AppSettings()
        current.custom_target_langs = [CustomTargetLang(name="闽南语")]
        current = AppSettings(**current.model_dump())
        local_code = current.custom_target_langs[0].code

        merged = apply_document_config_import(
            current,
            parse_document_config_import(
                {
                    "type": DOCUMENT_CONFIG_EXPORT_TYPE,
                    "version": 1,
                    "document": {
                        "custom_target_langs": [
                            {"name": "粤语", "description": "", "code": "x-custom-yue"}
                        ]
                    },
                }
            ),
        )

        names = [entry.name for entry in merged.custom_target_langs]
        self.assertIn("闽南语", names)
        self.assertIn("粤语", names)
        # 本机那条的语言代码必须原样保留，否则库里的词条对不上任何语言对。
        self.assertEqual(merged.custom_target_langs[0].code, local_code)

    def test_summary_names_the_areas_a_file_would_change(self) -> None:
        imported = parse_document_config_import(
            {
                "type": DOCUMENT_CONFIG_EXPORT_TYPE,
                "version": 1,
                "document": {"pdf": {"page_retry_attempts": 1}, "word_custom_prompt": "x"},
            }
        )

        self.assertEqual(summarize_document_config_import(imported), ["PDF 参数", "领域与提示词"])


class DocumentConfigRouteTests(IsolatedAppDataTestCase):
    """The routes save settings, so they run against an isolated data dir."""

    def setUp(self) -> None:
        super().setUp()
        self.client = TestClient(create_app())

    def test_export_then_import_round_trips_through_the_api(self) -> None:
        self.client.put(
            "/api/settings",
            json={"word_batch": {"max_paragraphs_per_batch": 13}},
        )
        exported = self.client.get("/api/document-config/export").json()

        self.client.put(
            "/api/settings",
            json={"word_batch": {"max_paragraphs_per_batch": 5}},
        )
        preview = self.client.post("/api/document-config/import/preview", json=exported)
        applied = self.client.post("/api/document-config/import", json=exported)

        self.assertEqual(preview.status_code, 200)
        self.assertIn("Word 批次与重试", preview.json()["areas"])
        self.assertEqual(applied.status_code, 200)
        self.assertEqual(
            applied.json()["settings"]["word_batch"]["max_paragraphs_per_batch"], 13
        )

    def test_a_wrong_file_gets_a_422_and_changes_nothing(self) -> None:
        before = self.client.get("/api/document-config/export").json()

        response = self.client.post("/api/document-config/import", json={"type": "nope"})

        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.client.get("/api/document-config/export").json(), before)


if __name__ == "__main__":  # pragma: no cover - convenience runner
    unittest.main()
