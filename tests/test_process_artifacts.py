"""Isolated retention and discovery tests for the process artifact store."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.process_artifacts import ProcessArtifacts, find_process_dirs


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

    def tearDown(self) -> None:
        self.patch.stop()
        self.tmp.cleanup()

    def test_paths_are_stable_and_lookup_includes_active_and_finished(self) -> None:
        run = ProcessArtifacts.create("pdf", self.output, "run-1")
        checkpoint = run.work_dir / "checkpoint.json"
        checkpoint.write_text("{}", encoding="utf-8")
        original = run.work_dir
        self.assertEqual(find_process_dirs(self.output, "pdf"), [original])

        run.finish("needs_review")
        self.assertEqual(run.work_dir, original)
        self.assertEqual(find_process_dirs(self.output, "pdf"), [original])

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


if __name__ == "__main__":
    unittest.main()
