from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from api.task_manager import TranslationTaskManager
from core.model_api_identity import TaskApiContext
from settings import AppSettings


class _FinishedRunner:
    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def needs_poll(self) -> bool:
        return False

    def get_message(self, timeout: float = 0.05):  # noqa: ARG002
        return None


class ApiTaskSelectionTests(unittest.TestCase):
    def test_selected_paths_limit_files_passed_to_runner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.xlsx"
            second = root / "second.xlsx"
            first.touch()
            second.touch()
            captured: dict[str, object] = {}
            manager = TranslationTaskManager(settings_loader=AppSettings)
            manager._scan = lambda *_args: [
                SimpleNamespace(path=first),
                SimpleNamespace(path=second),
            ]
            manager._build_runner = lambda **kwargs: (
                captured.update(kwargs) or _FinishedRunner()
            )
            context = TaskApiContext(frozenset(), {})

            with patch("api.task_manager.task_api_context_for_page", return_value=context):
                manager.start_task(
                    surface="excel",
                    source_path=str(root),
                    selected_paths=[str(second)],
                )

        selected = captured["files"]
        self.assertEqual([item.path for item in selected], [second])


if __name__ == "__main__":
    unittest.main(verbosity=2)
