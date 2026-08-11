"""导入来的密钥不能被再次导出——挡住 A→B→C 的连环传播。

「导出含 Key」以前会把本机密钥库里的所有密钥一并带走，包括那些本来就是从别人的配置
文件导入进来的：A 把配置连密钥给了 B，B 再导一份给 C，A 的密钥就到了 C 手上，而 A
完全不知情。现在密钥带一份旁路的来源标记，导出时按来源过滤，并给出逐条回执。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import settings as settings_module
from api.app import create_app
from core import diagnostics, tm_manager
from core.model_config import (
    API_KEY_EXPORT_INCLUDED,
    API_KEY_EXPORT_KIND_CONNECTION,
    API_KEY_EXPORT_KIND_PROVIDER_MEMORY,
    API_KEY_EXPORT_MISSING,
    API_KEY_EXPORT_WITHHELD_IMPORTED,
    build_model_config_export_payload,
)
from core.model_roles import ROLE_TRANSLATION, add_role_connection
from settings import (
    KEY_ORIGIN_IMPORTED,
    AppSettings,
    api_key_scope,
    connection_key_scope,
    delete_all_keys,
    delete_connection_key,
    delete_key,
    is_imported_connection_key,
    is_imported_provider_key,
    key_origins_path,
    load_imported_key_scopes,
    load_keys,
    save_connection_key,
    save_key,
)
from tests.app_data_isolation import IsolatedAppDataTestCase

PROVIDER = "deepseek"
BASE_URL = "https://api.deepseek.com/v1"


class KeyOriginMarkerTests(IsolatedAppDataTestCase):
    """来源标记本身：怎么写进去、怎么被清掉、老用户没有它时是什么行为。"""

    def test_an_old_install_without_the_marker_file_treats_every_key_as_its_own(
        self,
    ) -> None:
        # 升级前存下来的密钥没有任何标记文件陪着。那时候的导出行为必须一个字不改，
        # 否则用户升级完会突然发现导出的文件里少了东西。
        save_key(PROVIDER, "sk-existing", BASE_URL)
        key_origins_path().unlink(missing_ok=True)

        self.assertFalse(key_origins_path().exists())
        self.assertEqual(load_imported_key_scopes(), set())
        self.assertFalse(is_imported_provider_key(PROVIDER, BASE_URL))

    def test_an_imported_key_is_marked_and_never_touches_the_key_store_format(
        self,
    ) -> None:
        save_key(PROVIDER, "sk-theirs", BASE_URL, origin=KEY_ORIGIN_IMPORTED)

        scope = api_key_scope(PROVIDER, BASE_URL)
        self.assertTrue(is_imported_provider_key(PROVIDER, BASE_URL))
        self.assertIn(scope, load_imported_key_scopes())
        # keys.json 仍然是老的扁平 {作用域: 字符串} 表：一个读取方都不用改。
        self.assertEqual(load_keys()[scope], "sk-theirs")

    def test_saving_the_same_scope_again_locally_makes_it_exportable_again(self) -> None:
        save_key(PROVIDER, "sk-theirs", BASE_URL, origin=KEY_ORIGIN_IMPORTED)

        # 用户自己在面板上重新填了一次 Key，这把就是他自己的了。
        save_key(PROVIDER, "sk-mine", BASE_URL)

        self.assertFalse(is_imported_provider_key(PROVIDER, BASE_URL))
        self.assertEqual(load_imported_key_scopes(), set())

    def test_deleting_a_key_leaves_no_orphan_marker(self) -> None:
        save_key(PROVIDER, "sk-theirs", BASE_URL, origin=KEY_ORIGIN_IMPORTED)

        delete_key(PROVIDER, BASE_URL)

        self.assertEqual(load_imported_key_scopes(), set())
        # 孤儿标记会让之后填进同一作用域的新密钥被误判成「导入来的」而永远导不出去。
        save_key(PROVIDER, "sk-mine", BASE_URL)
        self.assertFalse(is_imported_provider_key(PROVIDER, BASE_URL))

    def test_connection_scoped_keys_follow_the_same_rules(self) -> None:
        save_connection_key("conn-1", "sk-theirs", origin=KEY_ORIGIN_IMPORTED)
        self.assertTrue(is_imported_connection_key("conn-1"))
        self.assertIn(connection_key_scope("conn-1"), load_imported_key_scopes())

        save_connection_key("conn-1", "sk-mine")
        self.assertFalse(is_imported_connection_key("conn-1"))

        save_connection_key("conn-2", "sk-theirs", origin=KEY_ORIGIN_IMPORTED)
        delete_connection_key("conn-2")
        self.assertEqual(load_imported_key_scopes(), set())

    def test_clearing_every_key_clears_every_marker(self) -> None:
        save_key(PROVIDER, "sk-theirs", BASE_URL, origin=KEY_ORIGIN_IMPORTED)
        save_connection_key("conn-1", "sk-theirs", origin=KEY_ORIGIN_IMPORTED)

        delete_all_keys()

        self.assertEqual(load_imported_key_scopes(), set())
        self.assertFalse(key_origins_path().exists())

    def test_a_corrupt_marker_file_never_blocks_saving_a_key(self) -> None:
        save_key(PROVIDER, "sk-theirs", BASE_URL, origin=KEY_ORIGIN_IMPORTED)
        key_origins_path().write_text("{ not json", encoding="utf-8")

        save_key(PROVIDER, "sk-mine", BASE_URL)

        self.assertEqual(load_keys()[api_key_scope(PROVIDER, BASE_URL)], "sk-mine")
        self.assertFalse(is_imported_provider_key(PROVIDER, BASE_URL))


def _two_connection_settings() -> tuple[AppSettings, list[str]]:
    """一个角色两条连接：主用 + 备用，端点相同，各自有自己的 Key。"""
    settings = AppSettings()
    settings.engine.cloud_provider = "custom_openai"
    settings.engine.cloud_base_url = "https://vendor.example/v1"
    settings.engine.cloud_model = "vendor-model"
    settings = AppSettings(**settings.model_dump())
    add_role_connection(
        settings,
        ROLE_TRANSLATION,
        label="同事给的账号",
        provider="custom_openai",
        model="vendor-model",
        base_url="https://vendor.example/v1",
    )
    settings = AppSettings(**settings.model_dump())
    return settings, [conn.id for conn in settings.engine.connections]


class WithKeysExportFiltersImportedKeysTests(unittest.TestCase):
    """导出含 Key：导入来的那部分扣下，连接的其余配置照常导出。"""

    def _export(
        self,
        settings: AppSettings,
        scoped: dict[str, str],
        imported_scoped: set[str],
        *,
        report: list[dict[str, str]] | None = None,
    ) -> dict:
        return build_model_config_export_payload(
            settings,
            get_api_key=lambda *_: "",
            get_scoped_api_key=lambda connection_id: scoped.get(connection_id, ""),
            is_imported_api_key=lambda *_: False,
            is_imported_scoped_api_key=lambda cid: cid in imported_scoped,
            include_api_key=True,
            api_key_report=report,
        )

    def test_an_imported_key_is_withheld_but_the_connection_still_exports(self) -> None:
        settings, ids = _two_connection_settings()
        report: list[dict[str, str]] = []

        payload = self._export(
            settings,
            {ids[0]: "sk-MINE", ids[1]: "sk-THEIRS"},
            {ids[1]},
            report=report,
        )

        connections = payload["model_profiles"]["translation"]["connections"]
        self.assertEqual(connections[0]["api_key"], "sk-MINE")
        self.assertNotIn("api_key", connections[1])
        # 密钥没了，其余配置一个字段都不能少——否则对方连「这条连接连的是谁」都不知道。
        self.assertEqual(connections[1]["label"], "同事给的账号")
        self.assertEqual(connections[1]["provider"], "custom_openai")
        self.assertEqual(connections[1]["model"], "vendor-model")
        self.assertEqual(connections[1]["base_url"], "https://vendor.example/v1")
        self.assertNotIn("sk-THEIRS", json.dumps(payload, ensure_ascii=False))

    def test_an_imported_primary_key_does_not_leak_through_the_cloud_block(self) -> None:
        settings, ids = _two_connection_settings()

        payload = self._export(settings, {ids[0]: "sk-THEIRS"}, {ids[0]})

        translation = payload["model_profiles"]["translation"]
        self.assertNotIn("api_key", translation["cloud"])
        self.assertNotIn("sk-THEIRS", json.dumps(payload, ensure_ascii=False))

    def test_a_provider_scoped_imported_key_is_withheld_everywhere(self) -> None:
        settings, _ids = _two_connection_settings()

        payload = build_model_config_export_payload(
            settings,
            get_api_key=lambda *_: "sk-THEIRS",
            get_scoped_api_key=lambda _cid: "",
            is_imported_api_key=lambda *_: True,
            is_imported_scoped_api_key=lambda _cid: False,
            include_api_key=True,
        )

        # 连接列表、cloud 块、以及「记住的服务商配置」三条路都不能漏。
        self.assertNotIn("sk-THEIRS", json.dumps(payload, ensure_ascii=False))

    def test_an_install_with_no_markers_exports_exactly_what_it_did_before(self) -> None:
        settings, ids = _two_connection_settings()
        scoped = {ids[0]: "sk-ONE", ids[1]: "sk-TWO"}

        payload = self._export(settings, scoped, set())

        translation = payload["model_profiles"]["translation"]
        self.assertEqual(translation["cloud"]["api_key"], "sk-ONE")
        self.assertEqual(
            [conn.get("api_key") for conn in translation["connections"]],
            ["sk-ONE", "sk-TWO"],
        )

    def test_the_receipt_tells_the_three_cases_apart(self) -> None:
        settings, ids = _two_connection_settings()
        report: list[dict[str, str]] = []

        self._export(settings, {ids[1]: "sk-THEIRS"}, {ids[1]}, report=report)

        translation_rows = [row for row in report if row["role"] == ROLE_TRANSLATION]
        self.assertEqual(
            [row["status"] for row in translation_rows],
            [API_KEY_EXPORT_MISSING, API_KEY_EXPORT_WITHHELD_IMPORTED],
        )
        # 回执必须能定位到具体连接：角色名 + 连接名，连接没名字时用 Base URL 兜底。
        self.assertEqual(translation_rows[1]["connection"], "同事给的账号")
        self.assertEqual(translation_rows[0]["connection"], "https://vendor.example/v1")
        self.assertTrue(all(row["role_label"] for row in translation_rows))
        # 其余三个角色默认没配密钥，一律算「本来就没有」。
        self.assertTrue(
            all(
                row["status"] == API_KEY_EXPORT_MISSING
                for row in report
                if row["role"] != ROLE_TRANSLATION
            )
        )

    def test_the_receipt_marks_an_exported_key(self) -> None:
        settings, ids = _two_connection_settings()
        report: list[dict[str, str]] = []

        self._export(settings, {ids[0]: "sk-MINE"}, set(), report=report)

        self.assertEqual(report[0]["status"], API_KEY_EXPORT_INCLUDED)

    def test_a_withheld_provider_memory_key_still_shows_up_in_the_receipt(self) -> None:
        """「换服务商时记住的配置」被扣下时也要有回执行——不许有悄悄扣下的密钥。"""
        settings, _ids = _two_connection_settings()
        report: list[dict[str, str]] = []

        build_model_config_export_payload(
            settings,
            get_api_key=lambda *_: "sk-THEIRS",
            get_scoped_api_key=lambda _cid: "",
            is_imported_api_key=lambda *_: True,
            is_imported_scoped_api_key=lambda _cid: False,
            include_api_key=True,
            api_key_report=report,
        )

        memories = [
            row for row in report if row["kind"] == API_KEY_EXPORT_KIND_PROVIDER_MEMORY
        ]
        self.assertTrue(memories)
        self.assertTrue(
            all(row["status"] == API_KEY_EXPORT_WITHHELD_IMPORTED for row in memories)
        )
        # 它不是界面上的连接，所以得靠服务商 + 地址来指认。
        self.assertTrue(all(row["provider"] for row in memories))
        translation_memory = next(
            row for row in memories if row["role"] == ROLE_TRANSLATION
        )
        self.assertEqual(translation_memory["connection"], "https://vendor.example/v1")

    def test_an_exportable_provider_memory_key_adds_no_receipt_noise(self) -> None:
        settings, ids = _two_connection_settings()
        report: list[dict[str, str]] = []

        build_model_config_export_payload(
            settings,
            get_api_key=lambda *_: "sk-MINE",
            get_scoped_api_key=lambda cid: "sk-MINE" if cid in ids else "",
            is_imported_api_key=lambda *_: False,
            is_imported_scoped_api_key=lambda _cid: False,
            include_api_key=True,
            api_key_report=report,
        )

        # 没被扣下就不进回执：逐条列出「记住的配置」只是噪音，用户对照不到界面。
        self.assertTrue(
            all(row["kind"] == API_KEY_EXPORT_KIND_CONNECTION for row in report)
        )

    def test_an_export_without_keys_produces_no_receipt_rows(self) -> None:
        settings, ids = _two_connection_settings()
        report: list[dict[str, str]] = []

        build_model_config_export_payload(
            settings,
            get_api_key=lambda *_: "sk-MINE",
            get_scoped_api_key=lambda _cid: "sk-MINE",
            include_api_key=False,
            api_key_report=report,
        )

        self.assertEqual(report, [])


class ExportEndpointReceiptTests(unittest.TestCase):
    """端到端：导入别人的配置 → 再导出含 Key → 那把密钥不会跟着走。"""

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

    def _import_donor_config(self) -> None:
        response = self.client.post(
            "/api/model-config/import",
            json={
                "type": "translator_model_config",
                "version": 3,
                "model_profiles": {
                    "translation": {
                        "cloud": {
                            "provider": "custom_openai",
                            "model": "donor-model",
                            "base_url": "https://donor.example/v1",
                            "api_key": "sk-DONOR",
                        },
                    },
                },
            },
        )
        self.assertEqual(response.status_code, 200, response.text)

    def test_a_donors_key_is_not_forwarded_and_the_receipt_says_so(self) -> None:
        self._import_donor_config()

        exported = self.client.get(
            "/api/model-config/export?include_api_key=true&confirm_sensitive=true"
        )

        self.assertEqual(exported.status_code, 200)
        self.assertNotIn("sk-DONOR", exported.text)
        body = exported.json()
        # 导出文件里连接的其余配置照常在。
        translation = body["document"]["model_profiles"]["translation"]
        self.assertEqual(translation["cloud"]["base_url"], "https://donor.example/v1")
        self.assertEqual(translation["cloud"]["model"], "donor-model")
        report = body["api_key_report"]
        self.assertEqual(report["exported_count"], 0)
        withheld = [
            row
            for row in report["connections"]
            if row["status"] == API_KEY_EXPORT_WITHHELD_IMPORTED
        ]
        self.assertEqual(report["withheld_count"], len(withheld))
        # 别的角色默认也指着同一个端点，于是解析到同一把导入来的 Key，一起被扣下——
        # 这正是回执要逐条列出来的原因：数字对不上界面，名字才对得上。
        self.assertIn(ROLE_TRANSLATION, {row["role"] for row in withheld})

    def test_refilling_the_key_locally_makes_it_exportable_again(self) -> None:
        self._import_donor_config()

        refilled = self.client.put(
            "/api/keys/custom_openai",
            json={"api_key": "sk-MINE", "base_url": "https://donor.example/v1"},
        )
        self.assertEqual(refilled.status_code, 200)

        exported = self.client.get(
            "/api/model-config/export?include_api_key=true&confirm_sensitive=true"
        )
        self.assertIn("sk-MINE", exported.text)
        self.assertEqual(exported.json()["api_key_report"]["withheld_count"], 0)

    def test_the_receipt_never_rides_along_in_the_exported_document(self) -> None:
        self._import_donor_config()

        exported = self.client.get(
            "/api/model-config/export?include_api_key=true&confirm_sensitive=true"
        ).json()

        # 界面只把 document 写盘；回执要是混进文件，对方还会额外收到一份本机连接清单。
        self.assertNotIn("api_key_report", exported["document"])
        self.assertEqual(exported["document"]["type"], "translator_model_config")


if __name__ == "__main__":
    unittest.main()
