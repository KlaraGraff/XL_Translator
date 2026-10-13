"""运行日志和进度卡只说人话（界面走查 S1 / S2）。

- 运行日志里不出现秒数、拆批数字、原始英文异常；
- 自动识别源语言时，语言预检逐个文件报「正在检测语言 x/y」，进度卡不会停在一个数上不动；
- 四个翻译页面的阶段名同一套（读取文件 / 翻译 / 调整行高 / 生成文件），CAD 和 PDF 也不例外。
"""

from __future__ import annotations

import re
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.cad_task_runner import CadTaskRunner
from core.cad_translation import CadPipelineOptions, CadProgress
from core.file_scanner import FileItem
from core.language_preflight import TranslationLanguageResult
from core.mixed_language import translate_mixed_language_texts
from core.task_runner import LogMsg, ProgressMsg, StatusMsg, TaskRunner
from core.word_document import WordFileItem
from core.word_task_runner import WordTaskRunner
from engines.base_engine import TranslationEngine
from settings import AppSettings
from tests import test_excel_resume as excel_helpers
from tests import test_word_resume as word_helpers
from tests.app_data_isolation import IsolatedAppDataTestCase

CANONICAL_STAGES = {"读取文件", "翻译", "调整行高", "生成文件"}
_TIMING_RE = re.compile(r"\d+\.\d+s")


class _BrokenMixedEngine(TranslationEngine):
    """混合语言结构化调用永远返回解析不了的东西，逼出拆批重试和单条失败两条路径。"""

    @property
    def engine_name(self) -> str:
        return "fake/broken-mixed"

    def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
        return {text: text for text in texts}

    def chat(self, system: str, user: str) -> str:
        raise ValueError("Expecting value: line 1 column 1 (char 0)")


def _preflight(files, _detector, *, target_lang):
    return word_helpers._auto_zh_preflight_stub(files, _detector, target_lang=target_lang)


def _visible_logs(messages) -> list[str]:
    return [m.message for m in messages if isinstance(m, LogMsg) and m.visible]


def _statuses(messages) -> list[str]:
    return [m.phase_desc for m in messages if isinstance(m, StatusMsg)]


def _stage_names(messages) -> set[str]:
    return {m.phase_name for m in messages if isinstance(m, ProgressMsg)}


class MixedLanguageLogTests(unittest.TestCase):
    def test_mixed_language_failures_log_plain_sentences(self) -> None:
        lines: list[str] = []
        translate_mixed_language_texts(
            ["沉降观测 asdfg", "贝特瑞 BTR 项目"],
            engine=_BrokenMixedEngine(),
            target_lang="en",
            system_prompt="base",
            source_lang="zh",
            concurrency=1,
            retry_attempts=1,
            error_callback=lines.append,
        )
        self.assertTrue(lines)
        for line in lines:
            self.assertNotIn("Expecting value", line)
            self.assertNotIn("->", line)
            self.assertNotIn("混合语言", line)


