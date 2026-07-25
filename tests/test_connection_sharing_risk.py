"""Spreading across the pool removes the shared-connection warning."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from core.model_api_identity import (
    api_group_signature_from_config,
    task_api_context_for_page,
)
from core.model_roles import ROLE_TRANSLATION, resolve_effective_model_config
from settings import AppSettings, ModelConnection


def _two_vendor_settings(spread: bool) -> AppSettings:
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
    app = AppSettings(**app.model_dump())
    app.spread_tasks_across_connections = spread
    return app


def _fake_key(provider: str, base_url: str = "") -> str:
    return f"key::{provider}::{base_url}"


class ConnectionSharingRiskTests(unittest.TestCase):
    def test_two_tasks_on_different_connections_do_not_form_a_shared_group(self):
        app = _two_vendor_settings(spread=True)
        with patch("core.model_roles.get_key", side_effect=_fake_key):
            excel = task_api_context_for_page(app, "excel_translate")
            word = task_api_context_for_page(
                app,
                "word_translate",
                busy_connection_ids=frozenset({excel.role_connection_ids[ROLE_TRANSLATION]}),
            )
        # Distinct upstream identities mean the scheduler sees no contention, so
        # the concurrency confirmation has nothing to warn about.
        assert excel.api_groups.isdisjoint(word.api_groups)
        assert not word.shared_connection_roles


    def test_two_tasks_without_spreading_share_one_group_and_are_flagged(self):
        app = _two_vendor_settings(spread=False)
        with patch("core.model_roles.get_key", side_effect=_fake_key):
            excel = task_api_context_for_page(app, "excel_translate")
            word = task_api_context_for_page(
                app,
                "word_translate",
                busy_connection_ids=frozenset({excel.role_connection_ids[ROLE_TRANSLATION]}),
            )
        assert excel.api_groups == word.api_groups
        assert ROLE_TRANSLATION in word.shared_connection_roles


    def test_each_pool_entry_has_its_own_upstream_identity(self):
        app = _two_vendor_settings(spread=True)
        with patch("core.model_roles.get_key", side_effect=_fake_key):
            signatures = {
                api_group_signature_from_config(
                    resolve_effective_model_config(
                        app, ROLE_TRANSLATION, connection_id=conn.id
                    )
                )
                for conn in app.engine.connections
            }
        assert len(signatures) == len(app.engine.connections)


if __name__ == "__main__":
    unittest.main(verbosity=2)
