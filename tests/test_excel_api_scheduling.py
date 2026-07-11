from __future__ import annotations

import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.api_config_check import ApiConfigCheckResult
from core.file_scanner import FileItem
from core.model_throughput import EffectiveModelThroughput
from core.mixed_language import MIXED_ACTION_TRANSLATE, MixedLanguageResult
from core.task_runner import DoneMsg, TaskRunner
from engines.base_engine import TranslationEngine
from settings import AppSettings, EngineSettings


class FakeExcelSchedulingEngine(TranslationEngine):
    @property
    def engine_name(self) -> str:
        return "fake/excel-scheduling"

    def translate_batch(
        self,
        texts: list[str],
        target_lang: str,
        system_prompt: str,
        source_lang: str = "zh",
    ) -> dict[str, str]:
        return {text: f"translated:{text}" for text in texts}


class ExcelApiSchedulingTests(unittest.TestCase):
    def test_normal_and_mixed_api_paths_start_concurrently(self) -> None:
        settings = AppSettings(
            engine=EngineSettings(
                mode="cloud",
                cloud_provider="custom_openai",
                cloud_model="fake-model",
                cloud_base_url="https://example.invalid/v1",
                concurrency=2,
                batch_size=20,
            ),
            target_lang="fr",
            source_lang="zh",
        )
        normal_started = threading.Event()
        mixed_started = threading.Event()
        allow_normal_return = threading.Event()
        calls: list[str] = []
        calls_lock = threading.Lock()

        def fake_translate_texts(*args, **kwargs):
            with calls_lock:
                calls.append("normal_start")
            normal_started.set()
            self.assertTrue(
                mixed_started.wait(timeout=2),
                "mixed API path did not start while normal path was still running",
            )
            allow_normal_return.wait(timeout=2)
            progress_callback = args[6]
            if progress_callback:
                progress_callback(1, 1)
            with calls_lock:
                calls.append("normal_end")
            return {"普通词条": "Traduction normale"}

        def fake_translate_mixed_language_texts(texts, **kwargs):
            with calls_lock:
                calls.append("mixed_start")
            self.assertTrue(
                normal_started.wait(timeout=2),
                "normal API path did not start before mixed path assertion",
            )
            mixed_started.set()
            progress_callback = kwargs.get("progress_callback")
            if progress_callback:
                progress_callback(len(texts), len(texts))
            allow_normal_return.set()
            with calls_lock:
                calls.append("mixed_end")
            return {
                text: MixedLanguageResult(
                    source=text,
                    action=MIXED_ACTION_TRANSLATE,
                    translation="Prix unitaire HT",
                )
                for text in texts
            }

        with (
            patch("core.task_runner.TaskLogger", return_value=MagicMock(task_id="excel-api-scheduling")),
            patch("core.task_runner.check_translation_api_config", return_value=ApiConfigCheckResult(ok=True)),
            patch("core.task_runner.build_engine", return_value=FakeExcelSchedulingEngine()),
            patch("core.task_runner.get_system_prompt", return_value="system"),
            patch(
                "core.task_runner.resolve_effective_model_config",
                return_value=object(),
            ),
            patch(
                "core.task_runner.get_model_throughput",
                return_value=EffectiveModelThroughput(
                    profile_key="test",
                    batch_size=20,
                    concurrency=2,
                ),
            ),
            patch.object(
                TaskRunner,
                "_collect_texts",
                return_value=(["普通词条", "不含税单价/HT"], 1),
            ),
            patch(
                "core.task_runner.split_mixed_language_sources",
                return_value=(["普通词条"], ["不含税单价/HT"]),
            ),
            patch(
                "core.task_runner.tm_manager.lookup_batch",
                return_value={"普通词条": None},
            ),
            patch("core.task_runner.tm_manager.insert_batch", return_value=0),
            patch("core.task_runner.translate_texts", side_effect=fake_translate_texts),
            patch(
                "core.task_runner.translate_mixed_language_texts",
                side_effect=fake_translate_mixed_language_texts,
            ),
            patch("core.task_runner.bilingual_writer.build_output_dir", return_value=Path("/tmp/excel-api-scheduling")),
            patch(
                "core.task_runner.bilingual_writer.write_bilingual_file",
                return_value=Path("/tmp/excel-api-scheduling/out.xlsx"),
            ),
        ):
            runner = TaskRunner(
                [FileItem(path=Path("source.xlsx"), name="source", size_kb=1.0)],
                settings,
                source_root=Path("."),
            )
            runner._run()

        self.assertIn("normal_start", calls)
        self.assertIn("mixed_start", calls)
        self.assertLess(calls.index("mixed_start"), calls.index("normal_end"))

        done_messages = []
        while not runner._queue.empty():
            message = runner._queue.get_nowait()
            if isinstance(message, DoneMsg):
                done_messages.append(message)
        self.assertEqual(len(done_messages), 1)
        self.assertEqual(done_messages[0].api_call_count, 2)
        self.assertEqual(done_messages[0].file_results[0]["success"], True)

    def test_read_failure_does_not_skip_same_named_file_in_other_folder(self) -> None:
        settings = AppSettings(
            engine=EngineSettings(
                mode="cloud",
                cloud_provider="custom_openai",
                cloud_model="fake-model",
                cloud_base_url="https://example.invalid/v1",
                concurrency=1,
                batch_size=20,
            ),
            target_lang="fr",
            source_lang="zh",
        )
        first_path = Path("first") / "source.xlsx"
        second_path = Path("second") / "source.xlsx"

        def collect(real_path, *_args, **_kwargs):
            if real_path == first_path:
                raise ValueError("broken workbook")
            return ["施工内容"], 1

        with (
            patch("core.task_runner.TaskLogger", return_value=MagicMock(task_id="same-name")),
            patch("core.task_runner.check_translation_api_config", return_value=ApiConfigCheckResult(ok=True)),
            patch("core.task_runner.build_engine", return_value=FakeExcelSchedulingEngine()),
            patch("core.task_runner.get_system_prompt", return_value="system"),
            patch("core.task_runner.resolve_effective_model_config", return_value=object()),
            patch(
                "core.task_runner.get_model_throughput",
                return_value=EffectiveModelThroughput(
                    profile_key="test",
                    batch_size=20,
                    concurrency=1,
                ),
            ),
            patch.object(TaskRunner, "_collect_texts", side_effect=collect),
            patch("core.task_runner.tm_manager.lookup_batch", return_value={"施工内容": None}),
            patch("core.task_runner.tm_manager.insert_batch", return_value=0),
            patch("core.task_runner.translate_texts", return_value={"施工内容": "Travaux"}),
            patch("core.task_runner.bilingual_writer.build_output_dir", return_value=Path("/tmp/same-name")),
            patch(
                "core.task_runner.bilingual_writer.write_bilingual_file",
                return_value=Path("/tmp/same-name/out.xlsx"),
            ) as writer,
        ):
            runner = TaskRunner(
                [
                    FileItem(path=first_path, name="source", size_kb=1.0),
                    FileItem(path=second_path, name="source", size_kb=1.0),
                ],
                settings,
                source_root=Path("."),
            )
            runner._run()

        writer.assert_called_once()
        done = [
            message
            for message in list(runner._queue.queue)
            if isinstance(message, DoneMsg)
        ][0]
        by_path = {item["source_path"]: item for item in done.file_results}
        self.assertFalse(by_path[str(first_path)]["success"])
        self.assertTrue(by_path[str(second_path)]["success"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
