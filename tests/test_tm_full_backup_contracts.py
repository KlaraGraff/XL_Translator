"""Full TM JSON backup contracts using isolated SQLite/settings state."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from api.app import create_app
from core import tm_manager
from core.language_registry import CustomTargetLang, build_custom_target_lang_code
from settings import AppSettings


class FullTmBackupContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.old_db_path = tm_manager.DB_PATH
        self.old_backups_dir = tm_manager.BACKUPS_DIR
        tm_manager.DB_PATH = self.root / "source-tm.db"
        tm_manager.BACKUPS_DIR = self.root / "backups"
        tm_manager.init_db()

    def tearDown(self) -> None:
        tm_manager.DB_PATH = self.old_db_path
        tm_manager.BACKUPS_DIR = self.old_backups_dir
        self.temp_dir.cleanup()

    def test_full_backup_restores_status_provenance_and_custom_definition(self) -> None:
        custom_code = build_custom_target_lang_code("Project dialect")
        custom_definition = CustomTargetLang(
            name="Project dialect",
            description="Project terminology",
            code=custom_code,
        )
        entries = [
            {
                "source_text": "automatic",
                "target_text": "自动",
                "word_type": "auto",
                "pinned": 0,
                "source_engine": "translation-model",
                "created_at": "2026-07-20 10:00:00",
                "updated_at": "2026-07-20 10:01:00",
            },
            {
                "source_text": "reviewed",
                "target_text": "已审核",
                "word_type": "reviewed_auto",
                "pinned": 0,
                "source_engine": "cleaner-model",
                "created_at": "2026-07-20 10:02:00",
                "updated_at": "2026-07-20 10:03:00",
            },
            {
                "source_text": "locked",
                "target_text": "清洗锁定",
                "word_type": "cleaning_locked",
                "pinned": 0,
                "source_engine": "cleaner-model",
                "created_at": "2026-07-20 10:04:00",
                "updated_at": "2026-07-20 10:05:00",
            },
            {
                "source_text": "fixed",
                "target_text": "用户固定",
                "word_type": "manual",
                "pinned": 1,
                "source_engine": "manual",
                "created_at": "2026-07-20 10:06:00",
                "updated_at": "2026-07-20 10:07:00",
            },
        ]
        result = tm_manager.import_entries(
            entries,
            "en-zh",
            "overwrite",
            preserve_status=True,
        )
        self.assertEqual(result["inserted"], 4)
        self.assertEqual(
            tm_manager.import_entries(
                [
                    {
                        "source_text": "beam",
                        "target_text": "梁",
                        "word_type": "manual",
                        "source_engine": "manual",
                    }
                ],
                f"en-{custom_code}",
                "overwrite",
                preserve_status=True,
                sync_reverse=True,
            )["inserted"],
            1,
        )
        self.assertIsNone(
            tm_manager.lookup_batch(["梁"], f"{custom_code}-en")["梁"]
        )

        backup = tm_manager.get_full_export([custom_definition])
        self.assertEqual(backup["format_version"], "tm-full-v1")
        self.assertEqual(backup["custom_target_langs"], [custom_definition.model_dump()])
        exported = {
            (row["source_text"], row["lang_pair"]): row
            for row in backup["entries"]
        }
        self.assertEqual(exported[("reviewed", "en-zh")]["word_type"], "reviewed_auto")
        self.assertEqual(exported[("fixed", "en-zh")]["pinned"], 1)
        self.assertTrue(exported[("beam", f"en-{custom_code}")]["source_hash"])

        tm_manager.DB_PATH = self.root / "restored-tm.db"
        tm_manager.init_db()
        by_pair: dict[str, list[dict]] = {}
        for row in backup["entries"]:
            by_pair.setdefault(row["lang_pair"], []).append(row)
        for lang_pair, rows in by_pair.items():
            tm_manager.import_entries(
                rows,
                lang_pair,
                "overwrite",
                preserve_status=True,
            )

        restored, _ = tm_manager.search_entries("en-zh", keyword="reviewed")
        self.assertEqual(restored[0]["word_type"], "reviewed_auto")
        self.assertEqual(restored[0]["source_engine"], "cleaner-model")
        self.assertEqual(restored[0]["updated_at"], "2026-07-20 10:03:00")
        pinned, _ = tm_manager.search_entries("en-zh", keyword="fixed")
        self.assertEqual(pinned[0]["pinned"], 1)

    def test_full_import_maps_custom_target_and_rejects_undefined_codes(self) -> None:
        source_code = build_custom_target_lang_code("Incoming project dialect")
        mapped_code = build_custom_target_lang_code("Current project dialect")
        settings = AppSettings()
        payload = {
            "format_version": "tm-full-v1",
            "custom_target_langs": [
                {
                    "name": "Incoming project dialect",
                    "description": "Imported from a backup",
                    "code": source_code,
                }
            ],
            "entries": [
                {
                    "source_text": "beam",
                    "target_text": "梁",
                    "lang_pair": f"en-{source_code}",
                    "word_type": "cleaning_locked",
                    "pinned": 0,
                    "source_engine": "cleaner-model",
                }
            ],
            "code_map": {source_code: mapped_code},
            "sync_reverse": True,
        }
        with patch("api.app.load_settings", return_value=settings), patch(
            "api.app.save_settings"
        ):
            client = TestClient(create_app())
            response = client.post("/api/tm/import/full", json=payload)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["inserted"], 1)

            self.assertEqual(settings.custom_target_langs[0].code, mapped_code)
            self.assertEqual(
                tm_manager.lookup_batch(["beam"], f"en-{mapped_code}")["beam"], "梁"
            )
            self.assertIsNone(
                tm_manager.lookup_batch(["梁"], f"{mapped_code}-en")["梁"]
            )

            invalid = client.post(
                "/api/tm/import/full",
                json={
                    "format_version": "tm-full-v1",
                    "entries": [
                        {
                            "source_text": "orphan",
                            "target_text": "孤立",
                            "lang_pair": "en-x-custom-missing",
                        }
                    ],
                },
            )
            self.assertEqual(invalid.status_code, 422)

    def test_full_import_restores_conflict_candidates_and_endpoint_listing(self) -> None:
        tm_manager.insert_manual_entry("same", "当前译文", "en-zh", sync_reverse=False)
        tm_manager.import_entries(
            [{"source_text": "same", "target_text": "候选译文"}],
            "en-zh",
            "keep_both",
            sync_reverse=False,
        )
        backup = tm_manager.get_full_export()
        self.assertEqual(len(backup["conflict_candidates"]), 1)

        tm_manager.DB_PATH = self.root / "restored-conflicts.db"
        tm_manager.init_db()
        settings = AppSettings()
        with patch("api.app.load_settings", return_value=settings), patch(
            "api.app.save_settings"
        ):
            client = TestClient(create_app())
            response = client.post("/api/tm/import/full", json=backup)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["conflicts"], 1)
            listed = client.get("/api/tm/conflicts?lang_pair=en-zh")
            self.assertEqual(listed.status_code, 200)
            conflicts = listed.json()["conflicts"]
            self.assertEqual(len(conflicts), 1)
            self.assertEqual(conflicts[0]["candidate_target"], "候选译文")

            resolved = client.post(
                f"/api/tm/conflicts/{conflicts[0]['id']}/resolve",
                json={"action": "use_candidate"},
            )
            self.assertEqual(resolved.status_code, 200, resolved.text)
            self.assertEqual(
                tm_manager.lookup_batch(["same"], "en-zh")["same"], "候选译文"
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
