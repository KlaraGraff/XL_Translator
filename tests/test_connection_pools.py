"""Connection pool schema, key scoping and downgrade safety."""

from __future__ import annotations

import settings as settings_module
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


def test_pool_is_seeded_from_the_legacy_single_connection():
    engine = EngineSettings(
        cloud_provider="deepseek",
        cloud_model="deepseek-chat",
        cloud_base_url="https://api.deepseek.com/v1",
    )
    assert len(engine.connections) == 1
    primary = engine.connections[0]
    assert primary.provider == "deepseek"
    assert primary.model == "deepseek-chat"
    assert primary.base_url == "https://api.deepseek.com/v1"
    assert primary.id


def test_primary_mirrors_legacy_fields_so_a_downgrade_still_reads_the_connection():
    engine = EngineSettings(
        cloud_provider="deepseek",
        cloud_model="deepseek-chat",
        cloud_base_url="https://api.deepseek.com/v1",
        connections=[
            ModelConnection(provider="stale", model="stale", base_url="https://stale/v1"),
            ModelConnection(
                provider="deepseek",
                model="deepseek-chat",
                base_url="https://api.deepseek.com/v1",
            ),
        ],
    )
    # Entry 0 is the legacy connection: it must always agree with the flat fields.
    assert engine.connections[0].provider == "deepseek"
    assert engine.connections[0].base_url == "https://api.deepseek.com/v1"
    dumped = engine.model_dump()
    assert dumped["cloud_provider"] == "deepseek"
    assert dumped["cloud_base_url"] == "https://api.deepseek.com/v1"


def test_role_pools_are_independent_between_roles():
    app = AppSettings()
    pools = {
        "translation": app.engine.connections,
        "cleaner": app.cleaner_model_role.connections,
        "image": app.image_model_role.connections,
        "pdf_review": app.pdf_review_model_role.connections,
    }
    ids = [pool[0].id for pool in pools.values()]
    assert len(set(ids)) == len(ids), "each role must own a distinct connection"


def test_duplicate_connection_ids_are_reassigned():
    shared = "fixed-id"
    role = ModelRoleSettings(
        connections=[
            ModelConnection(id=shared, provider="a", base_url="https://a/v1"),
            ModelConnection(id=shared, provider="b", base_url="https://b/v1"),
        ]
    )
    assert role.connections[0].id != role.connections[1].id


def test_connection_scope_round_trips_and_never_looks_like_a_provider():
    scope = connection_key_scope("abc123")
    assert is_connection_key_scope(scope)
    assert connection_id_from_key_scope(scope) == "abc123"
    # A connection scope must not be mistaken for a provider named "conn".
    assert parse_api_key_scope(scope) == ("", "")


def test_two_connections_on_one_endpoint_hold_distinct_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_module, "APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(settings_module, "KEYS_PATH", tmp_path / "keys.json")

    first = ModelConnection(provider="deepseek", base_url="https://api.deepseek.com/v1")
    second = ModelConnection(provider="deepseek", base_url="https://api.deepseek.com/v1")
    settings_module.save_connection_key(first.id, "key-account-one")
    settings_module.save_connection_key(second.id, "key-account-two")

    assert settings_module.get_connection_key(
        first.id, first.provider, first.base_url
    ) == "key-account-one"
    assert settings_module.get_connection_key(
        second.id, second.provider, second.base_url
    ) == "key-account-two"


def test_connection_without_its_own_key_falls_back_to_the_provider_scope(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(settings_module, "APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(settings_module, "KEYS_PATH", tmp_path / "keys.json")

    settings_module.save_key("deepseek", "legacy-key", "https://api.deepseek.com/v1")
    conn = ModelConnection(provider="deepseek", base_url="https://api.deepseek.com/v1")

    assert settings_module.get_connection_key(
        conn.id, conn.provider, conn.base_url
    ) == "legacy-key"


def test_resolving_a_secondary_connection_uses_its_own_endpoint_and_key(
    tmp_path, monkeypatch
):
    from core.model_roles import ROLE_TRANSLATION, resolve_effective_model_config

    monkeypatch.setattr(settings_module, "APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(settings_module, "KEYS_PATH", tmp_path / "keys.json")

    app = AppSettings()
    app.engine.mode = "cloud"
    app.engine.cloud_provider = "custom_openai"
    app.engine.cloud_base_url = "https://primary.example/v1"
    app.engine.cloud_model = "cheap-model"
    secondary = ModelConnection(
        label="备用账号",
        provider="custom_openai",
        model="cheap-model",
        base_url="https://secondary.example/v1",
    )
    app.engine.connections = [*app.engine.connections, secondary]
    app = AppSettings(**app.model_dump())
    secondary_id = app.engine.connections[1].id

    settings_module.save_connection_key(secondary_id, "secondary-key")

    primary_config = resolve_effective_model_config(app, ROLE_TRANSLATION)
    secondary_config = resolve_effective_model_config(
        app, ROLE_TRANSLATION, connection_id=secondary_id
    )

    assert primary_config.base_url == "https://primary.example/v1"
    assert secondary_config.base_url == "https://secondary.example/v1"
    assert secondary_config.api_key == "secondary-key"
    assert secondary_config.connection_id == secondary_id
    assert secondary_config.connection_label == "备用账号"


def test_pool_entries_are_fully_independent_provider_url_model_key_sets(
    tmp_path, monkeypatch
):
    """A pool is a list of whole endpoints, not a list of keys for one endpoint."""
    from core.model_roles import ROLE_TRANSLATION, resolve_effective_model_config

    monkeypatch.setattr(settings_module, "APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(settings_module, "KEYS_PATH", tmp_path / "keys.json")

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
    ids = [conn.id for conn in app.engine.connections]
    settings_module.save_connection_key(ids[0], "key-a")
    settings_module.save_connection_key(ids[1], "key-b")
    settings_module.save_connection_key(ids[2], "key-c")

    resolved = [
        resolve_effective_model_config(app, ROLE_TRANSLATION, connection_id=cid)
        for cid in ids
    ]

    assert [c.provider for c in resolved] == ["custom_openai", "siliconflow", "zhipu"]
    assert [c.base_url for c in resolved] == [
        "https://vendor-a.example/v1",
        "https://vendor-b.example/v1",
        "https://vendor-c.example/v1",
    ]
    assert [c.model for c in resolved] == [
        "vendor-a-cheap",
        "vendor-b-model",
        "vendor-c-model",
    ]
    assert [c.api_key for c in resolved] == ["key-a", "key-b", "key-c"]


def test_unknown_connection_id_degrades_to_the_primary():
    from core.model_roles import ROLE_TRANSLATION, resolve_effective_model_config

    app = AppSettings()
    resolved = resolve_effective_model_config(
        app, ROLE_TRANSLATION, connection_id="does-not-exist"
    )
    assert resolved.connection_id == app.engine.connections[0].id


def test_deleting_a_connection_key_leaves_the_legacy_provider_key_intact(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(settings_module, "APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(settings_module, "KEYS_PATH", tmp_path / "keys.json")

    settings_module.save_key("deepseek", "legacy-key", "https://api.deepseek.com/v1")
    conn = ModelConnection(provider="deepseek", base_url="https://api.deepseek.com/v1")
    settings_module.save_connection_key(conn.id, "pool-key")
    settings_module.delete_connection_key(conn.id)

    # Rolling back to the pre-pool build must still find a usable key.
    assert settings_module.get_key("deepseek", "https://api.deepseek.com/v1") == "legacy-key"
