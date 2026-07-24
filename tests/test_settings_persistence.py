from __future__ import annotations

import json
import multiprocessing
import os
import stat
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import settings as settings_module
from config import SETTINGS_SCHEMA_VERSION
from settings import AppSettings, SettingsSchemaError


def _save_keys_in_process(root: str, worker_index: int, start_event) -> None:
    app_data_dir = Path(root)
    settings_module.APP_DATA_DIR = app_data_dir
    settings_module.KEYS_PATH = app_data_dir / "keys.json"
    start_event.wait()
    for item_index in range(5):
        settings_module.save_key(
            f"provider-{worker_index}-{item_index}",
            f"secret-{worker_index}-{item_index}",
        )


def _save_settings_in_process(root: str, worker_index: int, start_event) -> None:
    app_data_dir = Path(root)
    settings_module.APP_DATA_DIR = app_data_dir
    settings_module.SETTINGS_PATH = app_data_dir / "settings.json"
    start_event.wait()
    for item_index in range(5):
        settings_module.save_settings(
            AppSettings(custom_prompt=f"prompt-{worker_index}-{item_index}")
        )


class SettingsPersistenceTests(unittest.TestCase):
    def test_concurrent_settings_saves_use_independent_atomic_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app_data_dir = Path(tmp)
            settings_path = app_data_dir / "settings.json"
            with patch.multiple(
                settings_module,
                APP_DATA_DIR=app_data_dir,
                SETTINGS_PATH=settings_path,
            ):
                payloads = [
                    AppSettings(target_lang="fr", custom_prompt=f"prompt-{index}")
                    for index in range(80)
                ]
                with ThreadPoolExecutor(max_workers=8) as executor:
                    list(executor.map(settings_module.save_settings, payloads))

            persisted = json.loads(settings_path.read_text(encoding="utf-8"))
            AppSettings.model_validate(persisted)
            self.assertIn(
                persisted["custom_prompt"],
                {f"prompt-{index}" for index in range(80)},
            )
            self.assertEqual(list(app_data_dir.glob(".settings.json.*.tmp")), [])

    def test_concurrent_process_settings_saves_remain_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = multiprocessing.get_context("spawn")
            start_event = context.Event()
            processes = [
                context.Process(
                    target=_save_settings_in_process,
                    args=(tmp, worker_index, start_event),
                )
                for worker_index in range(6)
            ]
            for process in processes:
                process.start()
            start_event.set()
            for process in processes:
                process.join(timeout=30)
                self.assertEqual(process.exitcode, 0)

            settings_path = Path(tmp) / "settings.json"
            persisted = json.loads(settings_path.read_text(encoding="utf-8"))
            AppSettings.model_validate(persisted)
            self.assertRegex(persisted["custom_prompt"], r"^prompt-[0-5]-[0-4]$")
            self.assertEqual(list(Path(tmp).glob(".settings.json.*.tmp")), [])

    def test_concurrent_process_key_updates_do_not_lose_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = multiprocessing.get_context("spawn")
            start_event = context.Event()
            processes = [
                context.Process(
                    target=_save_keys_in_process,
                    args=(tmp, worker_index, start_event),
                )
                for worker_index in range(6)
            ]
            for process in processes:
                process.start()
            start_event.set()
            for process in processes:
                process.join(timeout=30)
                self.assertEqual(process.exitcode, 0)

            keys_path = Path(tmp) / "keys.json"
            keys = json.loads(keys_path.read_text(encoding="utf-8"))
            expected = {
                f"provider-{worker_index}-{item_index}":
                    f"secret-{worker_index}-{item_index}"
                for worker_index in range(6)
                for item_index in range(5)
            }
            self.assertEqual(keys, expected)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(keys_path.stat().st_mode), 0o600)

    def test_incompatible_settings_are_not_migrated_or_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app_data_dir = Path(tmp)
            settings_path = app_data_dir / "settings.json"
            settings_path.write_text(
                json.dumps(
                    {
                        "settings_version": SETTINGS_SCHEMA_VERSION - 1,
                        "target_lang": "fr",
                        "custom_prompt": "keep-me",
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch.multiple(
                    settings_module,
                    APP_DATA_DIR=app_data_dir,
                    SETTINGS_PATH=settings_path,
                    KEYS_PATH=app_data_dir / "keys.json",
                ),
            ):
                loaded = settings_module.load_settings()
                status = settings_module.get_settings_schema_status()
                with self.assertRaises(SettingsSchemaError):
                    settings_module.save_settings(AppSettings())

            self.assertEqual(status["state"], "incompatible")
            self.assertEqual(loaded.target_lang, AppSettings().target_lang)
            self.assertEqual(loaded.custom_prompt, "")
            self.assertEqual(
                json.loads(settings_path.read_text(encoding="utf-8"))["custom_prompt"],
                "keep-me",
            )

    def test_load_keeps_valid_normalized_settings_when_rewrite_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app_data_dir = Path(tmp)
            settings_path = app_data_dir / "settings.json"
            settings_path.write_text(
                json.dumps(
                    {
                        "settings_version": SETTINGS_SCHEMA_VERSION,
                        "target_lang": "fr",
                        "custom_prompt": "keep-normalized",
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch.multiple(
                    settings_module,
                    APP_DATA_DIR=app_data_dir,
                    SETTINGS_PATH=settings_path,
                    KEYS_PATH=app_data_dir / "keys.json",
                ),
                patch.object(
                    settings_module,
                    "save_settings",
                    side_effect=OSError("read-only filesystem"),
                ),
            ):
                loaded = settings_module.load_settings()

            self.assertEqual(loaded.target_lang, "fr")
            self.assertEqual(loaded.custom_prompt, "keep-normalized")

    def test_malformed_key_store_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app_data_dir = Path(tmp)
            keys_path = app_data_dir / "keys.json"
            keys_path.write_text("[]", encoding="utf-8")

            with patch.multiple(
                settings_module,
                APP_DATA_DIR=app_data_dir,
                KEYS_PATH=keys_path,
            ):
                with self.assertRaisesRegex(ValueError, "无法安全更新"):
                    settings_module.save_key("custom_openai", "new-secret")

            self.assertEqual(keys_path.read_text(encoding="utf-8"), "[]")

    def test_malformed_key_store_reads_as_empty_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app_data_dir = Path(tmp)
            keys_path = app_data_dir / "keys.json"
            keys_path.write_text("[]", encoding="utf-8")

            with patch.multiple(
                settings_module,
                APP_DATA_DIR=app_data_dir,
                KEYS_PATH=keys_path,
            ):
                self.assertEqual(settings_module.load_keys(), {})
                self.assertEqual(settings_module.get_key("custom_openai"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
