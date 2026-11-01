"""#283：「自动识别」认不出原文语言时，不再假定原文是中文。

以前语言预检一门语言都没认出来（没挑出样本、请求失败、回答用不了）时，Excel、
Word 和补译判定都退回默认的中文：日文译中文时只有汉字的格子被当成已是中文跳过，
补译时英文、法文格子因为「不像中文」不再补翻。现在原文语言未知就按未知处理：
明显已是目标语言的才跳过，其余送去翻，由模型认出「已是目标语言」原样保留。
"""

from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

from core.file_scanner import FileItem
from core.language_preflight import TranslationLanguageResult
from core.task_runner import TaskRunner
from core.translation_coverage import looks_like_source_text, resolve_coverage_source_lang
from core.translation_filter import drop_file_texts_ruled_out_after_preflight
from tests.test_audit_task_runner_fixes import _base_patches, _settings

_ROWS = ["安全注意事項", "設計図面", "これは図面です", "Design drawing", "中文说明"]


class _Engine:
    engine_name = "auto-source-unknown/mock"

    def __init__(self, reply) -> None:
        self._reply = reply

    def chat(self, system: str, user: str) -> str:
        if isinstance(self._reply, Exception):
            raise self._reply
        return self._reply


def _texts_sent_for_translation(target_lang: str, preflight_reply) -> list[str]:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        source = root / "source.xlsx"
        workbook = Workbook()
        for row in _ROWS:
            workbook.active.append([row])
        workbook.save(source)
        runner = TaskRunner(
            [FileItem(path=source, name="source", size_kb=1.0)],
            _settings(source_lang="auto", target_lang=target_lang),
            source_root=root,
        )
        sent: list[str] = []

        def fake_translate(misses, *args, **kwargs):
            sent.extend(misses)
            return {
                text: TranslationLanguageResult(text, f"T:{text}", "ja", target_lang, True)
                for text in misses
            }

        with ExitStack() as stack:
            _base_patches(stack, root=root, engine=_Engine(preflight_reply))
            stack.enter_context(patch("core.task_runner.tm_manager.lookup_batch", return_value={}))
            stack.enter_context(
                patch("core.task_runner.tm_manager.insert_auto_entries", return_value=0)
            )
            stack.enter_context(
                patch("core.task_runner.translate_texts_with_sources", side_effect=fake_translate)
            )
            stack.enter_context(patch("core.task_runner.bilingual_writer.write_bilingual_file"))
            runner._run()
        return sorted(sent)


class ExcelUnknownSourceLanguage(unittest.TestCase):
    def test_failed_preflight_sends_han_only_japanese_cells_to_a_chinese_target(self) -> None:
        for label, reply in (("回答用不了", "???"), ("请求失败", RuntimeError("network down"))):
            with self.subTest(label):
                sent = _texts_sent_for_translation("zh", reply)
                self.assertIn("安全注意事項", sent)
                self.assertIn("設計図面", sent)
                self.assertIn("これは図面です", sent)

    def test_detected_japanese_also_keeps_han_only_cells(self) -> None:
        sent = _texts_sent_for_translation("zh", '{"source_langs":["ja"]}')
        self.assertIn("安全注意事項", sent)

    def test_detected_english_drops_existing_chinese_cells_again(self) -> None:
        sent = _texts_sent_for_translation("zh", '{"source_langs":["en"]}')
        self.assertEqual(sent, ["Design drawing", "これは図面です"])


class CoverageUnknownSourceLanguage(unittest.TestCase):
    def test_unknown_source_is_no_longer_resolved_to_chinese(self) -> None:
        for value in ("auto", "", None):
            with self.subTest(value=value):
                self.assertEqual(resolve_coverage_source_lang(value), "auto")
        self.assertEqual(resolve_coverage_source_lang("ja"), "ja")

    def test_untranslated_foreign_text_counts_as_source(self) -> None:
        cases = [
            ("Design drawing", "zh"),
            ("これは図面です", "zh"),
            ("안전 계획", "zh"),
            ("设计图纸", "en"),
            ("これは図面です", "en"),
        ]
        for text, target in cases:
            with self.subTest(text=text, target=target):
                self.assertTrue(looks_like_source_text(text, source_lang="auto", target_lang=target))

    def test_text_already_in_the_target_language_is_not_source(self) -> None:
        cases = [("设计图纸", "zh"), ("Fire safety plan", "en"), ("12.5", "en")]
        for text, target in cases:
            with self.subTest(text=text, target=target):
                self.assertFalse(looks_like_source_text(text, source_lang="auto", target_lang=target))


class SharedRefilterAfterPreflight(unittest.TestCase):
    def test_only_files_with_a_detected_language_are_refiltered(self) -> None:
        paths = [Path("en.docx"), Path("unknown.docx"), Path("ja.docx")]
        file_texts = [{"中文说明", "Design drawing"}, {"中文说明"}, {"安全注意事項"}]
        global_texts = {"中文说明", "Design drawing", "安全注意事項"}
        dropped = drop_file_texts_ruled_out_after_preflight(
            paths,
            file_texts,
            global_texts,
            {"en.docx": ("en",), "unknown.docx": (), "ja.docx": ("ja",)},
            target_lang="zh",
        )
        self.assertEqual(dropped, 1)
        self.assertEqual(file_texts, [{"Design drawing"}, {"中文说明"}, {"安全注意事項"}])
        self.assertEqual(global_texts, {"Design drawing", "中文说明", "安全注意事項"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
