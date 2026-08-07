"""A failing TM write must not destroy an already-paid-for translation run."""

from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from core.language_preflight import TranslationLanguageResult
from core.task_runner import TaskRunner
from settings import AppSettings
from tests.app_data_isolation import IsolatedAppDataTestCase


def _runner() -> TaskRunner:
    return TaskRunner([], AppSettings())


class TaskRunnerTmWriteTests(IsolatedAppDataTestCase):
    def test_a_locked_database_is_reported_instead_of_killing_the_run(self) -> None:
        # TM insertion happens after every API call has been paid for and
        # before the translated files are written; an uncaught OperationalError
        # here used to take the finished translation down with it.
        runner = _runner()
        locked = sqlite3.OperationalError("database is locked")

        with patch("core.task_runner.tm_manager.insert_batch", side_effect=locked):
            written, error = runner.store_api_results_in_tm(
                auto_source_lang=False,
                normal_api_language_results={},
                normal_api_translations={"alpha": "bravo"},
                text_source_scopes={},
                target_lang="fr",
                lang_pair="zh-fr",
                max_len=200,
                engine_name="fake/engine",
            )

        self.assertEqual(written, 0)
        self.assertIn("翻译记忆库写入失败", error or "")
        self.assertIn("database is locked", error or "")

    def test_the_automatic_language_path_degrades_the_same_way(self) -> None:
        runner = _runner()
        locked = sqlite3.OperationalError("database is locked")
        item = TranslationLanguageResult(
            "alpha",
            "bravo",
            source_lang="en",
            target_lang="fr",
        )

        with patch("core.task_runner.tm_manager.insert_auto_entries", side_effect=locked):
            written, error = runner.store_api_results_in_tm(
                auto_source_lang=True,
                normal_api_language_results={"alpha": item},
                normal_api_translations={},
                text_source_scopes={"alpha": [frozenset({"en"})]},
                target_lang="fr",
                lang_pair=None,
                max_len=200,
                engine_name="fake/engine",
            )

        self.assertEqual(written, 0)
        self.assertIn("翻译记忆库写入失败", error or "")

    def test_a_successful_write_reports_no_error(self) -> None:
        runner = _runner()

        with patch("core.task_runner.tm_manager.insert_batch", return_value=1) as insert:
            written, error = runner.store_api_results_in_tm(
                auto_source_lang=False,
                normal_api_language_results={},
                normal_api_translations={"alpha": "bravo"},
                text_source_scopes={},
                target_lang="fr",
                lang_pair="zh-fr",
                max_len=200,
                engine_name="fake/engine",
            )

        self.assertEqual((written, error), (1, None))
        self.assertEqual(insert.call_count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
