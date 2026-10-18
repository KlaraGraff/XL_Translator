"""「翻译」页「全部开始」：几种类型一起开始、写进同一个输出文件夹、同一批内部不问共用连接。"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from api.task_manager import TaskConflictError, TaskInputError, TaskOptions
from core.file_scanner import GENERATED_OUTPUT_DIR_MARKER
from tests.test_phase7_scheduler_contracts import _context_side_effect, _phase7_manager


def _items(root: Path, *surfaces: str) -> list[tuple[str, str, list[str], TaskOptions]]:
    return [(surface, str(root), [], TaskOptions(target_lang="en")) for surface in surfaces]


class BatchStartTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = TemporaryDirectory()
        self.root = Path(self._temporary.name) / "项目"
        self.root.mkdir()
        self.runners: list[object] = []
        self.manager = _phase7_manager(self.root, runners=self.runners)
        self.built: list[dict] = []
        original = self.manager._build_runner

        def build_runner(**kwargs):
            self.built.append(kwargs)
            return original(**kwargs)

        self.manager._build_runner = build_runner
        patches = (
            patch("api.task_manager.task_api_context_for_page", side_effect=_context_side_effect),
            patch("api.task_manager.tm_manager.init_db"),
            patch("api.task_manager.threading.Thread"),
        )
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.addCleanup(self._temporary.cleanup)

    def test_types_start_together_into_one_output_folder(self) -> None:
        result = self.manager.start_batch(_items(self.root, "excel", "word", "pdf"))

        self.assertFalse(result["requires_confirmation"])
        self.assertEqual([task["surface"] for task in result["tasks"]], ["excel", "word", "pdf"])
        self.assertEqual(result["failed"], [])
        output_dir = Path(result["output_dir"])
        self.assertTrue(output_dir.is_dir())
        self.assertEqual(output_dir.parent, self.root.resolve())
        self.assertIn(GENERATED_OUTPUT_DIR_MARKER, output_dir.name)
        # 每一类的 runner 都拿到同一个文件夹，不再各建各的。
        self.assertEqual({kwargs["options"].batch_output_dir for kwargs in self.built}, {str(output_dir)})

    def test_sharing_a_connection_inside_the_batch_needs_no_confirmation(self) -> None:
        # excel 和 word 在测试里共用一条连接；单独开始时第二个要确认，同一批里不问。
        result = self.manager.start_batch(_items(self.root, "excel", "word"))
        self.assertEqual(len(result["tasks"]), 2)

    def test_sharing_with_a_task_outside_the_batch_asks_first(self) -> None:
        self.manager.start_task(surface="pdf", source_path=str(self.root))

        asked = self.manager.start_batch(_items(self.root, "excel", "word"))
        self.assertTrue(asked["requires_confirmation"])
        self.assertTrue(asked["shared_with"])
        # 问的时候什么都没开始，也没建文件夹。
        self.assertEqual(len(self.built), 1)
        self.assertFalse(any(GENERATED_OUTPUT_DIR_MARKER in p.name for p in self.root.iterdir()))

        confirmed = self.manager.start_batch(_items(self.root, "excel", "word"), allow_shared=True)
        self.assertEqual(len(confirmed["tasks"]), 2)

    def test_a_type_already_translating_stops_the_whole_batch(self) -> None:
        self.manager.start_task(surface="excel", source_path=str(self.root))
        with self.assertRaises(TaskConflictError) as raised:
            self.manager.start_batch(_items(self.root, "excel", "word"))
        self.assertEqual(raised.exception.reason, "surface_busy")

    def test_the_same_type_twice_is_refused(self) -> None:
        with self.assertRaises(TaskInputError):
            self.manager.start_batch(_items(self.root, "excel", "excel"))

    def test_one_type_failing_to_start_does_not_stop_the_others(self) -> None:
        original_scan = self.manager._scan

        def scan(root, surface, options, **kwargs):
            if surface == "word":
                return []
            return original_scan(root, surface, options)

        self.manager._scan = scan
        result = self.manager.start_batch(_items(self.root, "excel", "word"))
        self.assertEqual([task["surface"] for task in result["tasks"]], ["excel"])
        self.assertEqual([item["surface"] for item in result["failed"]], ["word"])
        self.assertTrue(result["failed"][0]["message"])

    def test_a_stopped_member_keeps_the_shared_folder_while_others_still_write(self) -> None:
        result = self.manager.start_batch(_items(self.root, "excel", "word"))
        output_dir = result["output_dir"]
        excel = self.manager._tasks[result["tasks"][0]["task_id"]]
        word = self.manager._tasks[result["tasks"][1]["task_id"]]

        self.manager._finish_if_needed(excel, "stopped", "stopped", {"output_dir": output_dir})
        self.assertTrue(Path(output_dir).is_dir())

        # 最后一个也停了、文件夹里什么都没有：照旧清掉空文件夹。
        self.manager._finish_if_needed(word, "stopped", "stopped", {"output_dir": output_dir})
        self.assertFalse(Path(output_dir).exists())

    def test_a_single_task_refuses_an_arbitrary_batch_folder(self) -> None:
        elsewhere = self.root / "别的文件夹"
        elsewhere.mkdir()
        with self.assertRaises(TaskInputError):
            self.manager.start_task(
                surface="excel",
                source_path=str(self.root),
                options=TaskOptions(batch_output_dir=str(elsewhere)),
            )


if __name__ == "__main__":
    unittest.main()