class ExcelWordRunLogTests(IsolatedAppDataTestCase):
    def test_excel_auto_source_reports_language_check_and_hides_timings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = []
            for name in ("a", "b"):
                path = excel_helpers._make_xlsx(root / f"{name}.xlsx", {"A1": "你好", "A2": f"再见{name}"})
                files.append(FileItem(path=path, name=name, size_kb=1.0))
            calls: list[list[str]] = []
            with excel_helpers._pipeline_patches(
                translate_side_effect=excel_helpers._fake_translate(calls),
                translate_with_sources_side_effect=excel_helpers._fake_translate_with_sources([]),
            ), patch("core.task_runner.preflight_files", side_effect=_preflight):
                runner = TaskRunner(files, excel_helpers._settings(source_lang="auto"), source_root=root)
                runner._run()
            messages = list(runner._queue.queue)

        statuses = _statuses(messages)
        self.assertIn("状态：正在检测语言 1/2…", statuses)
        self.assertIn("状态：正在检测语言 2/2…", statuses)
        logs = _visible_logs(messages)
        self.assertTrue(logs)
        for line in logs:
            self.assertIsNone(_TIMING_RE.search(line), line)
            self.assertNotIn("[阶段", line)
        self.assertLessEqual(_stage_names(messages), CANONICAL_STAGES)

    def test_excel_echoed_translations_are_counted_in_the_log(self) -> None:
        # 反馈 FB-20261013-UVW95F：自动识别源语言时，模型原样照抄回来的格子标了
        # 需复核，日志里却一句不提，复核数对不上失败数。
        def _echo_one(texts, engine, target_lang, *args, **kwargs):
            return {
                text: TranslationLanguageResult(
                    text, text if text == "Total" else "Bonjour",
                    source_lang="en", target_lang=target_lang,
                )
                for text in texts
            }

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = excel_helpers._make_xlsx(root / "a.xlsx", {"A1": "你好", "A2": "Total"})
            with excel_helpers._pipeline_patches(
                translate_side_effect=excel_helpers._fake_translate([]),
                translate_with_sources_side_effect=_echo_one,
            ), patch("core.task_runner.preflight_files", side_effect=_preflight):
                runner = TaskRunner(
                    [FileItem(path=path, name="a", size_kb=1.0)],
                    excel_helpers._settings(source_lang="auto", target_lang="fr"),
                    source_root=root,
                )
                runner._run()
            logs = _visible_logs(list(runner._queue.queue))

        self.assertIn("1 处模型返回的译文与原文相同，已标记需复核。", logs)

    def test_word_auto_source_reports_language_check_and_hides_timings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            items = []
            for name in ("a", "b"):
                path = root / f"{name}.docx"
                word_helpers.WordResumeTests._make_docx(path, ["项目名称", f"第{name}段"])
                items.append(WordFileItem(path=path, name=path.name, size_kb=1.0))
            runner = WordTaskRunner(
                items, AppSettings(source_lang="auto", target_lang="en"), source_root=root
            )
            with ExitStack() as stack:
                word_helpers.WordResumeTests._runner_patches(self, stack, root=root)
                stack.enter_context(patch("core.word_task_runner.preflight_files", side_effect=_preflight))
                runner._run()
            messages = list(runner._queue.queue)

        statuses = _statuses(messages)
        self.assertIn("状态：正在检测语言 1/2…", statuses)
        self.assertIn("状态：正在检测语言 2/2…", statuses)
        logs = _visible_logs(messages)
        self.assertTrue(logs)
        for line in logs:
            self.assertIsNone(_TIMING_RE.search(line), line)
            self.assertNotIn("[阶段", line)
        self.assertLessEqual(_stage_names(messages), CANONICAL_STAGES)


class CadStageNameTests(unittest.TestCase):
    def test_cad_progress_uses_shared_stage_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            items = []
            for name in ("a.dxf", "b.dxf"):
                path = root / name
                path.write_text("0\nTEXT\n1\n门\n", encoding="utf-8")
                items.append(SimpleNamespace(path=path, name=name, format="dxf"))
            runner = CadTaskRunner(
                files=items,
                source_root=root,
                output_dir=root / "results",
                converter=SimpleNamespace(),
                translator=lambda texts, glossary: {},
                options=CadPipelineOptions(),
            )
            for stage in ("scan", "match", "translate", "write", "verify"):
                runner._progress(CadProgress(stage, 1, 2, 50.0, ""), file_path=items[1].path)
            names = [m.phase_name for m in list(runner._queue.queue) if isinstance(m, ProgressMsg)]

        self.assertEqual(len(names), 5)
        # 「阶段名 · 具体一步：第 i / N 个文件」——界面取「 · 」前面的部分当阶段标签。
        self.assertEqual(
            [name.split(" · ")[0] for name in names],
            ["读取文件", "翻译", "翻译", "生成文件", "生成文件"],
        )
        self.assertTrue(all(name.endswith("第 2 / 2 个文件") for name in names), names)


class PdfStageNameTests(unittest.TestCase):
    def test_pdf_runner_reports_only_shared_stage_names(self) -> None:
        source = Path(__file__).resolve().parent.parent / "core" / "pdf_image_translation.py"
        text = source.read_text(encoding="utf-8")
        names = set(re.findall(r'ProgressMsg\(\s*\d+,\s*\d+,\s*"([^"]+)"', text))
        self.assertTrue(names)
        self.assertLessEqual(names, CANONICAL_STAGES)


if __name__ == "__main__":
    unittest.main(verbosity=2)
