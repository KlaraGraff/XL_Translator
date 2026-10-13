"""Isolated retention and discovery tests for the process artifact store."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

import settings as settings_module
from tests.app_data_isolation import IsolatedAppDataTestCase

from core import process_artifacts
from core.process_artifacts import ProcessArtifacts, find_process_dirs, prune_now, retention_overview


class ProcessArtifactStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.app_data = Path(self.tmp.name) / "app-data"
        self.output = Path(self.tmp.name) / "deliverables"
        self.other_output = Path(self.tmp.name) / "other-deliverables"
        self.output.mkdir()
        self.other_output.mkdir()
        self.patch = patch("core.process_artifacts.APP_DATA_DIR", self.app_data)
        self.patch.start()
        # 上限默认取自模块常量；用户设置那一层单独测（见 RetentionSettingsTests）。
        self.settings_patch = patch("core.process_artifacts._configured_retention", return_value={})
        self.settings_patch.start()

    def tearDown(self) -> None:
        self.settings_patch.stop()
        self.patch.stop()
        self.tmp.cleanup()

    def _finished(self, kind: str, status: str, size: int = 0, name: str = "") -> Path:
        run = ProcessArtifacts.create(kind, self.output, name or None)
        if size:
            (run.work_dir / "page.png").write_bytes(b"x" * size)
        run.finish(status)
        return run.work_dir

    def test_paths_are_stable_and_lookup_includes_active_and_finished(self) -> None:
        run = ProcessArtifacts.create("pdf", self.output, "run-1")
        checkpoint = run.work_dir / "checkpoint.json"
        checkpoint.write_text("{}", encoding="utf-8")
        original = run.work_dir
        self.assertEqual(find_process_dirs(self.output, "pdf"), [original])

        run.finish("needs_review")
        self.assertEqual(run.work_dir, original)
        self.assertEqual(find_process_dirs(self.output, "pdf"), [original])

    def test_default_limits(self) -> None:
        self.assertEqual(process_artifacts._retention_rules(), {
            "normal": (20, 1024 * 1024 * 1024),
            "errors": (30, 1024 * 1024 * 1024),
        })

    @patch("core.process_artifacts.NORMAL_RETENTION", 5)
    def test_success_retention_keeps_newest_five_only(self) -> None:
        runs = []
        for index in range(6):
            run = ProcessArtifacts.create("word", self.output, str(index))
            run.finish("succeeded")
            runs.append(run.work_dir)
        found = find_process_dirs(self.output, "word")
        self.assertEqual(len(found), 5)
        self.assertIn(runs[-1], found)
        self.assertNotIn(runs[0], found)

    @patch("core.process_artifacts.RETENTION_BYTES", 2_500_000)
    def test_size_limit_prunes_from_the_oldest(self) -> None:
        oldest_small = self._finished("pdf", "succeeded", 100_000)
        big = self._finished("pdf", "succeeded", 1_000_000)
        middle = self._finished("pdf", "succeeded", 1_000_000)
        newest = self._finished("pdf", "succeeded", 1_000_000)
        # 最近两份约 2 MB，再加一份 1 MB 就超了：它和比它更早的全部清掉，
        # 最早那份小的放得下也不留——「从最早的开始清理」，不跳着留。
        self.assertEqual(find_process_dirs(self.output, "pdf"), [newest, middle])
        self.assertFalse(big.exists())
        self.assertFalse(oldest_small.exists())

    @patch("core.process_artifacts.RETENTION_BYTES", 10)
    def test_newest_run_is_kept_even_over_the_size_limit(self) -> None:
        older = self._finished("cad", "needs_review", 500)
        newest = self._finished("cad", "needs_review", 500)
        self.assertEqual(find_process_dirs(self.output, "cad"), [newest])
        self.assertFalse(older.exists())

    @patch("core.process_artifacts.RETENTION_BYTES", 1_000_000)
    def test_resumable_runs_do_not_count_against_the_limit(self) -> None:
        paused = self._finished("pdf", "paused", 5_000_000)
        first = self._finished("pdf", "failed", 400_000)
        second = self._finished("pdf", "failed", 400_000)
        found = find_process_dirs(self.output, "pdf")
        self.assertEqual(found, [second, first, paused])
        overview = retention_overview()["errors"]
        self.assertEqual(overview["count"], 2)
        self.assertGreaterEqual(overview["size_bytes"], 800_000)
        self.assertLess(overview["size_bytes"], 1_000_000)

    def test_prune_now_applies_a_lowered_limit_immediately(self) -> None:
        runs = [self._finished("excel", "succeeded") for _ in range(4)]
        self.assertEqual(len(find_process_dirs(self.output, "excel")), 4)
        with patch("core.process_artifacts.NORMAL_RETENTION", 2):
            self.assertEqual(prune_now(), 2)
        self.assertEqual(find_process_dirs(self.output, "excel"), [runs[3], runs[2]])

    def test_empty_limits_mean_unlimited(self) -> None:
        unlimited = {"normal": (None, None), "errors": (2, None)}
        with patch("core.process_artifacts._configured_retention", return_value=unlimited):
            kept = [self._finished("word", "succeeded", 1_000_000) for _ in range(4)]
            failed = [self._finished("excel", "failed") for _ in range(3)]
            self.assertEqual(find_process_dirs(self.output, "word"), kept[::-1])
            self.assertEqual(find_process_dirs(self.output, "excel"), [failed[2], failed[1]])
            overview = retention_overview()
        self.assertIsNone(overview["normal"]["max_runs"])
        self.assertIsNone(overview["normal"]["max_bytes"])
        self.assertEqual(overview["errors"]["max_runs"], 2)

    def test_prune_now_without_a_store_is_a_no_op(self) -> None:
        self.assertEqual(prune_now(), 0)
        self.assertFalse((self.app_data / "logs" / "process").exists())

    def test_error_retention_and_active_runs_are_preserved(self) -> None:
        active = ProcessArtifacts.create("excel", self.output, "active")
        failed = []
        for index in range(31):
            run = ProcessArtifacts.create("excel", self.output, str(index))
            run.finish("failed")
            failed.append(run.work_dir)
        found = find_process_dirs(self.output, "excel")
        self.assertIn(active.work_dir, found)
        self.assertEqual(len([path for path in found if path != active.work_dir]), 30)
        self.assertNotIn(failed[0], found)

    def test_other_output_and_symlinked_owned_run_are_protected(self) -> None:
        unrelated = self.app_data / "logs" / "process" / "user-folder"
        unrelated.mkdir(parents=True)
        (unrelated / "keep.txt").write_text("keep", encoding="utf-8")

        old = ProcessArtifacts.create("pdf", self.output, "old")
        old.finish("succeeded")
        link = old.work_dir / "user-link"
        target = self.other_output / "keep.txt"
        target.write_text("keep", encoding="utf-8")
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        for index in range(6):
            run = ProcessArtifacts.create("pdf", self.output, str(index))
            run.finish("succeeded")
        self.assertTrue(unrelated.exists())
        self.assertTrue(target.exists())
        self.assertIn(old.work_dir, find_process_dirs(self.output, "pdf"))

    def test_retention_limits_are_clamped_to_a_safe_minimum(self) -> None:
        with patch("core.process_artifacts.NORMAL_RETENTION", 0), patch(
            "core.process_artifacts.ERROR_RETENTION", -50
        ):
            for index in range(2):
                run = ProcessArtifacts.create("image", self.output, str(index))
                run.finish("succeeded")
            found = find_process_dirs(self.output, "image")
        self.assertEqual(len(found), 1)


class RetentionSettingsTests(IsolatedAppDataTestCase):
    """上限来自用户设置；旧设置文件没有这一节时按默认值补齐。"""

    def test_saved_settings_drive_the_limits(self) -> None:
        settings = settings_module.load_settings()
        self.assertEqual(settings.process_retention.succeeded.max_runs, 20)
        self.assertEqual(settings.process_retention.failed.max_runs, 30)
        self.assertEqual(settings.process_retention.failed.max_mb, 1024)
        settings.process_retention.succeeded.max_runs = 3
        settings.process_retention.failed.max_mb = 200
        settings_module.save_settings(settings)
        self.assertEqual(process_artifacts._retention_rules(), {
            "normal": (3, 1024 * 1024 * 1024),
            "errors": (30, 200 * 1024 * 1024),
        })

    def test_lowering_the_limit_in_settings_prunes_at_once(self) -> None:
        from fastapi.testclient import TestClient

        from api.app import create_app

        output = self.app_data_dir.parent / "deliverables"
        output.mkdir()
        with patch("core.process_artifacts.APP_DATA_DIR", self.app_data_dir):
            runs = []
            for _ in range(4):
                run = ProcessArtifacts.create("cad", output)
                run.finish("succeeded")
                runs.append(run.work_dir)
            client = TestClient(create_app())
            overview = client.get("/api/maintenance/overview").json()["process_retention"]
            self.assertEqual(overview["normal"]["count"], 4)
            self.assertEqual(overview["normal"]["max_runs"], 20)
            response = client.put("/api/settings", json={"process_retention": {"succeeded": {"max_runs": 2}}})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["process_retention"]["succeeded"], {"max_runs": 2, "max_mb": 1024})
            self.assertEqual(find_process_dirs(output, "cad"), [runs[3], runs[2]])
            overview = client.get("/api/maintenance/overview").json()["process_retention"]
            self.assertEqual((overview["normal"]["count"], overview["normal"]["max_runs"]), (2, 2))
            rejected = client.put("/api/settings", json={"process_retention": {"failed": {"max_mb": 1}}})
            self.assertEqual(rejected.status_code, 422)
            # 留空（null）= 不设这一项上限，存得下、读得回。
            cleared = client.put("/api/settings", json={"process_retention": {"succeeded": {"max_runs": None, "max_mb": None}}})
            self.assertEqual(cleared.status_code, 200)
            self.assertEqual(cleared.json()["process_retention"]["succeeded"], {"max_runs": None, "max_mb": None})
            overview = client.get("/api/maintenance/overview").json()["process_retention"]["normal"]
            self.assertEqual((overview["max_runs"], overview["max_bytes"]), (None, None))

    def test_an_older_settings_file_gets_the_defaults(self) -> None:
        settings = settings_module.AppSettings.model_validate({"settings_version": 35})
        self.assertEqual(settings.process_retention.succeeded.model_dump(), {"max_runs": 20, "max_mb": 1024})
        self.assertEqual(settings.process_retention.failed.model_dump(), {"max_runs": 30, "max_mb": 1024})

    def test_out_of_range_values_are_rejected(self) -> None:
        for rule in ({"max_runs": 0}, {"max_runs": 1001}, {"max_mb": 99}, {"max_mb": 100 * 1024 + 1}):
            with self.assertRaises(ValidationError):
                settings_module.ProcessRetentionSettings.model_validate({"succeeded": rule})


if __name__ == "__main__":
    unittest.main()
