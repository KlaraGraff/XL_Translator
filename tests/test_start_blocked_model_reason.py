"""没配模型就点开始：拦截要带稳定的 reason，界面才能放「去设置」（界面走查 B10）。

以前这几条拦截只有一句中文，reason 是通用的 invalid_input，界面认不出来，弹窗只能
给一个「知道了」，用户得自己找到「设置 › 模型服务」。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from api.app import create_app
from api.surfaces import get_surface
from api.task_contracts import (
    REASON_IMAGE_MODEL_MISSING,
    REASON_PDF_REVIEW_MODEL_MISSING,
    REASON_TRANSLATION_MODEL_MISSING,
    TaskInputError,
    TaskOptions,
)
from api.task_manager import TranslationTaskManager
from core.file_scanner import FileItem
from settings import AppSettings, set_cloud_provider_config
from tests.app_data_isolation import IsolatedAppDataTestCase


def _no_model_settings() -> AppSettings:
    settings = AppSettings(target_lang="en")
    settings.engine.mode = "cloud"
    settings.engine.cloud_provider = "custom_openai"
    set_cloud_provider_config(settings.engine, "custom_openai", cloud_model="", cloud_base_url="")
    return settings


class StartBlockedModelReasonTests(IsolatedAppDataTestCase):
    def test_text_surfaces_tag_missing_translation_model(self) -> None:
        for surface in ("excel", "word", "cad"):
            with self.subTest(surface=surface), patch(
                "api.surfaces.cad.probe_status", return_value={"enabled": True}
            ):
                with self.assertRaises(TaskInputError) as caught:
                    get_surface(surface).validate_preflight(
                        files=[FileItem(path=Path(f"a.{'dxf' if surface == 'cad' else 'xlsx'}"), name="a", size_kb=1.0)],
                        settings=_no_model_settings(),
                        options=TaskOptions(),
                    )
                self.assertEqual(caught.exception.reason, REASON_TRANSLATION_MODEL_MISSING)

    def test_pdf_tags_missing_image_and_review_models(self) -> None:
        settings = AppSettings()
        settings.pdf.target_lang = "en"
        with self.assertRaises(TaskInputError) as caught:
            get_surface("pdf").validate_preflight(
                files=[object()], settings=settings, options=TaskOptions()
            )
        self.assertEqual(caught.exception.reason, REASON_IMAGE_MODEL_MISSING)

        image_role = settings.image_model_role
        image_role.cloud_provider = "custom_openai"
        set_cloud_provider_config(
            image_role,
            "custom_openai",
            cloud_model="mock-image-model",
            cloud_base_url="https://image.example/v1",
        )
        settings.pdf.review_enabled = True
        with patch("core.model_roles.get_key", return_value="secret"), self.assertRaises(TaskInputError) as caught:
            get_surface("pdf").validate_preflight(
                files=[object()], settings=settings, options=TaskOptions()
            )
        self.assertEqual(caught.exception.reason, REASON_PDF_REVIEW_MODEL_MISSING)

    def test_start_route_returns_reason_to_the_page(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "a.xlsx"
            source.write_bytes(b"")
            settings = _no_model_settings()
            manager = TranslationTaskManager(settings_loader=lambda: settings)
            manager._scan = lambda *_args: [FileItem(path=source, name="a", size_kb=1.0)]
            client = TestClient(create_app(task_manager=manager))
            response = client.post("/api/tasks", json={"surface": "excel", "source_path": str(root)})

        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["reason"], REASON_TRANSLATION_MODEL_MISSING)


if __name__ == "__main__":
    unittest.main(verbosity=2)
