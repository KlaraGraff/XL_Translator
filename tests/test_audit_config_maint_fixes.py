"""Regressions for CODE_AUDIT_2026-08-29 cluster A8 (config / maintenance).

Covers four independent findings, one file each:

* 中-1  settings.py / core/maintenance.py — a corrupted ``keys.json`` used to
  make *both* saving a new key and clearing all keys raise / 500, with no
  self-rescue.  ``clear_tm`` already had one for the equivalent TM case; keys
  needed the same treatment.
* 低-API  api/app.py — ``PUT /api/settings`` changing only
  ``engine.cloud_model`` returned 200 but the provider-memory validator
  quietly swapped it back to the last remembered value for that provider.
* 低-编排  api/task_manager.py + core/task_history.py + core/maintenance.py —
  three independent findings bundled by file ownership:
  1. ``_pump_runner``'s two fallback branches used to hand the user a raw
     English internal string or a bare exception class name.
  2. Both ``TaskHistoryStore.clear()`` and ``maintenance._reset_paths()``
     tried to delete a stray ``*.tmp`` file using a name
     (``path.with_suffix(".tmp")``) that never matches the name
     ``_write_locked`` actually writes (a hidden dot prefix + random uuid).
  3. ``core/maintenance.py`` resolved every data-dir-relative path once at
     import time from ``config.APP_DATA_DIR``, while ``TaskHistoryStore``
     resolves its path lazily from ``settings.APP_DATA_DIR`` on every call —
     patching only ``settings.APP_DATA_DIR`` (the common test/isolation
     idiom) left maintenance still pointed at the real path.
* 低-诊断  core/diagnostics.py — a background update-check failure logged a
  fresh diagnostic record on every launch; on an offline machine this filled
  the whole 80-slot history and evicted every real task record, and
  ``_safe_error_code``'s keyword buckets collapsed a specific code like
  ``checksum_invalid`` into the generic ``task_failed``.
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import settings as settings_module
from api.app import create_app
from api.task_manager import ApiTask, TranslationTaskManager
from core import diagnostics, maintenance, tm_manager
from core.task_history import TaskHistoryStore, default_history_path
from fastapi.testclient import TestClient
from settings import AppSettings


# ---------------------------------------------------------------------------
# 中-1: keys.json corruption must not dead-end save or clear.
# ---------------------------------------------------------------------------


class _KeysCorruptionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.app_data = Path(temporary.name) / "app-data"
        self.app_data.mkdir(parents=True)
        self.keys_path = self.app_data / "keys.json"
        self.backups_dir = self.app_data / "backups"
        self._patchers = [
            patch.object(settings_module, "APP_DATA_DIR", self.app_data),
            patch.object(settings_module, "KEYS_PATH", self.keys_path),
            patch.object(settings_module, "BACKUPS_DIR", self.backups_dir),
            patch.object(
                settings_module, "RECOVERY_PATH", self.app_data / "recovery.json"
            ),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        # A corrupted keys.json: valid bytes, invalid JSON.
        self.keys_path.write_text("{not json", encoding="utf-8")


class KeysCorruptionSaveTests(_KeysCorruptionTestCase):
    def test_save_key_recovers_instead_of_raising(self) -> None:
        # Before the fix this raised ValueError("keys.json 无法安全更新：...")
        # straight out of save_key, which api/app.py's put_key surfaced as a
        # bare 500 with nothing the user could do about it.
        settings_module.save_key("openai", "sk-new")

        stored = json.loads(self.keys_path.read_text(encoding="utf-8"))
        self.assertEqual(stored.get("openai"), "sk-new")

    def test_save_key_backs_up_the_unreadable_file_first(self) -> None:
        settings_module.save_key("openai", "sk-new")

        backups = list((self.backups_dir / "keys").glob("keys_unusable_*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), "{not json")

    def test_save_key_records_a_recovery_event_like_settings_and_tm_do(self) -> None:
        settings_module.save_key("openai", "sk-new")

        record = settings_module.read_recovery_record()
        self.assertIn(settings_module.KEYS_RECOVERY_SCOPE, record)
        event = record[settings_module.KEYS_RECOVERY_SCOPE]
        self.assertTrue(event.get("backup_path"))


class KeysCorruptionClearTests(_KeysCorruptionTestCase):
    def test_delete_all_keys_recovers_instead_of_raising(self) -> None:
        # Before the fix this raised inside maintenance.clear_keys(), which
        # api/app.py's /api/maintenance/clear surfaced as a 500 even though
        # the maintenance overview had just reported the category as
        # clearable with a readable count.
        result = maintenance.clear_keys()

        self.assertEqual(result.category, "keys")
        self.assertFalse(self.keys_path.exists())

    def test_delete_all_keys_leaves_a_usable_store_behind(self) -> None:
        maintenance.clear_keys()

        # The self-rescue must not just silence the error: a save right
        # after clearing has to actually work, the same way a fresh install
        # would.
        settings_module.save_key("openai", "sk-after-clear")
        stored = json.loads(self.keys_path.read_text(encoding="utf-8"))
        self.assertEqual(stored.get("openai"), "sk-after-clear")

    def test_delete_all_keys_records_no_recovery_event(self) -> None:
        # W3 mustFix A8-R4: "delete all keys" is the user explicitly giving
        # the file up — there is no loss to announce afterwards. Recording an
        # event here turned a deliberate clear into a warning banner urging
        # the user to re-enter the keys they had just removed on purpose.
        maintenance.clear_keys()

        self.assertNotIn(
            settings_module.KEYS_RECOVERY_SCOPE,
            settings_module.read_recovery_record(),
        )

    def test_delete_all_keys_leaves_no_backup_copy_behind(self) -> None:
        # W3 should-fix: the button promises deletion, not relocation. A
        # backup made here would park a plaintext copy of every key under
        # backups/ that the user just asked to destroy — silently, since the
        # force path records no event pointing at it either.
        maintenance.clear_keys()

        leftovers = (
            [p for p in self.backups_dir.rglob("*") if p.is_file()]
            if self.backups_dir.exists()
            else []
        )
        self.assertEqual(leftovers, [])


# ---------------------------------------------------------------------------
# W3 mustFix A8-R2: "cannot parse" (content corruption, self-heal) must stay
# apart from "cannot read" (OSError: AV/backup software holding the file,
# permission trouble) and from "backup itself failed" — both of the latter
# must still refuse the write with everything on disk untouched, exactly like
# settings.json's unreadable/unusable split.  Only the explicit "delete all
# keys" button (force=True) is allowed to plough through either.
# ---------------------------------------------------------------------------


class KeysUnreadableSaveTests(unittest.TestCase):
    """keys.json exists and is fine, but the OS will not hand it over."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.app_data = Path(temporary.name) / "app-data"
        self.app_data.mkdir(parents=True)
        real_path = self.app_data / "keys.json"
        real_path.write_text('{"openai": "sk-old"}', encoding="utf-8")

        # A Path subclass whose read_text() fails the way Windows AV/backup
        # software holding the file open does — content is intact, the OS
        # just will not serve it right now.
        class _UnreadableKeysPath(type(real_path)):
            def read_text(self, *args: Any, **kwargs: Any) -> str:
                raise PermissionError("locked by antivirus (simulated)")

        self.keys_path = _UnreadableKeysPath(real_path)
        self.backups_dir = self.app_data / "backups"
        self._patchers = [
            patch.object(settings_module, "APP_DATA_DIR", self.app_data),
            patch.object(settings_module, "KEYS_PATH", self.keys_path),
            patch.object(settings_module, "BACKUPS_DIR", self.backups_dir),
            patch.object(
                settings_module, "RECOVERY_PATH", self.app_data / "recovery.json"
            ),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_save_key_refuses_instead_of_discarding_the_other_providers(self) -> None:
        # Before this fix, an OSError here fell into the same self-heal branch
        # as a corrupted file and silently rewrote keys.json down to just the
        # one key being saved — discarding every other provider's key with no
        # backup, because a file that cannot be read cannot be copied either.
        with self.assertRaises(settings_module.SettingsSchemaError):
            settings_module.save_key("anthropic", "sk-new")

        # Original file must be completely untouched: read it back through a
        # plain Path, not the instrumented subclass.
        self.assertEqual(
            Path(str(self.keys_path)).read_text(encoding="utf-8"),
            '{"openai": "sk-old"}',
        )
        self.assertFalse(self.backups_dir.exists())
        self.assertNotIn(
            settings_module.KEYS_RECOVERY_SCOPE,
            settings_module.read_recovery_record(),
        )

    def test_delete_all_keys_still_succeeds_despite_being_unreadable(self) -> None:
        # The explicit "delete all keys" button is the one caller allowed to
        # push through an unreadable file — deleting it outright is the whole
        # point of the button the user pressed.
        result = maintenance.clear_keys()

        self.assertEqual(result.category, "keys")
        self.assertFalse(Path(str(self.keys_path)).exists())


class KeysBackupFailureTests(_KeysCorruptionTestCase):
    """keys.json content is corrupt, but the backup copy itself fails."""

    def test_save_key_refuses_when_backup_fails(self) -> None:
        with patch.object(
            settings_module, "_backup_keys_file", side_effect=OSError("disk full (simulated)")
        ):
            with self.assertRaises(settings_module.SettingsSchemaError):
                settings_module.save_key("openai", "sk-new")

        # A failed backup must not be treated as "backed up well enough to
        # overwrite" — the corrupted original has to survive untouched, and
        # no recovery event gets recorded for a recovery that did not happen.
        self.assertEqual(self.keys_path.read_text(encoding="utf-8"), "{not json")
        self.assertNotIn(
            settings_module.KEYS_RECOVERY_SCOPE,
            settings_module.read_recovery_record(),
        )

    def test_delete_all_keys_still_succeeds_when_backup_fails(self) -> None:
        with patch.object(
            settings_module, "_backup_keys_file", side_effect=OSError("disk full (simulated)")
        ):
            result = maintenance.clear_keys()

        self.assertEqual(result.category, "keys")
        self.assertFalse(self.keys_path.exists())

    def test_delete_all_keys_with_failed_backup_records_no_phantom_backup(self) -> None:
        # W3 mustFix A8-R4, worst half: force + failed backup used to record
        # an event with backup_path="" — data_health() then reported
        # "recreated" and the banner's `|| "备份目录"` fallback told the user
        # a backup exists when not a single byte was saved anywhere.
        with patch.object(
            settings_module, "_backup_keys_file", side_effect=OSError("disk full (simulated)")
        ):
            maintenance.clear_keys()

        self.assertNotIn(
            settings_module.KEYS_RECOVERY_SCOPE,
            settings_module.read_recovery_record(),
        )


# ---------------------------------------------------------------------------
# W3 mustFix A8-R3: the recovery event keys self-heal writes must actually
# reach the same data_health() report settings/tm already surface, or the
# user never learns their other providers' keys were dropped.
# ---------------------------------------------------------------------------


class KeysRecoveryVisibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.app_data = Path(temporary.name) / "app-data"
        self.app_data.mkdir(parents=True)
        self._patchers = [
            patch.object(settings_module, "APP_DATA_DIR", self.app_data),
            patch.object(settings_module, "SETTINGS_PATH", self.app_data / "settings.json"),
            patch.object(
                settings_module, "RECOVERY_PATH", self.app_data / "recovery.json"
            ),
            patch.object(tm_manager, "DB_PATH", self.app_data / "tm.db"),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_data_health_surfaces_a_past_keys_recovery_event(self) -> None:
        # Seed the event directly rather than going through a real corrupted
        # file: this test is about whether data_health() reads it, not about
        # how it got written (that is KeysCorruption*'s job).
        backup_path = str(self.app_data / "backups" / "keys" / "keys_unusable_x.json")
        settings_module.record_recovery_event(
            settings_module.KEYS_RECOVERY_SCOPE,
            stored_version=None,
            current_version=1,
            backup_path=backup_path,
        )

        health = maintenance.data_health()

        self.assertIn("keys", health)
        self.assertEqual(health["keys"]["state"], "recreated")
        self.assertEqual(health["keys"]["backup_path"], backup_path)

    def test_data_health_reports_current_when_no_keys_event_exists(self) -> None:
        health = maintenance.data_health()

        self.assertEqual(health["keys"]["state"], "current")


# ---------------------------------------------------------------------------
# 低-API: PUT /api/settings must persist an explicit engine.cloud_model, not
# let the provider-memory validator silently swap it back.
# ---------------------------------------------------------------------------


class CloudModelPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self._patchers = [
            patch.multiple(
                settings_module,
                APP_DATA_DIR=self.root / "app-data",
                SETTINGS_PATH=self.root / "app-data" / "settings.json",
                KEYS_PATH=self.root / "app-data" / "keys.json",
            ),
            patch.object(tm_manager, "DB_PATH", self.root / "app-data" / "tm.db"),
            patch.object(
                diagnostics, "DIAGNOSTIC_RECORDS_DIR", self.root / "diagnostics"
            ),
            patch.object(
                diagnostics, "LOG_PATH", self.root / "app-data" / "app.log"
            ),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_explicit_cloud_model_survives_provider_memory_override(self) -> None:
        from fastapi.testclient import TestClient

        from api.app import create_app

        client = TestClient(create_app())

        settings = settings_module.load_settings()
        provider = settings.engine.cloud_provider
        # Plant a provider memory entry with a *different* model than the one
        # about to be saved, so the validator has something to swap back to.
        settings_module.set_cloud_provider_config(
            settings.engine, provider, cloud_model="remembered-model"
        )
        settings_module.save_settings(settings)

        response = client.put(
            "/api/settings",
            json={"engine": {"cloud_model": "brand-new-model"}},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json()["engine"]["cloud_model"], "brand-new-model"
        )

        # And it must actually be on disk, not just in the response body.
        persisted = settings_module.load_settings()
        self.assertEqual(persisted.engine.cloud_model, "brand-new-model")
        # The provider memory itself must move too, or switching away and
        # back would resurrect the stale value.
        remembered = settings_module.get_cloud_provider_config(
            persisted.engine, provider
        )
        self.assertEqual(remembered.cloud_model, "brand-new-model")


# ---------------------------------------------------------------------------
# 低-编排 (1/3): _pump_runner must never hand the user a raw English string
# or a bare exception class name.
# ---------------------------------------------------------------------------


class _NoTerminalMessageRunner:
    """Ends without ever posting a terminal message."""

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def needs_poll(self) -> bool:
        return False

    def get_message(self, timeout: float = 0.05):
        return None


class _ExplodingRunner:
    """Raises a bare exception with no message the moment it is polled."""

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def needs_poll(self) -> bool:
        return True

    def get_message(self, timeout: float = 0.05):
        raise RuntimeError()


class _Lease:
    def release(self) -> None:
        return None

    def scheduler_for(self, group):
        return None


def _bare_task(runner: Any) -> ApiTask:
    return ApiTask(
        task_id="t1",
        surface="excel",
        source_path="/tmp/does-not-matter.xlsx",
        source_label="does-not-matter.xlsx",
        runner=runner,
        lease=_Lease(),
        created_at=time.time(),
    )


class PumpRunnerUserFacingMessageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = TranslationTaskManager.__new__(TranslationTaskManager)
        # _pump_runner / _finish_if_needed / _retire_terminal_task only touch
        # a handful of attributes; avoid running the real constructor (which
        # wants a live history store, scheduler, etc.) and provide bare
        # stand-ins for what they need.
        self.manager._history = None
        self.manager._on_task_finished = None

    def _run(self, runner: Any) -> ApiTask:
        task = _bare_task(runner)
        # _pump_runner reaches into a few manager internals through methods;
        # patch out the ones unrelated to message shaping so this stays a
        # narrow test of the fallback text itself.
        with (
            patch.object(TranslationTaskManager, "_append_event", lambda *a, **k: None),
            patch.object(
                TranslationTaskManager, "_retire_terminal_task", lambda *a, **k: None
            ),
        ):
            self.manager._pump_runner(task)
        return task

    def test_missing_terminal_message_gets_a_chinese_fallback(self) -> None:
        task = self._run(_NoTerminalMessageRunner())

        self.assertEqual(task.state, "error")
        message = task.result["message"]
        self.assertNotIn("Translation runner ended", message)
        self.assertTrue(any("一" <= ch <= "鿿" for ch in message))

    def test_bare_exception_gets_a_chinese_fallback_not_its_class_name(self) -> None:
        task = self._run(_ExplodingRunner())

        self.assertEqual(task.state, "error")
        message = task.result["message"]
        self.assertNotEqual(message, "RuntimeError")
        self.assertTrue(any("一" <= ch <= "鿿" for ch in message))


# ---------------------------------------------------------------------------
# 低-编排 (2/3): the *.tmp cleanup path must match the name _write_locked
# actually writes, in both TaskHistoryStore and maintenance's full reset.
# ---------------------------------------------------------------------------


class TaskHistoryTempFileCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.app_data = Path(temporary.name) / "app-data"
        self.app_data.mkdir(parents=True)
        self.patcher = patch.object(settings_module, "APP_DATA_DIR", self.app_data)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

        self.history_path = default_history_path()
        # A stray temp file exactly like the crash _write_locked is meant to
        # guard against: written, never renamed into place.
        self.stray = self.history_path.parent / f".{self.history_path.name}.deadbeef.tmp"
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        self.stray.write_text("[]", encoding="utf-8")
        # The wrong guess the old code made must NOT be what finds it.
        self.wrong_guess = self.history_path.with_suffix(".tmp")

    def test_store_clear_removes_the_real_stray_temp_file(self) -> None:
        self.assertNotEqual(self.stray, self.wrong_guess)
        store = TaskHistoryStore()
        store.clear()
        self.assertFalse(self.stray.exists())

    def test_maintenance_reset_covers_the_real_stray_temp_file(self) -> None:
        # 只断言路径清单包含真实命名的临时文件，绝不真的执行删除：
        # _reset_paths() 里的 SETTINGS_PATH / KEYS_PATH / LOG_PATH 是指向真实
        # 数据目录的模块常量，不随 settings.APP_DATA_DIR 的 patch 走。
        self.assertIn(self.stray, maintenance._reset_paths())
        self.assertNotIn(self.wrong_guess, maintenance._reset_paths())


# ---------------------------------------------------------------------------
# 低-编排 (3/3): maintenance's data-dir paths must track settings.APP_DATA_DIR
# the same way TaskHistoryStore does, not a value frozen at import time.
# ---------------------------------------------------------------------------


class MaintenanceHistoryDirBindingConsistencyTests(unittest.TestCase):
    def test_maintenance_and_history_store_agree_after_patching_only_settings(
        self,
    ) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        app_data = Path(temporary.name) / "app-data"

        # Deliberately patch *only* settings.APP_DATA_DIR — the idiom several
        # existing test fixtures use (e.g. tests/app_data_isolation.py) — and
        # nothing on the maintenance module itself.
        with patch.object(settings_module, "APP_DATA_DIR", app_data):
            store = TaskHistoryStore()
            store.upsert({"task_id": "abc", "state": "done"})

            overview = maintenance.data_overview()
            category = next(
                c for c in overview["categories"] if c["id"] == "task_history"
            )
            self.assertEqual(category["count"], 1)

            result = maintenance.clear_task_history()
            self.assertEqual(result.removed_count, 1)
            self.assertFalse(store._path.exists())


# ---------------------------------------------------------------------------
# 低-诊断: repeated system-diagnostic failures must replace, not stack, and
# the caller-supplied error code must survive verbatim.
# ---------------------------------------------------------------------------


class DiagnosticsFloodAndCodeFoldingTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self._patchers = [
            patch.object(
                diagnostics, "DIAGNOSTIC_RECORDS_DIR", self.root / "diagnostics" / "records"
            ),
            patch.object(diagnostics, "DIAGNOSTICS_DIR", self.root / "diagnostics"),
            patch.object(diagnostics, "LOG_PATH", self.root / "app.log"),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_repeated_background_check_failures_do_not_accumulate(self) -> None:
        for _ in range(5):
            diagnostics.record_system_diagnostic(
                phase="update_check", error_code="network_unreachable"
            )

        records = [
            r for r in diagnostics.list_diagnostic_records() if r.get("surface") == "system"
        ]
        self.assertEqual(len(records), 1)

    def test_repeated_failures_do_not_evict_real_task_records(self) -> None:
        # A handful of genuine task diagnostics that must survive 80 offline
        # launches' worth of the same background failure.
        for i in range(3):
            diagnostics.archive_task_diagnostics(
                surface="excel",
                phase="done",
                task_id=f"task-{i}",
                settings=AppSettings(),
                selected_files=[],
                logs=[],
                status="error",
                error_message="some task failure",
            )
        for _ in range(80):
            diagnostics.record_system_diagnostic(
                phase="update_check", error_code="network_unreachable"
            )

        records = diagnostics.list_diagnostic_records()
        task_records = [r for r in records if r.get("surface") != "system"]
        system_records = [r for r in records if r.get("surface") == "system"]
        self.assertEqual(len(task_records), 3)
        self.assertEqual(len(system_records), 1)

    def test_specific_error_code_is_not_folded_into_a_generic_bucket(self) -> None:
        diagnostics.record_system_diagnostic(
            phase="update_check", error_code="checksum_invalid"
        )

        records = diagnostics.list_diagnostic_records()
        self.assertEqual(len(records), 1)
        # Before the fix this went through _safe_error_code's keyword
        # buckets, none of which recognise "checksum" — it fell through to
        # the generic "task_failed", discarding the one specific detail a
        # support triage needs.
        self.assertEqual(records[0]["error_code"], "checksum_invalid")


# ---------------------------------------------------------------------------
# W3 mustFix A8-R5: dismissing the banner must only clear the scopes whose
# messages actually rendered.  keys 的恢复事件是惰性写入的（只在下一次写 Key
# 时补录），横幅已经在屏时新出现的事件从未展示过——旧的整文件清除会把它
# 连带抹掉，用户永远见不到「keys.json 曾坏过、备份在哪」这条通知。
# ---------------------------------------------------------------------------


class _RecoveryRecordTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.app_data = Path(temporary.name) / "app-data"
        self.app_data.mkdir(parents=True)
        for patcher in (
            patch.multiple(
                settings_module,
                APP_DATA_DIR=self.app_data,
                SETTINGS_PATH=self.app_data / "settings.json",
                KEYS_PATH=self.app_data / "keys.json",
                BACKUPS_DIR=self.app_data / "backups",
                RECOVERY_PATH=self.app_data / "recovery.json",
            ),
            patch.object(tm_manager, "DB_PATH", self.app_data / "tm.db"),
            patch.object(
                diagnostics,
                "DIAGNOSTIC_RECORDS_DIR",
                Path(temporary.name) / "diagnostics",
            ),
            patch.object(diagnostics, "LOG_PATH", self.app_data / "app.log"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _seed(self, *scopes: str) -> None:
        for scope in scopes:
            settings_module.record_recovery_event(
                scope,
                stored_version=None,
                current_version=1,
                backup_path=f"/backups/{scope}",
            )


class ScopedRecoveryDismissTests(_RecoveryRecordTestCase):
    def test_scoped_clear_keeps_the_scope_that_never_rendered(self) -> None:
        self._seed(
            settings_module.SETTINGS_RECOVERY_SCOPE,
            settings_module.KEYS_RECOVERY_SCOPE,
        )

        cleared = settings_module.clear_recovery_record(
            [settings_module.SETTINGS_RECOVERY_SCOPE]
        )

        self.assertTrue(cleared)
        record = settings_module.read_recovery_record()
        self.assertNotIn(settings_module.SETTINGS_RECOVERY_SCOPE, record)
        # 未展示过的 keys 事件必须留到它露面的那天。
        self.assertIn(settings_module.KEYS_RECOVERY_SCOPE, record)

    def test_none_keeps_the_legacy_clear_everything_semantics(self) -> None:
        self._seed(
            settings_module.SETTINGS_RECOVERY_SCOPE,
            settings_module.KEYS_RECOVERY_SCOPE,
        )

        cleared = settings_module.clear_recovery_record(None)

        self.assertTrue(cleared)
        self.assertEqual(settings_module.read_recovery_record(), {})

    def test_empty_scope_list_clears_nothing(self) -> None:
        # 横幅被「阻塞」类消息占据时展示的是阻塞文案，恢复事件一条都没露面，
        # 前端会送 scopes=[] ——此时什么都不能清。
        self._seed(settings_module.KEYS_RECOVERY_SCOPE)

        cleared = settings_module.clear_recovery_record([])

        self.assertFalse(cleared)
        self.assertIn(
            settings_module.KEYS_RECOVERY_SCOPE,
            settings_module.read_recovery_record(),
        )

    def test_scoped_clear_on_missing_file_reports_nothing_removed(self) -> None:
        cleared = settings_module.clear_recovery_record(
            [settings_module.SETTINGS_RECOVERY_SCOPE]
        )

        self.assertFalse(cleared)

    def test_maintenance_passthrough_reports_cleared_flag(self) -> None:
        self._seed(settings_module.TM_RECOVERY_SCOPE)

        result = maintenance.dismiss_recovery_notice(
            [settings_module.TM_RECOVERY_SCOPE]
        )

        self.assertEqual(result, {"cleared": True})
        self.assertEqual(settings_module.read_recovery_record(), {})

    def test_factory_reset_covers_the_backups_dir(self) -> None:
        # 备份目录里躺着损坏文件的原文（keys 是明文 Key）；恢复出厂承诺
        # 「回到首次启动状态」，_reset_paths 必须把它列进去。
        self.assertIn(settings_module.BACKUPS_DIR, maintenance._reset_paths())


class ScopedRecoveryDismissEndpointTests(_RecoveryRecordTestCase):
    """DELETE /api/data/health/notice 的作用域必须原样传到 settings 层。"""

    def setUp(self) -> None:
        super().setUp()
        self.client = TestClient(create_app())

    def test_delete_with_scopes_clears_only_those(self) -> None:
        self._seed(
            settings_module.SETTINGS_RECOVERY_SCOPE,
            settings_module.KEYS_RECOVERY_SCOPE,
        )

        response = self.client.request(
            "DELETE",
            "/api/data/health/notice",
            json={"scopes": [settings_module.SETTINGS_RECOVERY_SCOPE]},
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json().get("cleared"))
        record = settings_module.read_recovery_record()
        self.assertNotIn(settings_module.SETTINGS_RECOVERY_SCOPE, record)
        self.assertIn(settings_module.KEYS_RECOVERY_SCOPE, record)

    def test_delete_without_body_keeps_the_legacy_clear_all(self) -> None:
        # 旧前端（以及手工 curl）不带 body ——语义必须还是整份清除。
        self._seed(
            settings_module.SETTINGS_RECOVERY_SCOPE,
            settings_module.KEYS_RECOVERY_SCOPE,
        )

        response = self.client.request("DELETE", "/api/data/health/notice")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(settings_module.read_recovery_record(), {})


if __name__ == "__main__":
    unittest.main()
