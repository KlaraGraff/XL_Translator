"""HTTP surface for editing a role's connection pool."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import settings as settings_module
from api.app import create_app

TOKEN = "test-token"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_module, "APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(settings_module, "KEYS_PATH", tmp_path / "keys.json")
    monkeypatch.setattr(settings_module, "SETTINGS_PATH", tmp_path / "settings.json")
    with TestClient(create_app(auth_token=TOKEN)) as test_client:
        test_client.headers.update({"X-Translator-Token": TOKEN})
        yield test_client


def _pool(client) -> list[dict]:
    response = client.get("/api/models/roles")
    assert response.status_code == 200
    return response.json()["roles"]["translation"]["connections"]


def test_a_fresh_install_reports_one_primary_connection(client):
    connections = _pool(client)
    assert len(connections) == 1
    assert connections[0]["primary"] is True


def test_adding_a_connection_appends_it_without_touching_the_primary(client):
    before = _pool(client)
    response = client.post(
        "/api/models/roles/translation/connections",
        json={
            "label": "厂商 B",
            "provider": "custom_openai",
            "model": "model-b",
            "base_url": "https://vendor-b.example/v1",
            "api_key": "key-b",
        },
    )
    assert response.status_code == 200
    connections = response.json()["connections"]
    assert len(connections) == 2
    assert connections[0]["id"] == before[0]["id"]
    assert connections[1]["label"] == "厂商 B"
    assert connections[1]["has_api_key"] is True
    assert connections[1]["primary"] is False


def test_reordering_promotes_a_different_primary(client):
    client.post(
        "/api/models/roles/translation/connections",
        json={"label": "B", "base_url": "https://vendor-b.example/v1", "model": "model-b"},
    )
    connections = _pool(client)
    ids = [conn["id"] for conn in connections]

    response = client.post(
        "/api/models/roles/translation/connections/reorder",
        json={"ordered_ids": [ids[1], ids[0]]},
    )
    assert response.status_code == 200
    reordered = response.json()["connections"]
    assert reordered[0]["id"] == ids[1]
    assert reordered[0]["primary"] is True
    # The promoted entry must also become the role's effective endpoint.
    assert response.json()["base_url"] == "https://vendor-b.example/v1"


def test_deleting_a_connection_removes_it_and_its_key(client):
    added = client.post(
        "/api/models/roles/translation/connections",
        json={"label": "B", "base_url": "https://vendor-b.example/v1", "api_key": "key-b"},
    ).json()["connections"]
    victim = added[1]["id"]

    response = client.delete(f"/api/models/roles/translation/connections/{victim}")
    assert response.status_code == 200
    assert [conn["id"] for conn in response.json()["connections"]] == [added[0]["id"]]
    assert settings_module.get_connection_scoped_key(victim) == ""


def test_the_last_connection_cannot_be_deleted(client):
    only = _pool(client)[0]["id"]
    response = client.delete(f"/api/models/roles/translation/connections/{only}")
    assert response.status_code == 422


def test_an_empty_api_key_keeps_the_stored_one(client):
    added = client.post(
        "/api/models/roles/translation/connections",
        json={"label": "B", "base_url": "https://vendor-b.example/v1", "api_key": "key-b"},
    ).json()["connections"]
    target = added[1]["id"]

    client.put(
        f"/api/models/roles/translation/connections/{target}",
        json={"label": "改名了", "api_key": ""},
    )
    assert settings_module.get_connection_scoped_key(target) == "key-b"


def test_moving_an_endpoint_clears_its_test_state(client):
    added = client.post(
        "/api/models/roles/translation/connections",
        json={"label": "B", "base_url": "https://vendor-b.example/v1"},
    ).json()["connections"]
    target = added[1]["id"]

    response = client.put(
        f"/api/models/roles/translation/connections/{target}",
        json={"base_url": "https://moved.example/v1"},
    )
    moved = next(
        conn for conn in response.json()["connections"] if conn["id"] == target
    )
    assert moved["base_url"] == "https://moved.example/v1"
    assert moved["availability_status"] == "unknown"


def test_pools_are_per_role(client):
    client.post(
        "/api/models/roles/cleaner/connections",
        json={"label": "清洗备用", "base_url": "https://cleaner-b.example/v1"},
    )
    roles = client.get("/api/models/roles").json()["roles"]
    assert len(roles["cleaner"]["connections"]) == 2
    assert len(roles["translation"]["connections"]) == 1


def test_saving_the_role_returns_a_fresh_pool_not_a_stale_one(client):
    """The pool is only re-synced on construction, so the response must re-read."""
    response = client.put(
        "/api/models/roles/translation",
        json={
            "mode": "cloud",
            "provider": "custom_openai",
            "base_url": "https://vendor-a.example/v1",
            "model": "model-a",
        },
    )
    assert response.status_code == 200
    primary = response.json()["connections"][0]
    assert primary["base_url"] == "https://vendor-a.example/v1"
    assert primary["model"] == "model-a"


def test_unknown_role_is_rejected(client):
    response = client.post(
        "/api/models/roles/nope/connections", json={"label": "x"}
    )
    assert response.status_code == 404
