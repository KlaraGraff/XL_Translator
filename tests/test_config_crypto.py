"""导出配置文件加密：core/config_crypto.py 的内核测试，以及导出/导入端点的集成测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import settings as settings_module
from api.app import create_app
from core import diagnostics, tm_manager
from core.config_crypto import (
    SEALED_FIELD,
    UNSEAL_CORRUPT,
    UNSEAL_EXPIRED,
    UNSEAL_OK,
    UNSEAL_PLAINTEXT,
    UNSEAL_UNSUPPORTED,
    seal_model_config_document,
    unseal_model_config_document,
)


def _sample_document() -> dict:
    """一份带三处密钥的合成文档：connections[]、cloud 块、provider_configs{}。"""
    return {
        "type": "translator_model_config",
        "version": 3,
        "connections": [
            {
                "id": "conn-1",
                "provider": "custom_openai",
                "base_url": "https://a.example/v1",
                "api_key": "sk-CONN-ONE",
            },
            {
                "id": "conn-2",
                "provider": "dashscope",
                "base_url": "https://b.example/v1",
                "api_key": "sk-CONN-TWO",
            },
        ],
        "model_profiles": {
            "translation": {
                "cloud": {
                    "provider": "custom_openai",
                    "model": "some-model",
                    "base_url": "https://cloud.example/v1",
                    "api_key": "sk-CLOUD-KEY",
                },
            },
        },
        "provider_configs": {
            "deepseek": {
                "model": "deepseek-chat",
                "api_key": "sk-PROVIDER-MEM",
            },
        },
    }


class SealUnsealRoundTripTests(unittest.TestCase):
    def test_round_trip_removes_plaintext_and_restores_every_path(self) -> None:
        document = _sample_document()
        sealed = seal_model_config_document(document)

        serialized = json.dumps(sealed, ensure_ascii=False)
        for plaintext_key in ("sk-CONN-ONE", "sk-CONN-TWO", "sk-CLOUD-KEY", "sk-PROVIDER-MEM"):
            self.assertNotIn(plaintext_key, serialized)
        self.assertNotIn('"api_key"', serialized)

        result = unseal_model_config_document(sealed)
        self.assertEqual(result.status, UNSEAL_OK)
        self.assertEqual(result.key_count, 4)
        self.assertEqual(
            result.document["connections"][0]["api_key"], "sk-CONN-ONE"
        )
        self.assertEqual(
            result.document["connections"][1]["api_key"], "sk-CONN-TWO"
        )
        self.assertEqual(
            result.document["model_profiles"]["translation"]["cloud"]["api_key"],
            "sk-CLOUD-KEY",
        )
        self.assertEqual(
            result.document["provider_configs"]["deepseek"]["api_key"],
            "sk-PROVIDER-MEM",
        )
        # 非密钥字段原样保留。
        self.assertEqual(result.document["connections"][0]["base_url"], "https://a.example/v1")

    def test_original_document_is_not_mutated(self) -> None:
        document = _sample_document()
        original = deepcopy(document)
        seal_model_config_document(document)
        self.assertEqual(document, original)

    def test_long_lived_seal_has_no_expiry(self) -> None:
        document = _sample_document()
        sealed = seal_model_config_document(document, valid_days=None)
        self.assertIsNone(sealed[SEALED_FIELD]["expires_at"])

        result = unseal_model_config_document(sealed)
        self.assertEqual(result.status, UNSEAL_OK)
        self.assertIsNone(result.expires_at)

    def test_expired_seal_reports_expired_and_leaks_nothing(self) -> None:
        document = _sample_document()
        sealed = seal_model_config_document(document, valid_days=-1)

        result = unseal_model_config_document(sealed)
        self.assertEqual(result.status, UNSEAL_EXPIRED)
        serialized = json.dumps(result.document, ensure_ascii=False)
        self.assertNotIn("sk-CONN-ONE", serialized)
        self.assertNotIn("sk-CLOUD-KEY", serialized)
        self.assertNotIn("sk-PROVIDER-MEM", serialized)

    def test_tampering_the_body_is_reported_as_corrupt(self) -> None:
        document = _sample_document()
        sealed = seal_model_config_document(document)
        sealed["connections"][0]["base_url"] = "https://evil.example/v1"

        result = unseal_model_config_document(sealed)
        self.assertEqual(result.status, UNSEAL_CORRUPT)

    def test_tampering_the_expiry_is_reported_as_corrupt_not_expired(self) -> None:
        document = _sample_document()
        sealed = seal_model_config_document(document, valid_days=1)
        # 往后延有效期：这本该让「过期」的文件看起来还没过期，AAD 必须拦下它。
        sealed[SEALED_FIELD]["expires_at"] = "2099-01-01T00:00:00Z"

        result = unseal_model_config_document(sealed)
        self.assertEqual(result.status, UNSEAL_CORRUPT)

    def test_unknown_key_id_is_unsupported(self) -> None:
        document = _sample_document()
        sealed = seal_model_config_document(document)
        sealed[SEALED_FIELD]["key_id"] = "some-future-key"

        result = unseal_model_config_document(sealed)
        self.assertEqual(result.status, UNSEAL_UNSUPPORTED)
        self.assertNotIn(SEALED_FIELD, result.document)

    def test_legacy_plaintext_document_is_recognized(self) -> None:
        document = _sample_document()  # 没有 sealed_keys，天然就是「旧版明文」文档

        result = unseal_model_config_document(document)
        self.assertEqual(result.status, UNSEAL_PLAINTEXT)
        self.assertEqual(result.key_count, 4)
        self.assertEqual(result.document, document)

    def test_document_without_any_key_gets_no_sealed_field(self) -> None:
        document = {
            "type": "translator_model_config",
            "version": 3,
            "connections": [
                {"id": "conn-1", "provider": "custom_openai", "base_url": "https://a.example/v1"},
            ],
        }
        sealed = seal_model_config_document(document)
        self.assertNotIn(SEALED_FIELD, sealed)

        # 没有 sealed_keys 字段，落进 unseal 的「旧版明文」分支——里面就是没有 key。
        result = unseal_model_config_document(sealed)
        self.assertEqual(result.status, UNSEAL_PLAINTEXT)
        self.assertEqual(result.key_count, 0)

    def test_two_seals_of_the_same_document_produce_different_ciphertext(self) -> None:
        document = _sample_document()
        sealed_a = seal_model_config_document(document)
        sealed_b = seal_model_config_document(document)

        self.assertNotEqual(
            sealed_a[SEALED_FIELD]["ciphertext"], sealed_b[SEALED_FIELD]["ciphertext"]
        )
        self.assertNotEqual(sealed_a[SEALED_FIELD]["nonce"], sealed_b[SEALED_FIELD]["nonce"])
        self.assertNotEqual(sealed_a[SEALED_FIELD]["epk"], sealed_b[SEALED_FIELD]["epk"])
        # 但两份密文各自都能正常解开，值也一致。
        for sealed in (sealed_a, sealed_b):
            result = unseal_model_config_document(sealed)
            self.assertEqual(result.status, UNSEAL_OK)
            self.assertEqual(result.document["connections"][0]["api_key"], "sk-CONN-ONE")


class ExportImportEndpointSealTests(unittest.TestCase):
    """通过真实的 FastAPI 端点，覆盖 export/preview/import 里的密封与解封分支。"""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for patcher in (
            patch.multiple(
                settings_module,
                APP_DATA_DIR=self.root / "app-data",
                SETTINGS_PATH=self.root / "app-data" / "settings.json",
                KEYS_PATH=self.root / "app-data" / "keys.json",
            ),
            patch.object(tm_manager, "DB_PATH", self.root / "app-data" / "tm.db"),
            patch.object(diagnostics, "DIAGNOSTIC_RECORDS_DIR", self.root / "diag"),
            patch.object(diagnostics, "LOG_PATH", self.root / "app-data" / "app.log"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(create_app())

    def _seed_key(self) -> None:
        response = self.client.put(
            "/api/keys/custom_openai",
            json={"api_key": "sk-MINE", "base_url": "https://mine.example/v1"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        response = self.client.put(
            "/api/models/roles/translation",
            json={
                "mode": "cloud",
                "provider": "custom_openai",
                "model": "mine-model",
                "base_url": "https://mine.example/v1",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)

    def test_export_rejects_out_of_range_valid_days(self) -> None:
        for bad_value in (-1, 3651):
            response = self.client.get(
                "/api/model-config/export"
                f"?include_api_key=true&confirm_sensitive=true&valid_days={bad_value}"
            )
            self.assertEqual(response.status_code, 422, response.text)

    def test_export_with_keys_is_sealed_and_hides_plaintext(self) -> None:
        self._seed_key()

        response = self.client.get(
            "/api/model-config/export?include_api_key=true&confirm_sensitive=true"
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["sealed"])
        self.assertNotIn("sk-MINE", json.dumps(body["document"], ensure_ascii=False))
        self.assertIn(SEALED_FIELD, body["document"])

    def test_export_without_keys_is_not_sealed(self) -> None:
        self._seed_key()

        response = self.client.get("/api/model-config/export")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertFalse(body["sealed"])
        self.assertNotIn(SEALED_FIELD, body["document"])

    def test_preview_and_import_unseal_a_sealed_document(self) -> None:
        self._seed_key()
        exported = self.client.get(
            "/api/model-config/export?include_api_key=true&confirm_sensitive=true"
        ).json()
        sealed_document = exported["document"]

        preview = self.client.post(
            "/api/model-config/import/preview", json=sealed_document
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        preview_body = preview.json()
        self.assertEqual(preview_body["seal_status"], UNSEAL_OK)
        self.assertTrue(preview_body["sealed"])
        self.assertFalse(preview_body["legacy_plaintext"])
        self.assertGreaterEqual(preview_body["sealed_key_count"], 1)
        self.assertIn("expires_at", preview_body)

        imported = self.client.post("/api/model-config/import", json=sealed_document)
        self.assertEqual(imported.status_code, 200, imported.text)
        imported_body = imported.json()
        self.assertEqual(imported_body["seal_status"], UNSEAL_OK)
        self.assertTrue(imported_body["sealed"])
        self.assertFalse(imported_body["legacy_plaintext"])

    def test_preview_and_import_reject_a_tampered_sealed_document(self) -> None:
        self._seed_key()
        exported = self.client.get(
            "/api/model-config/export?include_api_key=true&confirm_sensitive=true"
        ).json()
        tampered = deepcopy(exported["document"])
        tampered["model_profiles"]["translation"]["cloud"]["base_url"] = "https://evil.example/v1"

        preview = self.client.post("/api/model-config/import/preview", json=tampered)
        self.assertEqual(preview.status_code, 422, preview.text)

        settings_before = self.client.get("/api/models/roles")

        imported = self.client.post("/api/model-config/import", json=tampered)
        self.assertEqual(imported.status_code, 422, imported.text)

        settings_after = self.client.get("/api/models/roles")
        # 篡改文档被拒后，任何配置都没被写入。
        self.assertEqual(settings_before.json(), settings_after.json())


class CorruptDetailContractTests(unittest.TestCase):
    """界面靠这句原话认出「文件被改过」这条分支，两边的字面量必须逐字相同。

    settings.ts 里是 `errorMessage(error) === CORRUPT_IMPORT_DETAIL` 的字符串相等
    判断。后端改个标点，那个专门的弹窗就会静默退化成一条通用红色 toast——用户再也
    看不到「请联系发送方确认文件来源」这句唯一的安全提示，而且没有任何测试会红。
    这条测试就是那个机器验证。
    """

    def test_the_frontend_carries_the_exact_same_sentence(self) -> None:
        from api.app import CORRUPT_IMPORT_DETAIL

        settings_ts = (
            Path(__file__).resolve().parents[1] / "ui" / "src" / "views" / "settings.ts"
        ).read_text(encoding="utf-8")
        self.assertIn(
            f'const CORRUPT_IMPORT_DETAIL = "{CORRUPT_IMPORT_DETAIL}";',
            settings_ts,
            "ui/src/views/settings.ts 里的 CORRUPT_IMPORT_DETAIL 和 api/app.py 的不一致了",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
