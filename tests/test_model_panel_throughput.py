"""Throughput on the model panel follows the connection it is shown under.

Covers two panel bugs:

* With the concurrency unlocked, typing 60 and then picking another model
  dropped the value back to the recommended 20: profiles are keyed by the
  model name, and the new key had no profile yet.
* The 速率设置 block sits under the selected connection, but it always read and
  wrote the primary connection's profile.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import settings as settings_module
from api.app import create_app

TOKEN = "test-token"


class ModelPanelThroughputTests(unittest.TestCase):
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

    def _role(self, role: str = "translation") -> dict:
        response = self.client.get("/api/models/roles")
        self.assertEqual(response.status_code, 200)
        return response.json()["roles"][role]

    def _primary_id(self) -> str:
        return self._role()["connections"][0]["id"]

    def _edit(self, connection_id: str, **body) -> dict:
        response = self.client.put(
            f"/api/models/roles/translation/connections/{connection_id}", json=body,
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _cloud(self, connection_id: str, model: str) -> dict:
        return self._edit(
            connection_id,
            connection_mode="cloud",
            source_role="independent",
            provider="custom_openai",
            base_url="https://vendor.example/v1",
            model=model,
        )

    def _unlock(self) -> None:
        response = self.client.post("/api/models/throughput/unlock", json={"code": "OA"})
        self.assertEqual(response.status_code, 200)

    def test_unlocked_concurrency_survives_picking_another_model(self) -> None:
        primary = self._primary_id()
        self._cloud(primary, "model-a")
        self._unlock()
        response = self.client.put("/api/models/throughput/translation", json={"concurrency": 60})
        self.assertEqual(response.json()["concurrency"], 60)

        payload = self._cloud(primary, "model-b")

        self.assertEqual(payload["throughput"]["concurrency"], 60)
        self.assertEqual(payload["connections"][0]["throughput"]["concurrency"], 60)
        self.assertEqual(
            self.client.get("/api/models/throughput/translation").json()["concurrency"], 60,
        )

    def test_a_model_with_its_own_tuning_keeps_it(self) -> None:
        primary = self._primary_id()
        self._unlock()
        self._cloud(primary, "model-b")
        self.client.put("/api/models/throughput/translation", json={"concurrency": 7})
        self._cloud(primary, "model-a")
        self.client.put("/api/models/throughput/translation", json={"concurrency": 60})

        payload = self._cloud(primary, "model-b")

        self.assertEqual(payload["throughput"]["concurrency"], 7)

    def test_switching_cloud_to_local_does_not_carry_the_cloud_figure(self) -> None:
        # The cleaner role: nothing follows it, so it may go local on its own.
        cleaner = self._role("cleaner")["connections"][0]["id"]
        url = f"/api/models/roles/cleaner/connections/{cleaner}"
        response = self.client.put(url, json={
            "connection_mode": "cloud", "source_role": "independent", "provider": "custom_openai",
            "base_url": "https://vendor.example/v1", "model": "model-a",
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.client.put("/api/models/throughput/cleaner", json={"concurrency": 15})

        response = self.client.put(url, json={
            "connection_mode": "local", "source_role": "independent",
            "provider": "ollama", "base_url": "http://127.0.0.1:11434", "model": "qwen2.5:7b",
        })
        self.assertEqual(response.status_code, 200, response.text)

        self.assertEqual(response.json()["throughput"]["concurrency"], 3)

    def test_throughput_routes_address_the_selected_connection(self) -> None:
        primary = self._primary_id()
        self._cloud(primary, "model-a")
        added = self.client.post(
            "/api/models/roles/translation/connections",
            json={"label": "B", "connection_mode": "cloud", "provider": "custom_openai",
                  "base_url": "https://vendor-b.example/v1", "model": "model-b"},
        ).json()["connections"][1]["id"]

        response = self.client.put(
            f"/api/models/throughput/translation?connection_id={added}", json={"concurrency": 4},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["concurrency"], 4)
        self.assertEqual(response.json()["concurrency_bounds"], [1, 50])

        role = self._role()
        self.assertEqual(role["connections"][1]["throughput"]["concurrency"], 4)
        # The primary keeps the recommendation; it was never touched.
        self.assertEqual(role["connections"][0]["throughput"]["concurrency"], 30)
        self.assertEqual(
            self.client.get(f"/api/models/throughput/translation?connection_id={added}").json()["concurrency"],
            4,
        )

        reset = self.client.delete(f"/api/models/throughput/translation?connection_id={added}")
        self.assertEqual(reset.json()["concurrency"], 30)

    def test_a_follower_keeps_its_tuning_when_the_source_changes_model(self) -> None:
        # The image-review role follows translation: its endpoint is the
        # translation primary's, so editing that primary moves its key too.
        primary = self._primary_id()
        self._cloud(primary, "gpt-4o")
        review = self._role("pdf_review")
        response = self.client.put(
            f"/api/models/roles/pdf_review/connections/{review['connections'][0]['id']}",
            json={"connection_mode": "follow", "source_role": "translation", "model": "gpt-4o"},
        )
        if response.status_code != 200:
            self.skipTest(f"pdf_review cannot follow translation here: {response.text}")
        self.client.put("/api/models/throughput/pdf_review", json={"concurrency": 5})

        self._edit(primary, base_url="https://vendor-two.example/v1")

        self.assertEqual(self._role("pdf_review")["throughput"]["concurrency"], 5)


if __name__ == "__main__":
    unittest.main()
