"""Isolated Phase 8 contracts for maintenance, reset, diagnostics, and updates."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

import settings as settings_module
from api.app import create_app
from core import diagnostics, maintenance, tm_manager
from core.update_checker import UpdateCheckResult


class _TaskManager:
    def __init__(self, active_count: int) -> None:
        self._active_count = active_count

    def active_task_count(self) -> int:
        return self._active_count


class Phase8MaintenanceContractsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.app_data = self.root / "Translator"
        self.legacy_data = self.root / ".xl_translator"
        self.source = self.root / "source.xlsx"
        self.output = self.root / "translated.xlsx"
        self.legacy_data.mkdir()
        self.legacy_data.joinpath("settings.json").write_text("legacy", encoding="utf-8")
        self.source.write_text("source", encoding="utf-8")
        self.output.write_text("output", encoding="utf-8")

        settings_path = self.app_data / "settings.json"
        keys_path = self.app_data / "keys.json"
        log_path = self.app_data / "app.log"
        records_dir = self.app_data / "diagnostics" / "records"
        self._patchers = [
            patch.multiple(
                settings_module,
                APP_DATA_DIR=self.app_data,
                SETTINGS_PATH=settings_path,
                KEYS_PATH=keys_path,
            ),
            patch.multiple(
                maintenance,
                APP_DATA_DIR=self.app_data,
                SETTINGS_PATH=settings_path,
                KEYS_PATH=keys_path,
                LOG_PATH=log_path,
                TASK_HISTORY_PATH=self.app_data / "task_history.json",
                WORKSPACES_DIR=self.app_data / "workspaces",
                API_HEALTH_STATE_PATH=self.app_data / "api_health_state.json",
            ),
            patch.multiple(
                diagnostics,
                APP_DATA_DIR=self.app_data,
                DIAGNOSTICS_DIR=self.app_data / "diagnostics",
                DIAGNOSTIC_RECORDS_DIR=records_dir,
                LOG_PATH=log_path,
            ),
            patch.object(tm_manager, "DB_PATH", self.app_data / "tm.db"),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_reset_only_removes_current_owned_data_and_never_sources_outputs_or_legacy(self) -> None:
        self.app_data.mkdir()
        self.app_data.joinpath("settings.json").write_text("{}", encoding="utf-8")
        self.app_data.joinpath("keys.json").write_text('{"provider":"secret"}', encoding="utf-8")
        owned_workspace = self.app_data / "workspaces" / "task-1"
        owned_workspace.mkdir(parents=True)
        owned_workspace.joinpath(".translator-workspace.json").write_text("{}", encoding="utf-8")

        result = maintenance.reset_all_local_data()

        self.assertEqual(result.category, "reset_full")
        self.assertTrue(result.restart_required)
        self.assertFalse(self.app_data.joinpath("settings.json").exists())
        self.assertTrue(self.source.exists())
        self.assertTrue(self.output.exists())
        self.assertEqual(self.legacy_data.joinpath("settings.json").read_text(encoding="utf-8"), "legacy")

    def test_workspace_cleanup_requires_the_translator_owned_marker(self) -> None:
        owned = self.app_data / "workspaces" / "owned"
        unowned = self.app_data / "workspaces" / "unowned"
        owned.mkdir(parents=True)
        unowned.mkdir(parents=True)
        owned.joinpath(".translator-workspace.json").write_text("{}", encoding="utf-8")
        unowned.joinpath("user-file.txt").write_text("keep", encoding="utf-8")

        result = maintenance.clear_owned_workspaces()

        self.assertEqual(result.removed_count, 1)
        self.assertFalse(owned.exists())
        self.assertTrue(unowned.exists())

    def test_diagnostic_export_has_no_key_content_paths_or_document_text(self) -> None:
        record = diagnostics.archive_task_diagnostics(
            surface="excel",
            phase="translation",
            task_id="task-with-private-name",
            settings=SimpleNamespace(),
            selected_files=[self.source],
            logs=[{"level": "error", "message": "source_text=confidential translation"}],
            error_message="Bearer top-secret-token failed for /private/user/source.xlsx",
            source_root=self.root,
            status="failed",
        )

        payload, _filename = diagnostics.build_diagnostic_zip_bytes(record)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            extracted = "\n".join(
                archive.read(name).decode("utf-8", errors="replace")
                for name in archive.namelist()
                if name.endswith((".json", ".csv", ".txt"))
            )
        for forbidden in ("top-secret-token", "confidential translation", str(self.source), "private-name"):
            self.assertNotIn(forbidden, extracted)

    def test_overview_exposes_only_counts_and_marks_outputs_protected(self) -> None:
        self.app_data.mkdir()
        self.app_data.joinpath("keys.json").write_text('{"provider":"secret"}', encoding="utf-8")

        overview = maintenance.data_overview(active_task_count=2)
        rendered = json.dumps(overview, ensure_ascii=False)

        self.assertTrue(overview["outputs_protected"])
        self.assertEqual(overview["active_task_count"], 2)
        self.assertNotIn("secret", rendered)
        self.assertIn("keys", [item["id"] for item in overview["categories"]])

    def test_active_tasks_block_key_deletion_and_full_reset(self) -> None:
        client = TestClient(create_app(task_manager=_TaskManager(active_count=1)))

        key_clear = client.post(
            "/api/maintenance/clear",
            json={"category": "keys", "confirmation": True},
        )
        full_reset = client.post(
            "/api/maintenance/reset-full",
            json={"confirmation": True, "phrase": "RESET"},
        )

        self.assertEqual(key_clear.status_code, 409)
        self.assertEqual(full_reset.status_code, 409)

    def test_old_data_migration_code_and_routes_are_absent(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self.assertFalse((root / "core" / "data_migration.py").exists())
        api_source = (root / "api" / "app.py").read_text(encoding="utf-8")
        self.assertNotIn("/api/migration/", api_source)

    def test_background_update_waits_for_quick_start_but_manual_check_is_available(self) -> None:
        client = TestClient(create_app(task_manager=_TaskManager(active_count=0)))
        with patch("core.update_checker.check_for_updates") as check:
            background = client.get("/api/updates/check?mode=background")
        self.assertEqual(background.status_code, 200)
        self.assertEqual(background.json()["status"], "deferred")
        check.assert_not_called()

        with patch(
            "core.update_checker.check_for_updates",
            return_value=UpdateCheckResult(True, "current", "Current"),
        ) as check:
            manual = client.get("/api/updates/check?mode=manual")
        self.assertEqual(manual.status_code, 200)
        check.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
