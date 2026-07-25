"""Allocation seen through the real task context resolver."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import settings as settings_module
from core.model_api_identity import task_api_context_for_page
from core.model_roles import ROLE_TRANSLATION
from settings import AppSettings, ModelConnection


def _two_vendor_settings() -> AppSettings:
    app = AppSettings()
    app.engine.mode = "cloud"
    app.engine.cloud_provider = "custom_openai"
    app.engine.cloud_base_url = "https://vendor-a.example/v1"
    app.engine.cloud_model = "model-a"
    app.engine.connections = [
        *app.engine.connections,
        ModelConnection(
            label="厂商 B",
            provider="custom_openai",
            model="model-b",
            base_url="https://vendor-b.example/v1",
        ),
    ]
    return AppSettings(**app.model_dump())


def _fake_key(provider: str, base_url: str = "") -> str:
    return f"key::{provider}::{base_url}"


class ConnectionSpreadingTests(unittest.TestCase):
    def test_spreading_off_keeps_concurrent_tasks_on_the_primary(self):
        app = _two_vendor_settings()
        app.spread_tasks_across_connections = False
        primary_id = app.engine.connections[0].id

        with patch("core.model_roles.get_key", side_effect=_fake_key):
            first = task_api_context_for_page(app, "excel_translate")
            second = task_api_context_for_page(
                app,
                "word_translate",
                busy_connection_ids=frozenset({first.role_connection_ids[ROLE_TRANSLATION]}),
            )

        assert first.role_connection_ids[ROLE_TRANSLATION] == primary_id
        assert second.role_connection_ids[ROLE_TRANSLATION] == primary_id
        # Sharing must still be reported so the concurrency warning can fire.
        assert ROLE_TRANSLATION in second.shared_connection_roles


    def test_spreading_on_sends_word_and_excel_to_different_vendors(self):
        app = _two_vendor_settings()
        app.spread_tasks_across_connections = True

        with patch("core.model_roles.get_key", side_effect=_fake_key):
            excel = task_api_context_for_page(app, "excel_translate")
            word = task_api_context_for_page(
                app,
                "word_translate",
                busy_connection_ids=frozenset({excel.role_connection_ids[ROLE_TRANSLATION]}),
            )

        excel_snapshot = excel.model_snapshot[ROLE_TRANSLATION]
        word_snapshot = word.model_snapshot[ROLE_TRANSLATION]
        assert excel_snapshot["base_url"] == "https://vendor-a.example/v1"
        assert word_snapshot["base_url"] == "https://vendor-b.example/v1"
        assert excel_snapshot["model"] == "model-a"
        assert word_snapshot["model"] == "model-b"
        # Different endpoints means no shared-connection warning at all.
        assert not word.shared_connection_roles


    def test_a_solo_task_still_uses_the_primary_when_spreading_is_on(self):
        app = _two_vendor_settings()
        app.spread_tasks_across_connections = True

        with patch("core.model_roles.get_key", side_effect=_fake_key):
            context = task_api_context_for_page(app, "excel_translate")

        assert context.role_connection_ids[ROLE_TRANSLATION] == app.engine.connections[0].id


    def test_snapshot_records_the_fallback_chain_for_the_task(self):
        app = _two_vendor_settings()
        app.spread_tasks_across_connections = True

        with patch("core.model_roles.get_key", side_effect=_fake_key):
            context = task_api_context_for_page(app, "excel_translate")

        chain = context.model_snapshot[ROLE_TRANSLATION]["pool_connection_chain"]
        assert chain == [conn.id for conn in app.engine.connections]


    def test_each_pool_entry_freezes_its_own_key_override(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for item in (
            patch.object(settings_module, "APP_DATA_DIR", root),
            patch.object(settings_module, "KEYS_PATH", root / "keys.json"),
        ):
            item.start()
            self.addCleanup(item.stop)

        app = AppSettings()
        app.engine.mode = "cloud"
        app.engine.cloud_provider = "custom_openai"
        app.engine.cloud_base_url = "https://same.example/v1"
        app.engine.cloud_model = "model"
        app.engine.connections = [
            *app.engine.connections,
            ModelConnection(
                label="第二个账号",
                provider="custom_openai",
                model="model",
                base_url="https://same.example/v1",
            ),
        ]
        app = AppSettings(**app.model_dump())
        app.spread_tasks_across_connections = True
        ids = [conn.id for conn in app.engine.connections]
        settings_module.save_connection_key(ids[0], "account-one")
        settings_module.save_connection_key(ids[1], "account-two")

        first = task_api_context_for_page(app, "excel_translate")
        second = task_api_context_for_page(
            app,
            "word_translate",
            busy_connection_ids=frozenset({first.role_connection_ids[ROLE_TRANSLATION]}),
        )

        first_scope = settings_module.connection_key_scope(ids[0])
        second_scope = settings_module.connection_key_scope(ids[1])
        # Same endpoint, two accounts: each task must freeze its own credential.
        assert first.key_overrides[first_scope] == "account-one"
        assert second.key_overrides[second_scope] == "account-two"


if __name__ == "__main__":
    unittest.main(verbosity=2)
