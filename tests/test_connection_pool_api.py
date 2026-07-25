"""HTTP surface for editing a role's connection pool."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import settings as settings_module
from api.app import create_app

TOKEN = "test-token"


class ConnectionPoolApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        for item in (
            patch.object(settings_module, "APP_DATA_DIR", root),
            patch.object(settings_module, "KEYS_PATH", root / "keys.json"),
            patch.object(settings_module, "SETTINGS_PATH", root / "settings.json"),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.client = TestClient(create_app(auth_token=TOKEN))
        self.client.headers.update({"X-Translator-Token": TOKEN})
        self.addCleanup(self.client.close)

    def _pool(self) -> list[dict]:
        response = self.client.get("/api/models/roles")
        self.assertEqual(response.status_code, 200)
        return response.json()["roles"]["translation"]["connections"]

    def test_a_fresh_install_reports_one_primary_connection(self) -> None:
        connections = self._pool()
        assert len(connections) == 1
        assert connections[0]["primary"] is True


    def test_adding_a_connection_appends_it_without_touching_the_primary(self) -> None:
        before = self._pool()
        response = self.client.post(
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


    def test_reordering_promotes_a_different_primary(self) -> None:
        self.client.post(
            "/api/models/roles/translation/connections",
            json={"label": "B", "base_url": "https://vendor-b.example/v1", "model": "model-b"},
        )
        connections = self._pool()
        ids = [conn["id"] for conn in connections]

        response = self.client.post(
            "/api/models/roles/translation/connections/reorder",
            json={"ordered_ids": [ids[1], ids[0]]},
        )
        assert response.status_code == 200
        reordered = response.json()["connections"]
        assert reordered[0]["id"] == ids[1]
        assert reordered[0]["primary"] is True
        # The promoted entry must also become the role's effective endpoint.
        assert response.json()["base_url"] == "https://vendor-b.example/v1"


    def test_deleting_a_connection_removes_it_and_its_key(self) -> None:
        added = self.client.post(
            "/api/models/roles/translation/connections",
            json={"label": "B", "base_url": "https://vendor-b.example/v1", "api_key": "key-b"},
        ).json()["connections"]
        victim = added[1]["id"]

        response = self.client.delete(f"/api/models/roles/translation/connections/{victim}")
        assert response.status_code == 200
        assert [conn["id"] for conn in response.json()["connections"]] == [added[0]["id"]]
        assert settings_module.get_connection_scoped_key(victim) == ""


    def test_the_last_connection_cannot_be_deleted(self) -> None:
        only = self._pool()[0]["id"]
        response = self.client.delete(f"/api/models/roles/translation/connections/{only}")
        assert response.status_code == 422


    def test_an_empty_api_key_keeps_the_stored_one(self) -> None:
        added = self.client.post(
            "/api/models/roles/translation/connections",
            json={"label": "B", "base_url": "https://vendor-b.example/v1", "api_key": "key-b"},
        ).json()["connections"]
        target = added[1]["id"]

        self.client.put(
            f"/api/models/roles/translation/connections/{target}",
            json={"label": "改名了", "api_key": ""},
        )
        assert settings_module.get_connection_scoped_key(target) == "key-b"


    def test_moving_an_endpoint_clears_its_test_state(self) -> None:
        added = self.client.post(
            "/api/models/roles/translation/connections",
            json={"label": "B", "base_url": "https://vendor-b.example/v1"},
        ).json()["connections"]
        target = added[1]["id"]

        response = self.client.put(
            f"/api/models/roles/translation/connections/{target}",
            json={"base_url": "https://moved.example/v1"},
        )
        moved = next(
            conn for conn in response.json()["connections"] if conn["id"] == target
        )
        assert moved["base_url"] == "https://moved.example/v1"
        assert moved["availability_status"] == "unknown"


    def test_pools_are_per_role(self) -> None:
        self.client.post(
            "/api/models/roles/cleaner/connections",
            json={"label": "清洗备用", "base_url": "https://cleaner-b.example/v1"},
        )
        roles = self.client.get("/api/models/roles").json()["roles"]
        assert len(roles["cleaner"]["connections"]) == 2
        assert len(roles["translation"]["connections"]) == 1


    def test_saving_the_role_returns_a_fresh_pool_not_a_stale_one(self) -> None:
        """The pool is only re-synced on construction, so the response must re-read."""
        response = self.client.put(
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


    def test_unknown_role_is_rejected(self) -> None:
        response = self.client.post(
            "/api/models/roles/nope/connections", json={"label": "x"}
        )
        assert response.status_code == 404


if __name__ == "__main__":
    unittest.main(verbosity=2)
