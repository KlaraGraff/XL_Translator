"""自动识别源语言时，模型报「本来就是目标语言」的格子保持原样、不标需复核。

英译英表里的 Total、Steel plate 这类格子，模型只能原样返回；以前写入端把
「译文 == 原文」一律判成没翻成功，整格涂上复核底色。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook

from core.file_scanner import FileItem
from core.language_preflight import LanguagePreflightResult, TranslationLanguageResult
from core.task_runner import TaskRunner
from tests.test_excel_resume import (
    _fake_translate,
    _make_xlsx,
    _pipeline_patches,
    _run_and_get_done,
    _settings,
)


def _translate_reporting_languages(texts, engine, target_lang, *args, **kwargs):
    results = {}
    for text in texts:
        if text.isascii():
            results[text] = TranslationLanguageResult(
                text, text, source_lang=target_lang, target_lang=target_lang
            )
        else:
            results[text] = TranslationLanguageResult(
                text, f"T:{text}", source_lang="zh", target_lang=target_lang
            )
    return results


class AlreadyTargetLanguageTests(unittest.TestCase):
    def test_cells_already_in_target_language_are_left_unmarked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "proj"
            source = _make_xlsx(proj / "a.xlsx", {"A1": "你好", "A2": "Steel plate"})
            preflight = {
                str(source): LanguagePreflightResult(source_langs=("zh", "en"), requested=True)
            }
            settings = _settings(source_lang="auto")
            with _pipeline_patches(
                translate_side_effect=_fake_translate([]),
                preflight_result=preflight,
                translate_with_sources_side_effect=_translate_reporting_languages,
            ):
                runner = TaskRunner(
                    [FileItem(path=source, name="a", size_kb=1.0)],
                    settings,
                    source_root=proj,
                )
                done = _run_and_get_done(runner)

            result = done.file_results[0]
            self.assertTrue(result.get("success"), result)
            review_cells = {item.get("cell") for item in result.get("review_items") or []}
            self.assertNotIn("A2", review_cells)
            wb = load_workbook(result["output_path"])
            try:
                sheet = wb.worksheets[0]
                self.assertEqual(sheet["A1"].value, "你好\nT:你好")
                self.assertEqual(sheet["A2"].value, "Steel plate")
                self.assertIsNone(sheet["A2"].fill.fgColor.rgb if sheet["A2"].fill.fill_type else None)
            finally:
                wb.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
