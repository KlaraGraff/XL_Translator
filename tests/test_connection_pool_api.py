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


    def test_a_saved_key_is_reported_only_as_a_masked_preview(self) -> None:
        secret = "sk-vendor-b-secret-value-0987"
        response = self.client.post(
            "/api/models/roles/translation/connections",
            json={
                "label": "厂商 B",
                "provider": "custom_openai",
                "model": "model-b",
                "base_url": "https://vendor-b.example/v1",
                "api_key": secret,
            },
        )
        assert response.status_code == 200
        assert secret not in response.text
        added = response.json()["connections"][1]
        assert added["has_api_key"] is True
        assert added["api_key_preview"] == "sk-v••••••0987"

        listed = self._pool()[1]
        assert listed["api_key_preview"] == "sk-v••••••0987"


    def test_a_connection_without_a_key_reports_an_empty_preview(self) -> None:
        response = self.client.post(
            "/api/models/roles/translation/connections",
            json={"label": "无密钥", "base_url": "https://vendor-c.example/v1", "model": "model-c"},
        )
        assert response.status_code == 200
        added = response.json()["connections"][1]
        assert added["has_api_key"] is False
        assert added["api_key_preview"] == ""


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
        # Cleaner follows translation out of the box, and a following role
        # borrows the source's pool instead of owning one, so an independent
        # cleaner is what makes this about per-role separation.
        self.client.put(
            "/api/models/roles/cleaner", json={"source_role": "independent"}
        )
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

    def test_secondary_follow_is_owned_editable_and_deletable(self) -> None:
        original = self.client.put(
            "/api/models/roles/cleaner",
            json={
                "source_role": "independent", "mode": "cloud",
                "provider": "custom_openai", "model": "deepseek-flash",
                "base_url": "https://own.example/v1",
            },
        ).json()["connections"][0]
        response = self.client.post(
            "/api/models/roles/cleaner/connections",
            json={
                "label": "跟随文档翻译", "connection_mode": "follow",
                "source_role": "translation", "model": "another-cleaner-model",
                "provider": "custom_openai", "base_url": "https://own-backup.example/v1",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        rows = response.json()["connections"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["id"], original["id"])
        self.assertEqual(rows[0]["connection_mode"], "cloud")
        self.assertEqual(rows[0]["model"], "deepseek-flash")
        follower = rows[1]
        self.assertEqual(follower["connection_pool_role"], "cleaner")
        self.assertEqual(follower["model"], "another-cleaner-model")
        self.assertEqual(follower["own_base_url"], "https://own-backup.example/v1")

        response = self.client.put(
            f"/api/models/roles/cleaner/connections/{follower['id']}",
            json={"model": "third-cleaner-model"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        saved = response.json()["connections"][1]
        self.assertEqual(saved["model"], "third-cleaner-model")
        self.assertEqual(self.client.get("/api/models/roles").json()["roles"]["cleaner"]["connections"][1]["model"], "third-cleaner-model")

        restored = self.client.put(
            f"/api/models/roles/cleaner/connections/{follower['id']}",
            json={"connection_mode": "cloud", "source_role": "independent"},
        )
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(restored.json()["connections"][1]["base_url"], "https://own-backup.example/v1")

        response = self.client.delete(
            f"/api/models/roles/cleaner/connections/{follower['id']}"
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([row["id"] for row in response.json()["connections"]], [original["id"]])

    def test_primary_follow_model_name_persists_and_source_endpoint_tracks_primary(self) -> None:
        before = self.client.get("/api/models/roles").json()["roles"]["cleaner"]
        self.assertEqual(before["connections"][0]["connection_mode"], "follow")
        response = self.client.put(
            "/api/models/roles/cleaner",
            json={"source_role": "translation", "model": "another-cleaner-model"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["model"], "another-cleaner-model")
        self.assertEqual(response.json()["connections"][0]["model"], "another-cleaner-model")
        self.assertEqual(
            self.client.get("/api/models/roles").json()["roles"]["cleaner"]["model"],
            "another-cleaner-model",
        )

        response = self.client.put(
            "/api/models/roles/translation",
            json={"provider": "custom_openai", "base_url": "https://new-source.example/v1"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        follower = self.client.get("/api/models/roles").json()["roles"]["cleaner"]["connections"][0]
        self.assertEqual(follower["base_url"], "https://new-source.example/v1")
        self.assertEqual(follower["model"], "another-cleaner-model")

    def test_promoting_and_deleting_follow_row_updates_primary_and_source_options(self) -> None:
        first = self.client.put(
            "/api/models/roles/cleaner",
            json={"source_role": "independent", "mode": "cloud", "model": "original-model"},
        ).json()["connections"][0]
        second = self.client.post(
            "/api/models/roles/cleaner/connections",
            json={"connection_mode": "follow", "source_role": "translation", "model": "follow-model"},
        ).json()["connections"][1]
        roles = self.client.get("/api/models/roles").json()["roles"]
        self.assertIn("cleaner", roles["translation"]["source_role_options"])

        promoted = self.client.post(
            "/api/models/roles/cleaner/connections/reorder",
            json={"ordered_ids": [second["id"], first["id"]]},
        )
        self.assertEqual(promoted.status_code, 200, promoted.text)
        self.assertEqual(promoted.json()["source_role"], "translation")
        self.assertEqual(promoted.json()["model"], "follow-model")
        roles = self.client.get("/api/models/roles").json()["roles"]
        self.assertNotIn("cleaner", roles["translation"]["source_role_options"])

        deleted = self.client.delete(
            f"/api/models/roles/cleaner/connections/{second['id']}"
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(deleted.json()["source_role"], "independent")
        self.assertEqual(deleted.json()["model"], "original-model")
        self.assertEqual(deleted.json()["connections"][0]["id"], first["id"])

    def test_follow_row_key_preview_tracks_source_primary(self) -> None:
        first = self._pool()[0]
        saved = self.client.put(
            f"/api/models/roles/translation/connections/{first['id']}",
            json={"api_key": "sk-first-secret-123456"},
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        second = self.client.post(
            "/api/models/roles/translation/connections",
            json={
                "provider": "custom_openai", "model": "second-model",
                "base_url": "https://second.example/v1", "api_key": "sk-second-secret-654321",
            },
        ).json()["connections"][1]
        follower = self.client.post(
            "/api/models/roles/cleaner/connections",
            json={"connection_mode": "follow", "source_role": "translation", "model": "own-model"},
        ).json()["connections"][1]
        before = self.client.get("/api/models/roles").json()["roles"]["cleaner"]["connections"][1]
        self.assertEqual(before["id"], follower["id"])
        self.assertTrue(before["has_api_key"])
        self.assertNotIn("sk-first-secret-123456", str(before))

        switched = self.client.post(
            "/api/models/roles/translation/connections/reorder",
            json={"ordered_ids": [second["id"], first["id"]]},
        )
        self.assertEqual(switched.status_code, 200, switched.text)
        after = self.client.get("/api/models/roles").json()["roles"]["cleaner"]["connections"][1]
        self.assertEqual(after["id"], follower["id"])
        self.assertEqual(after["base_url"], "https://second.example/v1")
        self.assertNotEqual(before["api_key_preview"], after["api_key_preview"])
        self.assertEqual(after["model"], "own-model")

    def test_primary_cloud_local_cloud_restores_cloud_connection(self) -> None:
        cloud = self.client.put(
            "/api/models/roles/translation",
            json={"mode": "cloud", "provider": "custom_openai", "model": "cloud-model", "base_url": "https://cloud.example/v1"},
        )
        self.assertEqual(cloud.status_code, 200, cloud.text)
        self.client.put("/api/models/roles/image", json={"source_role": "independent"})
        self.client.put("/api/models/roles/pdf_review", json={"source_role": "independent"})
        local = self.client.put(
            "/api/models/roles/translation",
            json={"mode": "local", "provider": "ollama", "model": "qwen-local", "base_url": "http://127.0.0.1:11434"},
        )
        self.assertEqual(local.status_code, 200, local.text)
        restored = self.client.put(
            "/api/models/roles/translation", json={"mode": "cloud"}
        )
        self.assertEqual(restored.status_code, 200, restored.text)
        row = restored.json()["connections"][0]
        self.assertEqual((row["provider"], row["model"], row["base_url"]),
                         ("custom_openai", "cloud-model", "https://cloud.example/v1"))

    def test_secondary_cloud_local_cloud_restores_its_own_connection(self) -> None:
        self.client.put("/api/models/roles/cleaner", json={"source_role": "independent"})
        created = self.client.post(
            "/api/models/roles/cleaner/connections",
            json={"provider": "custom_openai", "model": "backup-cloud", "base_url": "https://backup.example/v1"},
        )
        self.assertEqual(created.status_code, 200, created.text)
        connection_id = created.json()["connections"][1]["id"]
        local = self.client.put(
            f"/api/models/roles/cleaner/connections/{connection_id}",
            json={"connection_mode": "local", "provider": "ollama", "model": "backup-local", "base_url": "http://127.0.0.1:11434"},
        )
        self.assertEqual(local.status_code, 200, local.text)
        restored = self.client.put(
            f"/api/models/roles/cleaner/connections/{connection_id}",
            json={"connection_mode": "cloud"},
        )
        self.assertEqual(restored.status_code, 200, restored.text)
        row = restored.json()["connections"][1]
        self.assertEqual((row["provider"], row["model"], row["base_url"]),
                         ("custom_openai", "backup-cloud", "https://backup.example/v1"))

    def test_follow_model_edit_preserves_previous_cloud_model(self) -> None:
        cloud = self.client.put(
            "/api/models/roles/cleaner",
            json={"source_role": "independent", "mode": "cloud", "model": "own-cloud-model",
                  "provider": "custom_openai", "base_url": "https://own.example/v1"},
        )
        self.assertEqual(cloud.status_code, 200, cloud.text)
        followed = self.client.put(
            "/api/models/roles/cleaner",
            json={"source_role": "translation", "model": "follow-only-model"},
        )
        self.assertEqual(followed.status_code, 200, followed.text)
        row = followed.json()["connections"][0]
        self.assertEqual(row["model"], "follow-only-model")
        self.assertEqual(row["own_cloud_model"], "own-cloud-model")
        self.assertEqual(row["own_cloud_base_url"], "https://own.example/v1")

        restored = self.client.put(
            "/api/models/roles/cleaner", json={"source_role": "independent", "mode": "cloud"}
        )
        self.assertEqual(restored.status_code, 200, restored.text)
        row = restored.json()["connections"][0]
        self.assertEqual(row["model"], "own-cloud-model")
        self.assertEqual(row["base_url"], "https://own.example/v1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
