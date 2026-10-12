"""翻译参数里的「失败重试次数」（Excel、CAD 各一项，settings v32 起）。"""

from __future__ import annotations

import unittest

from pydantic import ValidationError

from config import REQUEST_RETRY_ATTEMPTS_DEFAULT
from core.document_config import DOCUMENT_CONFIG_SECTIONS, build_document_config_export_payload
from settings import AppSettings


class RequestRetrySettingsTests(unittest.TestCase):
    def test_an_older_settings_file_gets_the_default(self) -> None:
        # v31 及更早的文件没有这一节：加法改动，读取时补默认值。
        settings = AppSettings.model_validate({"settings_version": 31})
        self.assertEqual(settings.request_retry.excel, REQUEST_RETRY_ATTEMPTS_DEFAULT)
        self.assertEqual(settings.request_retry.cad, REQUEST_RETRY_ATTEMPTS_DEFAULT)

    def test_bounds(self) -> None:
        self.assertEqual(AppSettings.model_validate({"request_retry": {"excel": 0, "cad": 5}}).request_retry.cad, 5)
        for bad in (-1, 6):
            with self.subTest(value=bad), self.assertRaises(ValidationError):
                AppSettings.model_validate({"request_retry": {"excel": bad}})

    def test_travels_with_the_document_config_bundle(self) -> None:
        self.assertIn("request_retry", DOCUMENT_CONFIG_SECTIONS)
        settings = AppSettings.model_validate({"request_retry": {"excel": 4, "cad": 1}})
        exported = build_document_config_export_payload(settings)
        self.assertEqual(exported["document"]["request_retry"], {"excel": 4, "cad": 1})


if __name__ == "__main__":
    unittest.main()
