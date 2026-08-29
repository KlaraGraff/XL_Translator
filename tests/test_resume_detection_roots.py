"""续译检测的扫描根归一 + Excel 自动识别模式的延迟换底稿。

这两处守的是同一条产品承诺：用户点了「接着上次继续」，程序就真的要接上——
不论他扫的是文件夹还是直接多选了几个文件，也不论源语言填的是具体语种还是
「自动识别」（后者是默认值，不接上等于功能对默认配置不存在）。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.bilingual_writer import bilingual_output_name
from core.language_registry import get_target_lang_display
from core.resume_detection import detect_previous_output
from core.task_runner import TaskRunner


class ScanRootNormalizationTests(unittest.TestCase):
    """直接选中文件时，扫描根要归一到它所在的文件夹。

    任务侧就是这么定的（task_manager: ``source if source.is_dir() else
    source.parent``），产物目录也建在那个文件夹里、按文件夹名命名。
    """

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name) / "报价资料"
        self.root.mkdir()
        self.source_a = self.root / "甲表.xlsx"
        self.source_a.write_bytes(b"placeholder")
        self.source_b = self.root / "乙表.xlsx"
        self.source_b.write_bytes(b"placeholder")
        self.out_dir = self.root / "报价资料_翻译输出_20260829_090000"
        self.out_dir.mkdir()
        lang_display = get_target_lang_display("en")
        (self.out_dir / bilingual_output_name("甲表.xlsx", lang_display)).write_bytes(
            b"placeholder"
        )

    def _detect(self, roots: list[Path]) -> dict | None:
        items = [
            SimpleNamespace(path=str(path))
            for path in roots
            if Path(path).is_file()
        ] or [SimpleNamespace(path=str(self.source_a))]
        return detect_previous_output(
            surface="excel",
            items=items,
            scan_roots=roots,
            custom_output_root=None,
            target_lang="en",
            source_lang="zh",
            preferred_dir=None,
        )

    def test_single_file_root_finds_sibling_output_dir(self) -> None:
        result = self._detect([self.source_a])
        self.assertIsNotNone(result, "单选一个文件也要能找到它所在文件夹里的历史产物")
        assert result is not None
        self.assertEqual(result["selected_dir"], str(self.out_dir))
        self.assertEqual(result["files"][0]["status"], "found")
        self.assertEqual(len(result["candidates"]), 1)

    def test_multiple_files_from_same_folder_collapse_to_single_root(self) -> None:
        result = self._detect([self.source_a, self.source_b])
        self.assertIsNotNone(result)
        assert result is not None
        # 同文件夹多选归一成单根：有唯一的 selected_dir，前端才有资格弹「接着上次继续」。
        self.assertEqual(result["selected_dir"], str(self.out_dir))
        by_path = {entry["path"]: entry["status"] for entry in result["files"]}
        self.assertEqual(by_path[str(self.source_a)], "found")
        self.assertEqual(by_path[str(self.source_b)], "none")

    def test_files_from_different_folders_stay_multi_root(self) -> None:
        other_root = Path(self._temp.name) / "另一批"
        other_root.mkdir()
        other_file = other_root / "丙表.xlsx"
        other_file.write_bytes(b"placeholder")
        result = self._detect([self.source_a, other_file])
        self.assertIsNotNone(result)
        assert result is not None
        # 跨文件夹多选定不出唯一底稿目录：selected_dir 必须为空，前端据此不弹续译窗。
        self.assertIsNone(result["selected_dir"])
        self.assertEqual(result["candidates"], [])


class DeferredResumeBaselineTests(unittest.TestCase):
    """自动识别源语言时，底稿在语言预检之后、补译清单重算之前换上。

    _apply_deferred_resume_baselines 现在要求 target_lang / source_lang（换底稿前先
    跑分歧核查 baseline_missing_source_texts）。这里的 source/baseline 都是随手写的
    占位字节文件，不是真 Excel——build_excel_coverage_plan 打不开，核查跑不起来，
    baseline_missing_source_texts 按「无法核查」返回 None，换底稿照常进行，不影响
    这几个用例本来要验证的行为（换底稿 / 失败不换 / 无底稿是空操作）。
    """

    def _runner_with_files(self, files: list[SimpleNamespace]) -> TaskRunner:
        runner = TaskRunner.__new__(TaskRunner)
        runner._files = files
        runner._logs = []
        runner._log = lambda level, message: runner._logs.append((level, message))
        return runner

    def test_deferred_baseline_swaps_process_path_and_marks_usage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "报价.xlsx"
            source.write_bytes(b"src")
            baseline = Path(temp) / "报价_英语_双语.xlsx"
            baseline.write_bytes(b"baseline")
            converted = Path(temp) / "报价_converted.xlsx"
            converted.write_bytes(b"tmp")

            file_item = SimpleNamespace(path=source, name="报价.xlsx")
            runner = self._runner_with_files([file_item])
            process_paths = [converted]
            resume_used = [False]
            runner._apply_deferred_resume_baselines(
                process_paths=process_paths,
                deferred_resume_baselines=[baseline],
                resume_baseline_used=resume_used,
                file_results=[],
                target_lang="en",
                source_lang="zh",
            )
            self.assertEqual(process_paths[0], baseline)
            self.assertTrue(resume_used[0])
            # 阶段 1 的 .xls 转换临时件此后用不上，必须清掉；底稿本身只读不动。
            self.assertFalse(converted.exists())
            self.assertTrue(baseline.exists())

    def test_failed_file_keeps_original_process_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "报价.xlsx"
            source.write_bytes(b"src")
            baseline = Path(temp) / "报价_英语_双语.xlsx"
            baseline.write_bytes(b"baseline")

            file_item = SimpleNamespace(path=source, name="报价.xlsx")
            runner = self._runner_with_files([file_item])
            process_paths = [source]
            resume_used = [False]
            runner._apply_deferred_resume_baselines(
                process_paths=process_paths,
                deferred_resume_baselines=[baseline],
                resume_baseline_used=resume_used,
                file_results=[
                    {"source_path": str(source), "success": False, "error": "读取失败"}
                ],
                target_lang="en",
                source_lang="zh",
            )
            self.assertEqual(process_paths[0], source)
            self.assertFalse(resume_used[0])

    def test_no_deferred_baseline_is_a_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "报价.xlsx"
            source.write_bytes(b"src")
            file_item = SimpleNamespace(path=source, name="报价.xlsx")
            runner = self._runner_with_files([file_item])
            process_paths = [source]
            resume_used = [False]
            runner._apply_deferred_resume_baselines(
                process_paths=process_paths,
                deferred_resume_baselines=[None],
                resume_baseline_used=resume_used,
                file_results=[],
                target_lang="en",
                source_lang="zh",
            )
            self.assertEqual(process_paths[0], source)
            self.assertFalse(resume_used[0])


if __name__ == "__main__":
    unittest.main()
