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
from settings import AppSettings


def _isolated_settings(module, app_data_dir: Path):
    """Keep every settings-owned path inside one temporary directory."""
    return patch.multiple(
        module,
        APP_DATA_DIR=app_data_dir,
        SETTINGS_PATH=app_data_dir / "settings.json",
        KEYS_PATH=app_data_dir / "keys.json",
        RECOVERY_PATH=app_data_dir / "recovery.json",
        BACKUPS_DIR=app_data_dir / "backups",
    )


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

    def test_older_but_valid_settings_are_adopted_and_restamped(self) -> None:
        """An older schema version alone must never cost the user their settings."""
        with tempfile.TemporaryDirectory() as tmp:
            app_data_dir = Path(tmp)
            settings_path = app_data_dir / "settings.json"
            settings_path.write_text(
                json.dumps(
                    {
                        "settings_version": SETTINGS_SCHEMA_VERSION - 1,
                        "target_lang": "fr",
                        "last_source_folder": "keep-me",
                    }
                ),
                encoding="utf-8",
            )

            with _isolated_settings(settings_module, app_data_dir):
                loaded = settings_module.load_settings()
                status = settings_module.get_settings_schema_status()
                settings_module.save_settings(loaded)
                status_after = settings_module.get_settings_schema_status()
                recovery = settings_module.read_recovery_record()

            self.assertEqual(status["state"], "adopted")
            self.assertTrue(status["can_write"])
            self.assertEqual(status["stored_version"], SETTINGS_SCHEMA_VERSION - 1)
            # Taken over as-is, not replaced by defaults.
            self.assertEqual(loaded.target_lang, "fr")
            self.assertEqual(loaded.last_source_folder, "keep-me")

            persisted = json.loads(settings_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["last_source_folder"], "keep-me")
            self.assertEqual(persisted["settings_version"], SETTINGS_SCHEMA_VERSION)
            self.assertEqual(status_after["state"], "current")
            # Adoption keeps everything, so the user is told nothing.
            self.assertEqual(recovery, {})
            self.assertFalse((app_data_dir / "backups").exists())

    def test_unreadable_settings_are_backed_up_and_rebuilt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app_data_dir = Path(tmp)
            settings_path = app_data_dir / "settings.json"
            settings_path.write_text("{ not json at all", encoding="utf-8")

            with _isolated_settings(settings_module, app_data_dir):
                loaded = settings_module.load_settings()
                # Loading is read-only: the broken file is still there, and no
                # backup has been taken yet.
                status_after_load = settings_module.get_settings_schema_status()
                self.assertEqual(status_after_load["state"], "unusable")
                self.assertEqual(
                    settings_path.read_text(encoding="utf-8"), "{ not json at all"
                )
                self.assertEqual(settings_module.read_recovery_record(), {})

                self.assertTrue(settings_module.recover_settings_file_if_needed())
                status = settings_module.get_settings_schema_status()
                recovery = settings_module.read_recovery_record()
                # The rebuilt file must accept writes straight away.
                settings_module.save_settings(loaded)

            self.assertEqual(loaded.custom_prompt, "")
            self.assertEqual(status["state"], "current")
            self.assertTrue(status["can_write"])

            event = recovery["settings"]
            backup = Path(event["backup_path"])
            self.assertTrue(backup.is_file())
            self.assertEqual(backup.read_text(encoding="utf-8"), "{ not json at all")
            self.assertEqual(
                json.loads(settings_path.read_text(encoding="utf-8"))["settings_version"],
                SETTINGS_SCHEMA_VERSION,
            )

    # os.geteuid does not exist on Windows, and this is evaluated at import
    # time — leaving it unguarded would fail collection and take every test in
    # this file down with it on the platform we also ship a build for.
    @unittest.skipIf(
        os.name == "nt" or os.geteuid() == 0,
        "chmod 000 does not deny root, and does not mean this on Windows",
    )
    def test_a_settings_file_that_cannot_be_read_is_never_overwritten(self) -> None:
        """No backup, no rebuild.  A file we cannot copy is not ours to replace.

        A permission problem — or on Windows an AV/backup product holding the
        file open — is not a broken configuration.  The content is very likely
        intact, and rebuilding without a copy set aside would destroy the
        user's entire configuration with nothing to point them at.
        """
        with tempfile.TemporaryDirectory() as tmp:
            app_data_dir = Path(tmp)
            settings_path = app_data_dir / "settings.json"
            original = json.dumps(
                {"settings_version": SETTINGS_SCHEMA_VERSION, "custom_prompt": "precious"}
            )
            settings_path.write_text(original, encoding="utf-8")
            settings_path.chmod(0o000)
            # Restore the mode even if an assertion aborts the test, or the
            # temporary directory cannot be cleaned up afterwards.
            def _restore_mode() -> None:
                if settings_path.exists():
                    settings_path.chmod(stat.S_IRUSR | stat.S_IWUSR)

            self.addCleanup(_restore_mode)

            with _isolated_settings(settings_module, app_data_dir):
                status = settings_module.get_settings_schema_status()
                # Reading falls back to defaults in memory only.
                self.assertEqual(settings_module.load_settings().custom_prompt, "")
                # Saving refuses rather than destroying what it could not copy.
                with self.assertRaises(settings_module.SettingsSchemaError) as caught:
                    settings_module.save_settings(AppSettings(custom_prompt="new"))
                # The explicit maintenance-page reset is the way out.
                settings_module.save_settings(
                    AppSettings(custom_prompt="after-reset"), replace_incompatible=True
                )
                after_reset = json.loads(settings_path.read_text(encoding="utf-8"))

            self.assertEqual(status["state"], "unreadable")
            self.assertFalse(status["can_write"])
            # The error names the directory it failed on, not a generic string.
            self.assertIn("备份", str(caught.exception))
            self.assertEqual(after_reset["custom_prompt"], "after-reset")

    def test_settings_failing_validation_are_backed_up_and_rebuilt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app_data_dir = Path(tmp)
            settings_path = app_data_dir / "settings.json"
            settings_path.write_text(
                json.dumps(
                    {
                        "settings_version": SETTINGS_SCHEMA_VERSION - 1,
                        "engine": "this should be an object",
                    }
                ),
                encoding="utf-8",
            )

            with _isolated_settings(settings_module, app_data_dir):
                status_before = settings_module.get_settings_schema_status()
                settings_module.recover_settings_file_if_needed()
                recovery = settings_module.read_recovery_record()

            self.assertEqual(status_before["state"], "unusable")
            self.assertFalse(status_before["can_write"])
            self.assertEqual(
                recovery["settings"]["stored_version"], SETTINGS_SCHEMA_VERSION - 1
            )
            self.assertTrue(Path(recovery["settings"]["backup_path"]).is_file())

    def test_a_directory_occupying_settings_json_never_blocks_the_reset(self) -> None:
        """目录占住 settings.json 时，显式重置仍必须留下一条出路。

        A sync tool resolving a conflict can leave a directory at the exact
        path: ``read_bytes`` fails, so it is classified ``unusable``, and the
        rebuild used to die in ``os.replace`` with IsADirectoryError — the
        maintenance page's reset was blocked too, leaving no way out at all.
        The placeholder is now moved into the backups directory first.
        """
        with tempfile.TemporaryDirectory() as tmp:
            app_data_dir = Path(tmp)
            settings_path = app_data_dir / "settings.json"
            settings_path.mkdir(parents=True)
            (settings_path / "leftover.txt").write_text("占位", encoding="utf-8")

            with _isolated_settings(settings_module, app_data_dir):
                status_before = settings_module.get_settings_schema_status()
                # A directory holds no configuration to protect, so it is not
                # lumped in with a file that merely cannot be read right now.
                self.assertEqual(status_before["state"], "unusable")
                self.assertFalse(status_before["can_write"])

                rebuilt = settings_module._recreate_settings_file(None, force=True)
                status_after = settings_module.get_settings_schema_status()
                recovery = settings_module.read_recovery_record()
                loaded = settings_module.load_settings()
                # The rebuilt file must accept writes straight away.
                settings_module.save_settings(loaded)

            self.assertTrue(settings_path.is_file())
            self.assertEqual(status_after["state"], "current")
            self.assertEqual(loaded.settings_version, rebuilt.settings_version)
            self.assertEqual(loaded.target_lang, rebuilt.target_lang)

            backup = Path(recovery["settings"]["backup_path"])
            self.assertTrue(backup.is_dir())
            self.assertEqual(
                (backup / "leftover.txt").read_text(encoding="utf-8"), "占位"
            )
            self.assertEqual(list(app_data_dir.glob(".settings.json.*.tmp")), [])

    def test_a_directory_that_cannot_be_moved_is_never_deleted_without_a_reset(self) -> None:
        """挪不走的占位目录：没有显式重置就原样保留并报错，不能悄悄删掉。"""
        with tempfile.TemporaryDirectory() as tmp:
            app_data_dir = Path(tmp)
            settings_path = app_data_dir / "settings.json"
            settings_path.mkdir(parents=True)
            (settings_path / "leftover.txt").write_text("占位", encoding="utf-8")

            with _isolated_settings(settings_module, app_data_dir), patch.object(
                settings_module.shutil, "move", side_effect=PermissionError("locked")
            ):
                with self.assertRaises(settings_module.SettingsSchemaError):
                    settings_module._recreate_settings_file(None)
                self.assertTrue((settings_path / "leftover.txt").is_file())

                # 用户点了「重置设置」才允许删掉它让路。
                settings_module._recreate_settings_file(None, force=True)
            self.assertTrue(settings_path.is_file())

    def test_the_maintenance_reset_survives_a_directory_at_settings_json(self) -> None:
        """维护页那颗「重置设置」按钮本身也必须能用（save_settings 的 force 路径）。

        The button is not a synthetic internal call: it reaches the rebuild
        through ``save_settings(..., replace_incompatible=True)``, which used
        to raise the same IsADirectoryError.  Both doors have to open.
        """
        with tempfile.TemporaryDirectory() as tmp:
            app_data_dir = Path(tmp)
            settings_path = app_data_dir / "settings.json"
            settings_path.mkdir(parents=True)

            with _isolated_settings(settings_module, app_data_dir):
                settings_module.save_settings(
                    AppSettings(custom_prompt="after-reset"),
                    replace_incompatible=True,
                )
                persisted = json.loads(settings_path.read_text(encoding="utf-8"))
                status = settings_module.get_settings_schema_status()
                recovery = settings_module.read_recovery_record()

            self.assertTrue(settings_path.is_file())
            self.assertEqual(persisted["custom_prompt"], "after-reset")
            self.assertEqual(status["state"], "current")
            self.assertTrue(Path(recovery["settings"]["backup_path"]).is_dir())

    def test_startup_recovery_also_clears_a_directory_at_settings_json(self) -> None:
        """启动自愈同样不能被占位目录挡住，否则应用一起来就废了。"""
        with tempfile.TemporaryDirectory() as tmp:
            app_data_dir = Path(tmp)
            settings_path = app_data_dir / "settings.json"
            settings_path.mkdir(parents=True)

            with _isolated_settings(settings_module, app_data_dir):
                self.assertTrue(settings_module.recover_settings_file_if_needed())
                self.assertEqual(
                    settings_module.get_settings_schema_status()["state"], "current"
                )
                settings_module.save_settings(
                    AppSettings(custom_prompt="written-after-recovery")
                )
                persisted = json.loads(settings_path.read_text(encoding="utf-8"))

            self.assertEqual(persisted["custom_prompt"], "written-after-recovery")

    def test_newer_settings_are_backed_up_and_rebuilt(self) -> None:
        """A downgrade cannot read the future file, but must still let the user work."""
        with tempfile.TemporaryDirectory() as tmp:
            app_data_dir = Path(tmp)
            settings_path = app_data_dir / "settings.json"
            settings_path.write_text(
                json.dumps(
                    {
                        "settings_version": SETTINGS_SCHEMA_VERSION + 5,
                        "custom_prompt": "from-the-future",
                    }
                ),
                encoding="utf-8",
            )

            with _isolated_settings(settings_module, app_data_dir):
                status_before = settings_module.get_settings_schema_status()
                # Never loaded: a write on its own has to clear the blockage.
                settings_module.save_settings(AppSettings(custom_prompt="written-now"))
                status_after = settings_module.get_settings_schema_status()
                recovery = settings_module.read_recovery_record()

            self.assertEqual(status_before["state"], "unusable")
            self.assertEqual(status_before["stored_version"], SETTINGS_SCHEMA_VERSION + 5)
            self.assertEqual(status_after["state"], "current")

            event = recovery["settings"]
            self.assertEqual(event["stored_version"], SETTINGS_SCHEMA_VERSION + 5)
            self.assertEqual(
                json.loads(Path(event["backup_path"]).read_text(encoding="utf-8"))[
                    "custom_prompt"
                ],
                "from-the-future",
            )
            persisted = json.loads(settings_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["custom_prompt"], "written-now")
            self.assertEqual(persisted["settings_version"], SETTINGS_SCHEMA_VERSION)

    def test_recovery_notice_can_be_cleared(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app_data_dir = Path(tmp)
            with _isolated_settings(settings_module, app_data_dir):
                settings_module.record_recovery_event(
                    "settings",
                    stored_version=3,
                    current_version=SETTINGS_SCHEMA_VERSION,
                    backup_path="/somewhere/settings.json",
                )
                stored = settings_module.read_recovery_record()
                self.assertTrue(settings_module.clear_recovery_record())
                cleared = settings_module.read_recovery_record()
                self.assertFalse(settings_module.clear_recovery_record())

            self.assertEqual(stored["settings"]["stored_version"], 3)
            self.assertEqual(cleared, {})

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

    def test_malformed_key_store_is_backed_up_and_recovered_on_save(self) -> None:
        """保存不再被一份读不出来的 keys.json 卡死。

        对照 clear_tm 对坏 TM 库的处理：先把打不开的旧文件备份留痕，再当空表
        继续这次写入——不是「拒绝写入且不给出路」的硬约束违规点了。
        """
        with tempfile.TemporaryDirectory() as tmp:
            app_data_dir = Path(tmp)
            keys_path = app_data_dir / "keys.json"
            keys_path.write_text("[]", encoding="utf-8")

            with patch.multiple(
                settings_module,
                APP_DATA_DIR=app_data_dir,
                KEYS_PATH=keys_path,
                BACKUPS_DIR=app_data_dir / "backups",
                RECOVERY_PATH=app_data_dir / "recovery.json",
            ):
                settings_module.save_key("custom_openai", "new-secret")

                stored = json.loads(keys_path.read_text(encoding="utf-8"))
                self.assertEqual(
                    stored.get(settings_module.api_key_scope("custom_openai", "")),
                    "new-secret",
                )
                backups = list((app_data_dir / "backups" / "keys").glob("keys_unusable_*.json"))
                self.assertEqual(len(backups), 1)
                self.assertEqual(backups[0].read_text(encoding="utf-8"), "[]")

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
