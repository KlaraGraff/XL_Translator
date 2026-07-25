"""Connection pool schema, key scoping and downgrade safety."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import settings as settings_module
from core.model_roles import ROLE_TRANSLATION, resolve_effective_model_config
from settings import (
    AppSettings,
    EngineSettings,
    ModelConnection,
    ModelRoleSettings,
    connection_id_from_key_scope,
    connection_key_scope,
    is_connection_key_scope,
    parse_api_key_scope,
)


class _IsolatedKeyStore(unittest.TestCase):
    """Point the key store at a throwaway directory for the whole test."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        patches = [
            patch.object(settings_module, "APP_DATA_DIR", root),
            patch.object(settings_module, "KEYS_PATH", root / "keys.json"),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.addCleanup(self._tmp.cleanup)


class ConnectionPoolSchemaTests(unittest.TestCase):
    def test_pool_is_seeded_from_the_legacy_single_connection(self) -> None:
        engine = EngineSettings(
            cloud_provider="deepseek",
            cloud_model="deepseek-chat",
            cloud_base_url="https://api.deepseek.com/v1",
        )
        self.assertEqual(len(engine.connections), 1)
        primary = engine.connections[0]
        self.assertEqual(primary.provider, "deepseek")
        self.assertEqual(primary.model, "deepseek-chat")
        self.assertEqual(primary.base_url, "https://api.deepseek.com/v1")
        self.assertTrue(primary.id)

    def test_primary_mirrors_legacy_fields_for_downgrade_safety(self) -> None:
        engine = EngineSettings(
            cloud_provider="deepseek",
            cloud_model="deepseek-chat",
            cloud_base_url="https://api.deepseek.com/v1",
            connections=[
                ModelConnection(provider="stale", base_url="https://stale/v1"),
                ModelConnection(
                    provider="deepseek", base_url="https://api.deepseek.com/v1"
                ),
            ],
        )
        # Entry 0 is the legacy connection: it must agree with the flat fields.
        self.assertEqual(engine.connections[0].provider, "deepseek")
        dumped = engine.model_dump()
        self.assertEqual(dumped["cloud_provider"], "deepseek")
        self.assertEqual(dumped["cloud_base_url"], "https://api.deepseek.com/v1")

    def test_seeded_connection_ids_are_stable_across_loads(self) -> None:
        """A fresh install is not persisted, so the seeded id must not be random."""
        first = AppSettings()
        second = AppSettings()
        self.assertEqual(
            first.engine.connections[0].id, second.engine.connections[0].id
        )
        self.assertEqual(
            first.cleaner_model_role.connections[0].id,
            second.cleaner_model_role.connections[0].id,
        )

    def test_stored_connection_ids_survive_a_reload(self) -> None:
        app = AppSettings()
        app.engine.connections = [
            *app.engine.connections,
            ModelConnection(provider="custom_openai", base_url="https://b.example/v1"),
        ]
        reloaded = AppSettings(**app.model_dump())
        self.assertEqual(
            [c.id for c in reloaded.engine.connections],
            [c.id for c in app.engine.connections],
        )

    def test_role_pools_are_independent_between_roles(self) -> None:
        app = AppSettings()
        ids = [
            app.engine.connections[0].id,
            app.cleaner_model_role.connections[0].id,
            app.image_model_role.connections[0].id,
            app.pdf_review_model_role.connections[0].id,
        ]
        self.assertEqual(len(set(ids)), len(ids))

    def test_duplicate_connection_ids_are_reassigned(self) -> None:
        role = ModelRoleSettings(
            connections=[
                ModelConnection(id="fixed-id", provider="a", base_url="https://a/v1"),
                ModelConnection(id="fixed-id", provider="b", base_url="https://b/v1"),
            ]
        )
        self.assertNotEqual(role.connections[0].id, role.connections[1].id)

    def test_connection_scope_never_looks_like_a_provider(self) -> None:
        scope = connection_key_scope("abc123")
        self.assertTrue(is_connection_key_scope(scope))
        self.assertEqual(connection_id_from_key_scope(scope), "abc123")
        # A connection scope must not be mistaken for a provider named "conn".
        self.assertEqual(parse_api_key_scope(scope), ("", ""))


