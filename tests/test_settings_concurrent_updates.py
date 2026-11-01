"""Regressions for the settings read-modify-write lost update and file ACLs.

Around 25 endpoints in ``api/app.py`` do ``load_settings()`` → mutate →
``save_settings()``.  FastAPI runs those synchronous handlers in a thread pool,
so two requests overlap freely.  The file itself was never at risk (the atomic
write and the cross-process lock both work), but the *content* was: the second
writer serialised its whole object, so whatever the first writer had changed
was silently reverted.  ``save_settings`` now replays only the caller's own
edits onto the file as it stands, under the same cross-process lock.

Every test here points ``APP_DATA_DIR`` / ``SETTINGS_PATH`` / ``KEYS_PATH`` at a
temporary directory before touching anything.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import settings as settings_module
from api.app import create_app
from core import tm_manager
from settings import AppSettings, ModelConnection


class IsolatedSettingsFileTestCase(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.app_data_dir = Path(temporary.name) / "app-data"
        self.settings_path = self.app_data_dir / "settings.json"
        for patcher in (
            patch.multiple(
                settings_module,
                APP_DATA_DIR=self.app_data_dir,
                SETTINGS_PATH=self.settings_path,
                KEYS_PATH=self.app_data_dir / "keys.json",
            ),
            patch.object(tm_manager, "DB_PATH", self.app_data_dir / "tm.db"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def persisted(self) -> dict:
        return json.loads(self.settings_path.read_text(encoding="utf-8"))


class SettingsLostUpdateTests(IsolatedSettingsFileTestCase):
    def test_interleaved_read_modify_write_keeps_both_edits(self) -> None:
        """The exact shape of the bug: two handlers overlap, one edit each."""
        settings_module.save_settings(AppSettings())

        first = settings_module.load_settings()
        second = settings_module.load_settings()
        first.appearance.theme = "dark"
        second.spread_tasks_across_connections = True
        settings_module.save_settings(first)
        settings_module.save_settings(second)

        persisted = self.persisted()
        self.assertEqual(persisted["appearance"]["theme"], "dark")
        self.assertTrue(persisted["spread_tasks_across_connections"])

    def test_the_saving_object_is_refreshed_with_what_it_merged(self) -> None:
        """The endpoint echoes its own object back; it must not be stale."""
        settings_module.save_settings(AppSettings())
        first = settings_module.load_settings()
        second = settings_module.load_settings()
        first.custom_prompt = "from first"
        settings_module.save_settings(first)
        second.target_lang = "fr"
        settings_module.save_settings(second)
        self.assertEqual(second.custom_prompt, "from first")
        self.assertEqual(second.target_lang, "fr")

    def test_concurrent_writers_each_keep_their_own_field(self) -> None:
        """Real threads, each editing a different field of the same file."""
        settings_module.save_settings(AppSettings())
        writers = 12
        ready = threading.Barrier(writers)

        def write(index: int) -> None:
            current = settings_module.load_settings()
            ready.wait(timeout=10)
            if index % 3 == 0:
                current.appearance.theme = "dark"
            elif index % 3 == 1:
                current.spread_tasks_across_connections = True
            else:
                current.target_lang = "fr"
            settings_module.save_settings(current)

        with ThreadPoolExecutor(max_workers=writers) as executor:
            list(executor.map(write, range(writers)))

        persisted = self.persisted()
        # Before the fix whichever thread finished last decided all three.
        self.assertEqual(persisted["appearance"]["theme"], "dark")
        self.assertTrue(persisted["spread_tasks_across_connections"])
        self.assertEqual(persisted["target_lang"], "fr")
        AppSettings.model_validate(persisted)

    def test_a_field_can_still_be_reverted_to_its_default(self) -> None:
        """Merging must not make "set it back" impossible."""
        settings_module.save_settings(AppSettings(custom_prompt="something"))
        current = settings_module.load_settings()
        current.custom_prompt = ""
        settings_module.save_settings(current)
        self.assertEqual(self.persisted()["custom_prompt"], "")

    def test_removing_a_nested_key_propagates(self) -> None:
        """A delete inside a dict field must not be read as "no change"."""
        settings_module.save_settings(AppSettings())
        current = settings_module.load_settings()
        current.domain_name_overrides = {"legal": "法务"}
        settings_module.save_settings(current)
        self.assertIn("legal", self.persisted()["domain_name_overrides"])

        current = settings_module.load_settings()
        current.domain_name_overrides = {}
        settings_module.save_settings(current)
        self.assertEqual(self.persisted()["domain_name_overrides"], {})

    def test_an_object_with_no_load_snapshot_is_written_wholesale(self) -> None:
        """A freshly constructed object has nothing to diff, so it wins outright."""
        settings_module.save_settings(AppSettings(custom_prompt="old"))
        settings_module.save_settings(AppSettings(target_lang="de"))
        persisted = self.persisted()
        self.assertEqual(persisted["target_lang"], "de")
        self.assertEqual(persisted["custom_prompt"], "")

    def test_explicit_reset_discards_the_file_instead_of_merging(self) -> None:
        settings_module.save_settings(AppSettings(custom_prompt="stale"))
        fresh = AppSettings()
        settings_module.carry_settings_baseline(settings_module.load_settings(), fresh)
        settings_module.save_settings(fresh, replace_incompatible=True)
        self.assertEqual(self.persisted()["custom_prompt"], "")

    def test_an_unmergeable_result_falls_back_to_a_plain_write(self) -> None:
        """Two independent edits must never leave settings unsavable."""
        settings_module.save_settings(AppSettings())
        current = settings_module.load_settings()
        current.target_lang = "fr"
        with patch.object(
            settings_module,
            "_apply_settings_delta",
            return_value={"target_lang": ["not", "a", "string"]},
        ):
            settings_module.save_settings(current)
        persisted = self.persisted()
        self.assertEqual(persisted["target_lang"], "fr")
        AppSettings.model_validate(persisted)

    def test_carry_settings_baseline_hands_over_the_snapshot(self) -> None:
        settings_module.save_settings(AppSettings(custom_prompt="kept"))
        loaded = settings_module.load_settings()
        rebuilt = AppSettings.model_validate(loaded.model_dump(mode="json"))
        self.assertIsNone(rebuilt._persisted_snapshot)
        settings_module.carry_settings_baseline(loaded, rebuilt)
        self.assertEqual(rebuilt._persisted_snapshot, loaded._persisted_snapshot)


class SettingsEndpointConcurrencyTests(IsolatedSettingsFileTestCase):
    """The lost update as the UI produced it: two switches flipped at once."""

    def test_two_settings_puts_do_not_undo_each_other(self) -> None:
        from fastapi.testclient import TestClient

        client = TestClient(create_app())
        self.assertEqual(client.get("/api/settings").status_code, 200)

        payloads = [
            {"appearance": {"theme": "dark"}},
            {"spread_tasks_across_connections": True},
            {"target_lang": "fr"},
        ]
        ready = threading.Barrier(len(payloads))

        def put(payload: dict) -> int:
            ready.wait(timeout=10)
            return client.put("/api/settings", json=payload).status_code

        with ThreadPoolExecutor(max_workers=len(payloads)) as executor:
            codes = list(executor.map(put, payloads))
        self.assertEqual(codes, [200] * len(payloads))

        final = client.get("/api/settings").json()
        self.assertEqual(final["appearance"]["theme"], "dark")
        self.assertTrue(final["spread_tasks_across_connections"])
        self.assertEqual(final["target_lang"], "fr")


class KeyedListMergeTests(IsolatedSettingsFileTestCase):
    """一条连接池是 list，但两个写入者改的可以是各自不同的那一条。

    ``_settings_delta`` 以前对非 dict 一律记整份 SET，于是「测试连接」这种跨网络
    往返后落盘的请求，会把期间用户对另一条连接的修改整份盖回去。带 id 的列表现在
    按元素合并；顺序变了、元素认不出 id 时仍整份替换。
    """

    def setUp(self) -> None:
        super().setUp()
        settings_module.save_settings(AppSettings())
        loaded = settings_module.load_settings()
        loaded.engine.mode = "cloud"
        loaded.engine.cloud_provider = "custom_openai"
        loaded.engine.cloud_base_url = "https://primary.example/v1"
        loaded.engine.cloud_model = "model-primary"
        settings_module.save_settings(loaded)

    def _connections(self) -> list[dict]:
        return self.persisted()["engine"]["connections"]

    def _append_loaded_connection(self, model: str) -> str:
        """Add one pool entry the way ``add_role_connection`` does, and save."""
        loaded = settings_module.load_settings()
        connection = ModelConnection(
            label=model,
            provider="custom_openai",
            model=model,
            base_url=f"https://{model}.example/v1",
        )
        loaded.engine.connections.append(connection)
        settings_module.save_settings(loaded)
        return connection.id

    def test_two_writers_editing_different_connections_both_win(self) -> None:
        first_id = self._append_loaded_connection("first")
        second_id = self._append_loaded_connection("second")

        dialling = settings_module.load_settings()
        editing = settings_module.load_settings()
        tested = next(
            connection
            for connection in dialling.engine.connections
            if connection.id == second_id
        )
        tested.availability_status = "available"
        tested.availability_message = "连接测试通过。"
        for connection in editing.engine.connections:
            if connection.id == first_id:
                connection.model = "first-edited"
        settings_module.save_settings(dialling)
        settings_module.save_settings(editing)

        by_id = {connection["id"]: connection for connection in self._connections()}
        # 修复前这里落盘的是 "first"：测试结果的整份旧列表把编辑盖了回去。
        self.assertEqual(by_id[first_id]["model"], "first-edited")
        self.assertEqual(by_id[second_id]["availability_status"], "available")

    def test_a_connection_deleted_meanwhile_stays_deleted(self) -> None:
        """另一条连接被删掉后，旧快照里那一条不能借这次保存复活。"""
        first_id = self._append_loaded_connection("first")
        second_id = self._append_loaded_connection("second")

        dialling = settings_module.load_settings()
        removing = settings_module.load_settings()
        tested = next(
            connection
            for connection in dialling.engine.connections
            if connection.id == second_id
        )
        tested.availability_status = "available"
        removing.engine.connections = [
            connection
            for connection in removing.engine.connections
            if connection.id != second_id
        ]
        settings_module.save_settings(removing)
        settings_module.save_settings(dialling)

        ids = [connection["id"] for connection in self._connections()]
        self.assertIn(first_id, ids)
        self.assertNotIn(second_id, ids)

    def test_a_reorder_still_replaces_the_whole_pool(self) -> None:
        """顺序就是语义（第 0 条是主用），所以顺序变了要整份替换。"""
        self._append_loaded_connection("first")
        second_id = self._append_loaded_connection("second")

        dialling = settings_module.load_settings()
        reordering = settings_module.load_settings()
        ordered = [connection.id for connection in reordering.engine.connections]
        reordering.engine.connections = list(reversed(reordering.engine.connections))
        tested = next(
            connection
            for connection in dialling.engine.connections
            if connection.id == second_id
        )
        tested.availability_status = "available"
        settings_module.save_settings(reordering)
        settings_module.save_settings(dialling)

        ids = [connection["id"] for connection in self._connections()]
        self.assertEqual(ids, list(reversed(ordered)))

    def test_a_list_without_ids_still_replaces_wholesale(self) -> None:
        """元素认不出 id 的列表维持原来的整份替换，不能被误当成连接池合并。"""
        first = settings_module.load_settings()
        second = settings_module.load_settings()
        first.recent_tm_lang_pairs = ["en->zh", "ja->zh"]
        second.recent_tm_lang_pairs = ["fr->de"]
        settings_module.save_settings(first)
        settings_module.save_settings(second)
        self.assertEqual(self.persisted()["recent_tm_lang_pairs"], ["fr->de"])


class _WindowsOs:
    """``os`` seen as Windows, for ``settings`` only.

    Patching ``os.name`` itself mutates the one shared module object, which
    also makes ``tempfile`` build Windows paths on a Mac.  This proxy keeps the
    lie local to the module under test.
    """

    name = "nt"

    def __getattr__(self, attribute: str):
        return getattr(os, attribute)


class WindowsFilePermissionTests(unittest.TestCase):
    """``keys.json`` used to be left at whatever the parent directory granted.

    Windows ignores the POSIX mode, so the ``chmod 0600`` was a no-op there.
    ``icacls`` cannot be exercised on this platform, so these tests pin the
    command that is issued and the failure handling rather than the effect.
    """

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "keys.json"
        self.path.write_text("{}", encoding="utf-8")

    def test_no_op_on_posix(self) -> None:
        self.assertEqual(os.name, "posix")
        self.assertFalse(settings_module.restrict_windows_file_to_owner(self.path))

    def test_inheritance_is_dropped_and_only_this_account_is_granted(self) -> None:
        completed = unittest.mock.Mock(returncode=0)
        with (
            patch.object(settings_module, "os", _WindowsOs()),
            patch.dict(
                os.environ,
                {"USERNAME": "tester", "USERDOMAIN": "WORKGROUP"},
                clear=False,
            ),
            patch.object(settings_module.subprocess, "run", return_value=completed) as run,
        ):
            self.assertTrue(settings_module.restrict_windows_file_to_owner(self.path))
        command = run.call_args.args[0]
        self.assertEqual(command[0], "icacls")
        self.assertEqual(command[1], str(self.path))
        self.assertIn("/inheritance:r", command)
        self.assertIn("/grant:r", command)
        self.assertIn("WORKGROUP\\tester:F", command)

    def test_a_failing_icacls_is_reported_not_raised(self) -> None:
        for outcome in (unittest.mock.Mock(returncode=5), OSError("no icacls")):
            with self.subTest(outcome=type(outcome).__name__):
                kwargs = (
                    {"side_effect": outcome}
                    if isinstance(outcome, Exception)
                    else {"return_value": outcome}
                )
                with (
                    patch.object(settings_module, "os", _WindowsOs()),
                    patch.dict(os.environ, {"USERNAME": "tester"}, clear=False),
                    patch.object(settings_module.subprocess, "run", **kwargs),
                ):
                    self.assertFalse(settings_module.restrict_windows_file_to_owner(self.path))

    def test_the_atomic_write_tightens_before_the_rename(self) -> None:
        """The destination must never exist with a looser ACL, even briefly."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        target = Path(temporary.name) / "secrets.json"
        tightened: list[Path] = []
        with (
            patch.object(settings_module, "os", _WindowsOs()),
            patch.object(
                settings_module,
                "restrict_windows_file_to_owner",
                side_effect=lambda path: tightened.append(Path(path)) or True,
            ),
        ):
            settings_module.write_private_text_file(target, "{}")
        self.assertEqual(len(tightened), 1)
        self.assertNotEqual(tightened[0], target)
        self.assertEqual(target.read_text(encoding="utf-8"), "{}")


if __name__ == "__main__":
    unittest.main()
