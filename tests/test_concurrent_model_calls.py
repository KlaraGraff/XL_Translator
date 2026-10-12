"""翻译前的几类独立模型请求要同时发出，不能一个一个等。

- Excel / Word：每个文件的原文语言识别同时进行；「自动识别场景」的通读请求和
  语言识别同时进行；
- PDF：每份文件的文档说明同时请求；
- CAD：后面几张图纸的图纸说明提前、同时请求，翻到那张时直接取用；
- 补译遗留原文：几条同时修。

做法：替身请求在一个 ``threading.Barrier`` 上等齐 N 方才返回。请求如果还是逐个
发出，等不齐就会超时报错（BrokenBarrierError），测试即失败。
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.cad_task_runner import CadTaskRunner
from core.cad_translation import CadPipelineOptions
from core.concurrent_calls import map_concurrently
from core.file_scanner import FileItem
from core.model_throughput import EffectiveModelThroughput
from core.pdf_image_translation import PdfFileItem, PdfImageTranslationRunner
from core.residual_repair import run_repair_ladder
from core.task_runner import StatusMsg, TaskRunner
from core.word_document import WordFileItem
from core.word_task_runner import WordTaskRunner
from settings import AppSettings
from tests import test_excel_resume as excel_helpers
from tests import test_pdf_image_translation as pdf_helpers
from tests import test_word_resume as word_helpers
from tests.app_data_isolation import IsolatedAppDataTestCase

_WAIT = 5.0


def _throughput(concurrency: int) -> EffectiveModelThroughput:
    return EffectiveModelThroughput(profile_key="test", batch_size=20, concurrency=concurrency)


class MapConcurrentlyTests(unittest.TestCase):
    def test_calls_overlap_and_results_keep_input_order(self) -> None:
        barrier = threading.Barrier(3, timeout=_WAIT)
        done: list[tuple[int, int]] = []

        def call(item: int) -> int:
            barrier.wait()
            time.sleep(0.01 * (3 - item))  # 后面的先返回
            return item * 10

        results = map_concurrently(call, [0, 1, 2], max_workers=3, on_done=lambda i, n: done.append((i, n)))
        self.assertEqual(results, [0, 10, 20])
        self.assertEqual([n for _i, n in done], [1, 2, 3])
        self.assertEqual(sorted(i for i, _n in done), [0, 1, 2])

    def test_one_worker_runs_in_order(self) -> None:
        seen: list[int] = []
        self.assertEqual(map_concurrently(lambda x: seen.append(x) or x, [3, 1, 2], max_workers=1), [3, 1, 2])
        self.assertEqual(seen, [3, 1, 2])


class RepairLadderConcurrencyTests(unittest.TestCase):
    def test_units_are_repaired_at_the_same_time(self) -> None:
        barrier = threading.Barrier(2, timeout=_WAIT)
        units = [
            SimpleNamespace(
                source_text=f"本工程{label}号楼采用整体浇筑工艺施工。",
                target_text=f"本工程{label}号楼采用整体浇筑工艺 pour la construction.",
            )
            for label in "一二"
        ]

        def send(_system: str, _user: str) -> str:
            barrier.wait()
            return '{"repaired": "Ce projet utilise un procede de coulage integral pour la construction."}'

        progress: list[tuple[int, int]] = []
        result = run_repair_ladder(
            units,
            target_lang="fr",
            send=send,
            max_workers=2,
            on_progress=lambda done, total: progress.append((done, total)),
        )
        self.assertEqual(list(result.accepted), [unit.source_text for unit in units])
        self.assertEqual(progress, [(1, 2), (2, 2)])
        self.assertEqual(result.remaining, [])


class ExcelWordLanguageCheckTests(IsolatedAppDataTestCase):
    def test_excel_checks_files_and_reads_document_together(self) -> None:
        # 两个文件的语言识别 + 一次通读，三方同时在等。
        barrier = threading.Barrier(3, timeout=_WAIT)

        def preflight(files, detector, *, target_lang):
            barrier.wait()
            return word_helpers._auto_zh_preflight_stub(files, detector, target_lang=target_lang)

        def profile(*_args, **_kwargs) -> str:
            barrier.wait()
            return "工程量清单。"

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = []
            for name in ("a", "b"):
                path = excel_helpers._make_xlsx(root / f"{name}.xlsx", {"A1": "你好", "A2": f"再见{name}"})
                files.append(FileItem(path=path, name=name, size_kb=1.0))
            with excel_helpers._pipeline_patches(
                translate_side_effect=excel_helpers._fake_translate([]),
                translate_with_sources_side_effect=excel_helpers._fake_translate_with_sources([]),
            ), patch("core.task_runner.get_model_throughput", return_value=_throughput(4)), patch(
                "core.task_runner.preflight_files", side_effect=preflight
            ), patch("core.task_runner.request_document_profile", side_effect=profile), patch(
                "core.task_runner.engine_supports_chat", return_value=True
            ):
                runner = TaskRunner(files, excel_helpers._settings(source_lang="auto"), source_root=root)
                runner._run()
            messages = list(runner._queue.queue)

        self.assertFalse(barrier.broken)
        statuses = [m.phase_desc for m in messages if isinstance(m, StatusMsg)]
        self.assertIn("状态：正在检测语言 2/2…", statuses)

    def test_word_checks_files_and_reads_document_together(self) -> None:
        barrier = threading.Barrier(3, timeout=_WAIT)

        def preflight(files, detector, *, target_lang):
            barrier.wait()
            return word_helpers._auto_zh_preflight_stub(files, detector, target_lang=target_lang)

        def profile(*_args, **_kwargs) -> str:
            barrier.wait()
            return "施工方案。"

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            items = []
            for name in ("a", "b"):
                path = root / f"{name}.docx"
                word_helpers.WordResumeTests._make_docx(path, ["项目名称", f"第{name}段"])
                items.append(WordFileItem(path=path, name=path.name, size_kb=1.0))
            runner = WordTaskRunner(items, AppSettings(source_lang="auto", target_lang="en"), source_root=root)
            with ExitStack() as stack:
                word_helpers.WordResumeTests._runner_patches(self, stack, root=root)
                stack.enter_context(patch("core.word_task_runner.get_model_throughput", return_value=_throughput(4)))
                stack.enter_context(patch("core.word_task_runner.preflight_files", side_effect=preflight))
                stack.enter_context(patch("core.word_task_runner.request_document_profile", side_effect=profile))
                stack.enter_context(patch("core.word_task_runner.engine_supports_chat", return_value=True))
                stack.enter_context(
                    patch(
                        "core.word_task_runner.build_engine",
                        return_value=SimpleNamespace(engine_name="resume/mock", chat=lambda *_args: ""),
                    )
                )
                runner._run()
            messages = list(runner._queue.queue)

        self.assertFalse(barrier.broken)
        statuses = [m.phase_desc for m in messages if isinstance(m, StatusMsg)]
        self.assertIn("状态：正在检测语言 2/2…", statuses)


class PdfProfileConcurrencyTests(unittest.TestCase):
    def test_each_pdf_profile_is_requested_together(self) -> None:
        barrier = threading.Barrier(2, timeout=_WAIT)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            items = []
            for name in ("a", "b"):
                source_pdf = root / f"{name}.pdf"
                source_pdf.write_bytes(b"%PDF-1.4\n")
                items.append(PdfFileItem(path=source_pdf, name=name, size_kb=1.0, page_count=1))
            image_client = pdf_helpers._ContextRecordingImageClient(pdf_helpers._png_bytes(1200, 1600))

            def profile(lines: list[str], _log) -> str:
                barrier.wait()
                return f"说明：{lines[0]}"

            runner = PdfImageTranslationRunner(
                items,
                pdf_helpers._page_review_settings(root),
                source_root=root,
                image_client=image_client,
                task_logger_enabled=False,
                profile_requester=profile,
                profile_concurrency=2,
            )
            with patch.dict(
                sys.modules,
                {"pypdfium2": pdf_helpers._fake_pdfium_module_by_page_count({"a.pdf": 1, "b.pdf": 1})},
            ), patch(
                "core.pdf_image_translation._pdf_text_lines", side_effect=lambda path: [Path(path).stem]
            ), patch("core.model_roles.get_key", return_value="secret"):
                runner._run()

        self.assertFalse(barrier.broken)
        self.assertEqual(sorted(image_client.contexts), ["说明：a", "说明：b"])


def _drawing(text: str) -> str:
    return f"0\nSECTION\n2\nENTITIES\n0\nTEXT\n8\n0\n10\n0\n20\n0\n40\n2.5\n1\n{text}\n0\nENDSEC\n0\nEOF\n"


class CadProfilePrefetchTests(unittest.TestCase):
    def test_next_drawing_profile_is_requested_while_the_first_is_read(self) -> None:
        barrier = threading.Barrier(2, timeout=_WAIT)
        requested: list[list[str]] = []
        logs: list[str] = []
        lock = threading.Lock()

        def profile(texts: list[str], log, *, neighbor_rows=()) -> str:
            with lock:
                requested.append(list(texts))
            barrier.wait()
            log("INFO", f"自动识别场景：文档说明：{texts[0]}图")
            return f"{texts[0]}图。"

        contexts: list[str] = []

        def translate(texts, glossary, context=""):
            contexts.append(context)
            return {text: f"{text}-en" for text in texts}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files = []
            for name, text in (("a", "门窗"), ("b", "给排水")):
                source = root / f"{name}.dxf"
                source.write_text(_drawing(text), encoding="utf-8")
                files.append(SimpleNamespace(path=source, name=source.name, format="dxf"))
            with patch("core.process_artifacts.APP_DATA_DIR", root / "app-data"):
                runner = CadTaskRunner(
                    files,
                    source_root=root,
                    output_dir=root / "results",
                    translator=translate,
                    profile_requester=profile,
                    profile_concurrency=2,
                    options=CadPipelineOptions(target_lang="en"),
                )
                runner.start()
                messages = []
                while runner.needs_poll():
                    message = runner.get_message(timeout=1)
                    if message is not None:
                        messages.append(message)

        self.assertFalse(barrier.broken)
        # 第二张图纸用的是提前拿到的说明，没有再请求一次。
        self.assertEqual(sorted(texts[0] for texts in requested), ["给排水", "门窗"])
        self.assertEqual(contexts, ["门窗图。", "给排水图。"])
        logs = [getattr(m, "message", "") for m in messages]
        self.assertTrue(any("文档说明：给排水图" in line for line in logs))


if __name__ == "__main__":
    unittest.main()
