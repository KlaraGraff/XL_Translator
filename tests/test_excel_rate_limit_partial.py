"""接口持续限流时，已拿到的译文照常写出，没翻到的保留原文并可续译，而不是整批失败。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook

from core.api_concurrency_control import ApiCredentialRejectedError, ApiKeyTemporarilyUnavailableError
from core.file_scanner import FileItem
from core.task_runner import ErrorMsg, TaskRunner
from tests.test_excel_resume import _make_xlsx, _pipeline_patches, _run_and_get_done, _settings


def _rate_limited_after_first(texts, *args, **kwargs):
    exc = ApiKeyTemporarilyUnavailableError("接口持续限流：已经放慢到最慢档，120 秒内上游一直反馈请求过多。")
    if texts:
        exc.partial_results = {texts[0]: f"T:{texts[0]}"}
    raise exc


def _rate_limited_without_results(texts, *args, **kwargs):
    raise ApiKeyTemporarilyUnavailableError("接口持续限流")


def _key_rejected_after_first(texts, *args, **kwargs):
    exc = ApiCredentialRejectedError("接口拒绝了这条连接的 API Key（HTTP 401）")
    if texts:
        exc.partial_results = {texts[0]: f"T:{texts[0]}"}
    raise exc


class RateLimitPartialTests(unittest.TestCase):
    def test_partial_translations_are_written_and_the_rest_kept_for_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "proj"
            source = _make_xlsx(proj / "a.xlsx", {"A1": "你好", "A2": "再见"})
            with _pipeline_patches(translate_side_effect=_rate_limited_after_first):
                runner = TaskRunner(
                    [FileItem(path=source, name="a", size_kb=1.0)], _settings(), source_root=proj
                )
                done = _run_and_get_done(runner)

            result = done.file_results[0]
            self.assertTrue(result.get("success"), result)
            wb = load_workbook(result["output_path"])
            try:
                values = {wb.active["A1"].value, wb.active["A2"].value}
            finally:
                wb.close()
            translated = [v for v in values if "\nT:" in v]
            self.assertEqual(len(translated), 1)
            self.assertEqual(len(values - set(translated)), 1)
            issue_types = {issue.get("type") for issue in done.issues or []}
            self.assertIn("rate_limited_partial", issue_types)

    def test_rate_limit_before_any_translation_still_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "proj"
            source = _make_xlsx(proj / "a.xlsx", {"A1": "你好"})
            with _pipeline_patches(translate_side_effect=_rate_limited_without_results):
                runner = TaskRunner(
                    [FileItem(path=source, name="a", size_kb=1.0)], _settings(), source_root=proj
                )
                runner._run()
            errors = [m for m in list(runner._queue.queue) if isinstance(m, ErrorMsg)]
            self.assertEqual(len(errors), 1)

    def test_rejected_key_fails_the_task_even_with_partial_results(self) -> None:
        # Key 被拒不是等一等就会好的事：不写只有部分原文的文件，直接失败并说清原因。
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "proj"
            source = _make_xlsx(proj / "a.xlsx", {"A1": "你好", "A2": "再见"})
            with _pipeline_patches(translate_side_effect=_key_rejected_after_first):
                runner = TaskRunner(
                    [FileItem(path=source, name="a", size_kb=1.0)], _settings(), source_root=proj
                )
                runner._run()
            errors = [m for m in list(runner._queue.queue) if isinstance(m, ErrorMsg)]
            self.assertEqual(len(errors), 1)
            self.assertIn("API Key", errors[0].message)
            self.assertFalse(list(proj.rglob("*_双语.xlsx")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