class ConnectionKeyStorageTests(_IsolatedKeyStore):
    def test_two_connections_on_one_endpoint_hold_distinct_keys(self) -> None:
        first = ModelConnection(
            provider="deepseek", base_url="https://api.deepseek.com/v1"
        )
        second = ModelConnection(
            provider="deepseek", base_url="https://api.deepseek.com/v1"
        )
        settings_module.save_connection_key(first.id, "key-account-one")
        settings_module.save_connection_key(second.id, "key-account-two")

        self.assertEqual(
            settings_module.get_connection_key(
                first.id, first.provider, first.base_url
            ),
            "key-account-one",
        )
        self.assertEqual(
            settings_module.get_connection_key(
                second.id, second.provider, second.base_url
            ),
            "key-account-two",
        )

    def test_a_connection_without_its_own_key_uses_the_provider_scope(self) -> None:
        settings_module.save_key(
            "deepseek", "legacy-key", "https://api.deepseek.com/v1"
        )
        conn = ModelConnection(
            provider="deepseek", base_url="https://api.deepseek.com/v1"
        )
        self.assertEqual(
            settings_module.get_connection_key(conn.id, conn.provider, conn.base_url),
            "legacy-key",
        )

    def test_deleting_a_connection_key_leaves_the_legacy_key_intact(self) -> None:
        settings_module.save_key(
            "deepseek", "legacy-key", "https://api.deepseek.com/v1"
        )
        conn = ModelConnection(
            provider="deepseek", base_url="https://api.deepseek.com/v1"
        )
        settings_module.save_connection_key(conn.id, "pool-key")
        settings_module.delete_connection_key(conn.id)
        # Rolling back to the pre-pool build must still find a usable key.
        self.assertEqual(
            settings_module.get_key("deepseek", "https://api.deepseek.com/v1"),
            "legacy-key",
        )

    def test_pool_entries_are_fully_independent_endpoint_sets(self) -> None:
        """A pool is a list of whole endpoints, not keys for one endpoint."""
        app = AppSettings()
        app.engine.mode = "cloud"
        app.engine.cloud_provider = "custom_openai"
        app.engine.cloud_base_url = "https://vendor-a.example/v1"
        app.engine.cloud_model = "vendor-a-cheap"
        app.engine.connections = [
            *app.engine.connections,
            ModelConnection(
                label="厂商 B",
                provider="siliconflow",
                model="vendor-b-model",
                base_url="https://vendor-b.example/v1",
            ),
            ModelConnection(
                label="厂商 C",
                provider="zhipu",
                model="vendor-c-model",
                base_url="https://vendor-c.example/v1",
            ),
        ]
        app = AppSettings(**app.model_dump())
        ids = [c.id for c in app.engine.connections]
        for connection_id, key in zip(ids, ["key-a", "key-b", "key-c"]):
            settings_module.save_connection_key(connection_id, key)

        resolved = [
            resolve_effective_model_config(app, ROLE_TRANSLATION, connection_id=cid)
            for cid in ids
        ]
        self.assertEqual(
            [c.provider for c in resolved],
            ["custom_openai", "siliconflow", "zhipu"],
        )
        self.assertEqual(
            [c.base_url for c in resolved],
            [
                "https://vendor-a.example/v1",
                "https://vendor-b.example/v1",
                "https://vendor-c.example/v1",
            ],
        )
        self.assertEqual(
            [c.model for c in resolved],
            ["vendor-a-cheap", "vendor-b-model", "vendor-c-model"],
        )
        self.assertEqual([c.api_key for c in resolved], ["key-a", "key-b", "key-c"])

    def test_resolving_a_secondary_uses_its_own_endpoint_and_key(self) -> None:
        app = AppSettings()
        app.engine.mode = "cloud"
        app.engine.cloud_provider = "custom_openai"
        app.engine.cloud_base_url = "https://primary.example/v1"
        app.engine.cloud_model = "cheap-model"
        app.engine.connections = [
            *app.engine.connections,
            ModelConnection(
                label="备用账号",
                provider="custom_openai",
                model="cheap-model",
                base_url="https://secondary.example/v1",
            ),
        ]
        app = AppSettings(**app.model_dump())
        secondary_id = app.engine.connections[1].id
        settings_module.save_connection_key(secondary_id, "secondary-key")

        primary = resolve_effective_model_config(app, ROLE_TRANSLATION)
        secondary = resolve_effective_model_config(
            app, ROLE_TRANSLATION, connection_id=secondary_id
        )
        self.assertEqual(primary.base_url, "https://primary.example/v1")
        self.assertEqual(secondary.base_url, "https://secondary.example/v1")
        self.assertEqual(secondary.api_key, "secondary-key")
        self.assertEqual(secondary.connection_id, secondary_id)
        self.assertEqual(secondary.connection_label, "备用账号")

    def test_unknown_connection_id_degrades_to_the_primary(self) -> None:
        app = AppSettings()
        resolved = resolve_effective_model_config(
            app, ROLE_TRANSLATION, connection_id="does-not-exist"
        )
        self.assertEqual(resolved.connection_id, app.engine.connections[0].id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
