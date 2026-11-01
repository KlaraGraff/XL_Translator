"""PDF 任务得出的图像模型结论要存进 settings.json（审计 2026-05-25:221 第 2/3 条）。

以前 runner 只改任务自己那份设置副本：任务成功后设置页仍写「未测试」，模型级不可用
也是重启就忘。用例全部离线：假图像客户端 + 假 pdfium，零真实 API 调用。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import settings as settings_module
from core.model_roles import ROLE_IMAGE, update_role_connection
from core.pdf_image_translation import PdfFileItem, PdfImageTranslationRunner
from tests.test_pdf_image_translation import (
    _FakeImageClient,
    _UnavailableImageClient,
    _fake_pdfium_module_by_page_count,
    _page_review_settings,
    _png_bytes,
)


class PdfAvailabilitySavedTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        data = self.root / "data"
        for patcher in (
            patch.object(settings_module, "APP_DATA_DIR", data),
            patch.object(settings_module, "SETTINGS_PATH", data / "settings.json"),
            patch.object(settings_module, "KEYS_PATH", data / "keys.json"),
            patch("core.model_roles.get_key", return_value="secret"),
            patch.dict(sys.modules, {"pypdfium2": _fake_pdfium_module_by_page_count({"source.pdf": 1})}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.settings = _page_review_settings(self.root)
        settings_module.save_settings(self.settings)
        self.source = self.root / "source.pdf"
        self.source.write_bytes(b"%PDF-1.4\n")

    def _run(self, client, task_settings=None) -> None:
        runner = PdfImageTranslationRunner(
            [PdfFileItem(path=self.source, name="source", size_kb=1.0, page_count=1)],
            task_settings or settings_module.load_settings(),
            source_root=self.root,
            image_client=client,
            task_logger_enabled=False,
        )
        runner._run()

    def _stored_status(self) -> str:
        return settings_module.load_settings().image_model_role.availability_status

    def test_a_successful_task_saves_available(self) -> None:
        self.assertEqual(self._stored_status(), "unknown")
        self._run(_FakeImageClient(_png_bytes(1200, 1600)))
        self.assertEqual(self._stored_status(), "available")

    def test_a_model_level_failure_saves_unavailable(self) -> None:
        self._run(_UnavailableImageClient())
        stored = settings_module.load_settings().image_model_role
        self.assertEqual(stored.availability_status, "unavailable")
        self.assertIn("密钥", stored.availability_message)

    def test_a_verdict_for_a_model_the_user_since_replaced_is_not_saved(self) -> None:
        task_settings = settings_module.load_settings()
        # 任务跑着的时候用户换了模型：任务的结论不属于新模型。
        changed = settings_module.load_settings()
        update_role_connection(
            changed,
            ROLE_IMAGE,
            changed.image_model_role.connections[0].id,
            model="another-image-model",
        )
        settings_module.save_settings(changed)

        self._run(_FakeImageClient(_png_bytes(1200, 1600)), task_settings)

        stored = settings_module.load_settings().image_model_role
        self.assertEqual(stored.connections[0].cloud_model, "another-image-model")
        self.assertEqual(stored.availability_status, "unknown")
        self.assertEqual(stored.connections[0].availability_status, "unknown")


if __name__ == "__main__":
    unittest.main()
