"""Isolated API contracts for OA throughput unlock and text protocol fields."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import settings as settings_module
from api.app import create_app
from core import diagnostics, tm_manager


class OaProtocolApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        patches = [
            patch.multiple(
                settings_module,
                APP_DATA_DIR=root / "app-data",
                SETTINGS_PATH=root / "app-data" / "settings.json",
                KEYS_PATH=root / "app-data" / "keys.json",
            ),
            patch.object(tm_manager, "DB_PATH", root / "app-data" / "tm.db"),
            patch.object(diagnostics, "DIAGNOSTIC_RECORDS_DIR", root / "diagnostics"),
            patch.object(diagnostics, "LOG_PATH", root / "app-data" / "app.log"),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.client = TestClient(create_app())

    def test_oa_unlock_persists_large_throughput_and_rejects_invalid_values(self) -> None:
        before = self.client.get("/api/models/throughput/translation")
        self.assertEqual(before.status_code, 200)
        self.assertIsNotNone(before.json()["batch_size_bounds"])

        self.assertEqual(
            self.client.put(
                "/api/models/throughput/translation",
                json={"batch_size": 0, "concurrency": 0},
            ).status_code,
            422,
        )
        self.assertEqual(
            self.client.post("/api/models/throughput/unlock", json={"code": "wrong"}).status_code,
            422,
        )
        self.assertEqual(
            self.client.post("/api/models/throughput/unlock", json={"code": " OA "}).status_code,
            200,
        )
        saved = self.client.put(
            "/api/models/throughput/translation",
            json={"batch_size": 1000000, "concurrency": 1000000},
        )
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json()["batch_size"], 1000000)
        self.assertEqual(saved.json()["concurrency"], 1000000)

        reloaded = self.client.get("/api/models/throughput/translation")
        self.assertEqual(reloaded.status_code, 200)
        self.assertEqual(reloaded.json()["batch_size"], 1000000)
        self.assertEqual(reloaded.json()["concurrency"], 1000000)
        self.assertIsNone(reloaded.json()["batch_size_bounds"])
        self.assertIsNone(reloaded.json()["concurrency_bounds"])

    def test_protocol_round_trip_for_role_and_secondary_connection(self) -> None:
        role = self.client.put(
            "/api/models/roles/translation",
            json={
                "source_role": "independent",
                "mode": "cloud",
                "provider": "custom_openai",
                "model": "model-a",
                "base_url": "https://api.example.test/v1",
                "api_mode": "responses",
            },
        )
        self.assertEqual(role.status_code, 200)
        self.assertEqual(role.json()["api_mode"], "responses")

        created = self.client.post(
            "/api/models/roles/translation/connections",
            json={
                "label": "secondary",
                "provider": "custom_openai",
                "model": "model-b",
                "base_url": "https://api.example.test/v1",
                "api_mode": "chat",
            },
        )
        self.assertEqual(created.status_code, 200)
        secondary = next(item for item in created.json()["connections"] if item["label"] == "secondary")
        self.assertEqual(secondary["api_mode"], "chat")

        updated = self.client.put(
            f"/api/models/roles/translation/connections/{secondary['id']}",
            json={"api_mode": "responses"},
        )
        self.assertEqual(updated.status_code, 200)
        secondary_after = next(item for item in updated.json()["connections"] if item["id"] == secondary["id"])
        self.assertEqual(secondary_after["api_mode"], "responses")


if __name__ == "__main__":
    unittest.main()
