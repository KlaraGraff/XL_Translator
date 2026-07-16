from __future__ import annotations

import tempfile
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from openpyxl import Workbook

import settings as settings_module
from api.app import create_app
from api.task_manager import TranslationTaskManager
from core import data_migration, diagnostics, tm_manager
from core.model_api_identity import TaskApiContext
from core.model_catalog import ModelCatalogResult
from core.task_runner import DoneMsg, LogMsg, ProgressMsg
from core.update_checker import UpdateCheckResult
from settings import AppSettings


class _FinishedRunner:
    def __init__(self) -> None:
        self._messages = deque(
            [
                ProgressMsg(1, 3, "scan", 1, 1),
                LogMsg("INFO", "started"),
                DoneMsg("/tmp/out", [], 0.1, 0, 0),
            ]
        )

    def start(self) -> None:
        return None

    def stop(self) -> None:
        self._messages.clear()

    def needs_poll(self) -> bool:
        return bool(self._messages)

    def get_message(self, timeout: float = 0.05):
        return self._messages.popleft() if self._messages else None


class _BlockingRunner:
    def __init__(self) -> None:
        self._running = True

    def start(self) -> None:
        return None

    def stop(self) -> None:
        self._running = False

    def needs_poll(self) -> bool:
        return self._running

    def get_message(self, timeout: float = 0.05):
        return None


class ApiAppTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary_directory.name)
        self.addCleanup(self._temporary_directory.cleanup)
        self._patchers = [
            patch.multiple(
                settings_module,
                APP_DATA_DIR=self.root / "app-data",
                SETTINGS_PATH=self.root / "app-data" / "settings.json",
                KEYS_PATH=self.root / "app-data" / "keys.json",
                BACKUPS_DIR=self.root / "app-data" / "backups",
            ),
            patch.object(tm_manager, "DB_PATH", self.root / "app-data" / "tm.db"),
            patch.object(diagnostics, "DIAGNOSTIC_RECORDS_DIR", self.root / "diagnostics"),
            patch.object(diagnostics, "LOG_PATH", self.root / "app-data" / "app.log"),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(create_app())

    def test_settings_keys_and_source_scan(self) -> None:
        self.assertEqual(self.client.get("/api/settings").status_code, 200)
        updated = self.client.put("/api/settings", json={"target_lang": "fr"})
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["target_lang"], "fr")

        key_response = self.client.put(
            "/api/keys/custom_openai",
            json={
                "api_key": "test-secret",
                "base_url": "https://api.example.test/v1",
            },
        )
        self.assertEqual(key_response.status_code, 200)
        listed_keys = self.client.get("/api/keys")
        self.assertEqual(listed_keys.status_code, 200)
        self.assertNotIn("test-secret", listed_keys.text)
        self.assertTrue(listed_keys.json()["keys"][0]["has_key"])

        workbook_path = self.root / "sample.xlsx"
        workbook = Workbook()
        workbook.active.title = "Sheet 1"
        workbook.save(workbook_path)
        scan = self.client.post(
            "/api/sources/scan",
            json={"surface": "excel", "path": str(workbook_path)},
        )
        self.assertEqual(scan.status_code, 200)
        self.assertEqual(scan.json()["items"][0]["path"], str(workbook_path))
        self.assertEqual(scan.json()["items"][0]["sheets"], ["Sheet 1"])

    def test_tasks_sse_and_resource_locks(self) -> None:
        context = TaskApiContext(
            frozenset({("cloud", "custom_openai", "https://api.test/v1", "hash")} ),
            {},
        )
        manager = TranslationTaskManager(settings_loader=AppSettings)
        manager._scan = lambda *_args: [object()]
        manager._build_runner = lambda **_kwargs: _FinishedRunner()
        client = TestClient(create_app(task_manager=manager))
        with patch("api.task_manager.task_api_context_for_page", return_value=context):
            started = client.post(
                "/api/tasks",
                json={"surface": "excel", "source_path": str(self.root)},
            )
            self.assertEqual(started.status_code, 202)
            task_id = started.json()["task_id"]
            events = client.get(f"/api/tasks/{task_id}/events")

        self.assertEqual(events.status_code, 200)
        self.assertIn("event: start", events.text)
        self.assertIn("event: progress", events.text)
        self.assertIn("event: log", events.text)
        self.assertIn("event: done", events.text)
        self.assertEqual(client.get(f"/api/tasks/{task_id}").json()["state"], "done")

        blocking_manager = TranslationTaskManager(settings_loader=AppSettings)
        blocking_manager._scan = lambda *_args: [object()]
        blocking_manager._build_runner = lambda **_kwargs: _BlockingRunner()
        blocked_client = TestClient(create_app(task_manager=blocking_manager))
        with patch("api.task_manager.task_api_context_for_page", return_value=context):
            first = blocked_client.post(
                "/api/tasks",
                json={"surface": "excel", "source_path": str(self.root)},
            )
            self.assertEqual(first.status_code, 202)
            conflict = blocked_client.post(
                "/api/tasks",
                json={"surface": "word", "source_path": str(self.root)},
            )
            self.assertEqual(conflict.status_code, 409)
            locks = blocked_client.get("/api/tasks/locks/current")
            self.assertEqual(len(locks.json()["reservations"]), 1)
            stopped = blocked_client.post(f"/api/tasks/{first.json()['task_id']}/stop")
            self.assertEqual(stopped.status_code, 200)

    def test_tm_models_and_connectivity_endpoints(self) -> None:
        created = self.client.post(
            "/api/tm/entries",
            json={"source_text": "术语", "target_text": "term", "lang_pair": "zh-en"},
        )
        self.assertEqual(created.status_code, 201)
        entries = self.client.get("/api/tm/entries?lang_pair=zh-en")
        self.assertEqual(entries.status_code, 200)
        entry_id = entries.json()["entries"][0]["id"]
        self.assertEqual(
            self.client.post(f"/api/tm/entries/{entry_id}/pin", json={"pinned": True}).status_code,
            200,
        )
        exported = self.client.get("/api/tm/export?lang_pair=zh-en")
        self.assertEqual(exported.status_code, 200)
        self.assertEqual(
            self.client.post(
                "/api/tm/import",
                json={
                    "lang_pair": "zh-en",
                    "mode": "skip",
                    "entries": exported.json()["entries"],
                },
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.post(
                "/api/tm/clean/apply",
                json={
                    "auto_pin": False,
                    "suggestions": [
                        {
                            "entry_id": entry_id,
                            "source_text": "术语",
                            "old_target": "term",
                            "new_target": "terminology",
                        }
                    ],
                },
            ).status_code,
            200,
        )

        self.assertEqual(self.client.get("/api/models/roles").status_code, 200)
        throughput = self.client.put(
            "/api/models/throughput/translation",
            json={"batch_size": 12, "concurrency": 3},
        )
        self.assertEqual(throughput.status_code, 200)
        with patch(
            "api.app.fetch_openai_compatible_models",
            return_value=ModelCatalogResult(True, ["model-a"], "ok", "ok"),
        ):
            fetched = self.client.post(
                "/api/models/fetch",
                json={
                    "provider": "custom_openai",
                    "base_url": "https://api.example.test/v1",
                    "api_key": "test-secret",
                },
            )
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.json()["models"], ["model-a"])
        for endpoint in (
            "/api/models/connectivity/text",
            "/api/models/connectivity/image",
            "/api/models/connectivity/pdf-review",
        ):
            self.assertEqual(self.client.post(endpoint).status_code, 200)

    def test_model_config_update_diagnostics_and_migration_endpoints(self) -> None:
        imported = self.client.post(
            "/api/model-config/import",
            json={
                "model_config": {
                    "engine": {
                        "cloud_provider": "custom_openai",
                        "cloud_model": "imported-model",
                        "cloud_base_url": "https://import.example/v1",
                    }
                },
                "api_keys": {"custom_openai": "imported-secret"},
            },
        )
        self.assertEqual(imported.status_code, 200)
        exported = self.client.get("/api/model-config/export")
        self.assertEqual(exported.status_code, 200)
        self.assertEqual(
            exported.json()["model_profiles"]["translation"]["cloud"]["model"],
            "imported-model",
        )

        with patch(
            "core.update_checker.check_for_updates",
            return_value=UpdateCheckResult(True, "current", "Current"),
        ):
            self.assertEqual(self.client.get("/api/updates/check").status_code, 200)
        self.assertEqual(
            self.client.put(
                "/api/updates/preferences",
                json={"ignore_updates": True, "ignored_release_version": "9.0.0"},
            ).status_code,
            200,
        )

        self.assertEqual(self.client.get("/api/diagnostics").status_code, 200)
        self.assertEqual(self.client.get("/api/diagnostics/history.zip").status_code, 200)

        plan = data_migration.inspect_data_migration(
            app_data_dir=self.root / "migration-target",
            legacy_data_dir=self.root / "legacy",
            legacy_launcher_dir=self.root / "legacy-launcher",
        )
        with patch("api.app.data_migration.inspect_data_migration", return_value=plan):
            migration = self.client.post(
                "/api/migration/apply",
                json={"action": "skip"},
            )
        self.assertEqual(migration.status_code, 200)
        self.assertEqual(migration.json()["status"], "skipped")

    def test_token_protects_api_routes_but_not_health(self) -> None:
        client = TestClient(create_app(auth_token="sidecar-token"))
        self.assertEqual(client.get("/health").status_code, 200)
        self.assertEqual(client.get("/api/settings").status_code, 401)
        self.assertEqual(
            client.get(
                "/api/settings",
                headers={"X-Translator-Token": "sidecar-token"},
            ).status_code,
            200,
        )

    def test_tauri_origins_receive_cors_headers(self) -> None:
        response = self.client.options(
            "/api/settings",
            headers={
                "Origin": "tauri://localhost",
                "Access-Control-Request-Method": "GET",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["access-control-allow-origin"],
            "tauri://localhost",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
