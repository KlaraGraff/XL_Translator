"""根外同名文件的译文各放进来源文件夹名的子文件夹，不互相覆盖（#288）。

maple 2026-11-02 选定：从两个文件夹拖进同名文件时不再拒收，名字撞了的根外文件
放进以它所在文件夹命名的子文件夹；不撞名的照旧放在输出文件夹顶层。
"""

from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path

from docx import Document
from openpyxl import load_workbook

from core.file_progress import FileProgressReporter
from core.file_scanner import FileItem
from core.source_layout import SourceLayout
from core.task_runner import TaskRunner
from core.word_document import WordFileItem
from core.word_task_runner import WordTaskRunner
from settings import AppSettings
from tests import test_excel_resume as excel_helpers
from tests import test_word_resume as word_helpers


class SourceLayoutTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def test_files_under_the_root_keep_their_folders(self) -> None:
        layout = SourceLayout([self.root / "a" / "x.xlsx", self.root / "y.xlsx"], self.root)
        self.assertEqual(layout.relative(self.root / "a" / "x.xlsx"), Path("a/x.xlsx"))
        self.assertEqual(layout.subdir(self.root / "y.xlsx"), Path("."))

    def test_outside_files_without_a_clash_stay_at_the_top(self) -> None:
        main = self.root / "一期"
        other = self.root / "二期" / "报价.xlsx"
        layout = SourceLayout([main / "清单.xlsx", other], main)
        self.assertEqual(layout.relative(other), Path("报价.xlsx"))

    def test_an_outside_file_clashing_with_a_root_file_moves_to_its_folder(self) -> None:
        main = self.root / "一期"
        inside, outside = main / "清单.xlsx", self.root / "二期" / "清单.xlsx"
        layout = SourceLayout([inside, outside], main)
        self.assertEqual(layout.relative(inside), Path("清单.xlsx"))
        self.assertEqual(layout.relative(outside), Path("二期/清单.xlsx"))

    def test_two_outside_files_with_the_same_name_each_get_a_folder(self) -> None:
        main = self.root / "main"
        first = self.root / "a" / "清单.xlsx"
        second = self.root / "b" / "清单.XLSX"
        layout = SourceLayout([main / "other.xlsx", first, second], main)
        self.assertEqual(layout.relative(first), Path("a/清单.xlsx"))
        self.assertEqual(layout.relative(second), Path("b/清单.XLSX"))

    def test_same_folder_names_get_a_number(self) -> None:
        main = self.root / "main"
        first = self.root / "p1" / "图纸" / "清单.xlsx"
        second = self.root / "p2" / "图纸" / "清单.xlsx"
        layout = SourceLayout([main / "清单.xlsx", first, second], main)
        self.assertEqual(
            {layout.relative(first), layout.relative(second)},
            {Path("图纸/清单.xlsx"), Path("图纸 (2)/清单.xlsx")},
        )

    def test_a_subfolder_name_already_used_inside_the_root_is_not_reused(self) -> None:
        main = self.root / "main"
        inside = main / "b" / "清单.xlsx"
        outside_a = self.root / "x" / "b" / "清单.xlsx"
        outside_b = self.root / "y" / "清单.xlsx"
        layout = SourceLayout([inside, main / "清单.xlsx", outside_a, outside_b], main)
        self.assertEqual(layout.relative(inside), Path("b/清单.xlsx"))
        self.assertEqual(layout.relative(outside_a), Path("b (2)/清单.xlsx"))
        self.assertEqual(layout.relative(outside_b), Path("y/清单.xlsx"))

    def test_the_file_list_shows_where_the_translation_goes(self) -> None:
        main = self.root / "一期"
        inside, outside = main / "清单.xlsx", self.root / "二期" / "清单.xlsx"
        items = [FileItem(path=inside, name="清单", size_kb=1), FileItem(path=outside, name="清单", size_kb=1)]
        reporter = FileProgressReporter(queue=None, files=items, source_root=main)
        shown = sorted(entry["relative_path"] for entry in reporter._entries.values())  # noqa: SLF001
        self.assertEqual(shown, sorted(["清单.xlsx", str(Path("二期/清单.xlsx"))]))


class SameNamedFilesEndToEndTests(unittest.TestCase):
    """真跑一遍运行器：两份同名文件各出一份译文，谁也不盖掉谁。"""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def test_excel_writes_two_outputs(self) -> None:
        main = self.root / "一期"
        first = excel_helpers._make_xlsx(main / "清单.xlsx", {"A1": "一期内容"})
        second = excel_helpers._make_xlsx(self.root / "二期" / "清单.xlsx", {"A1": "二期内容"})
        settings = excel_helpers._settings(source_lang="zh", target_lang="en")
        settings.excel_output.output_translation_only = True
        settings.excel_output.keep_original_sheets = False
        with excel_helpers._pipeline_patches(
            translate_side_effect=lambda texts, *a, **k: {
                text: "Phase one" if text == "一期内容" else "Phase two" for text in texts
            }
        ):
            runner = TaskRunner(
                [FileItem(path=first, name="清单", size_kb=1.0), FileItem(path=second, name="清单", size_kb=1.0)],
                settings,
                source_root=main,
            )
            runner._run()
        outputs = [
            path
            for path in self.root.rglob("*.xlsx")
            if path not in {first, second} and "翻译输出" in str(path)
        ]
        self.assertEqual(len(outputs), 2, outputs)
        values = {load_workbook(path).worksheets[0]["A1"].value for path in outputs}
        self.assertEqual(values, {"Phase one", "Phase two"})
        self.assertEqual(sum(1 for path in outputs if path.parent.name == "二期"), 1, outputs)

    def test_word_writes_two_outputs(self) -> None:
        main = self.root / "一期"
        main.mkdir()
        (self.root / "二期").mkdir()
        first, second = main / "说明.docx", self.root / "二期" / "说明.docx"
        word_helpers.WordResumeTests._make_docx(first, ["一期说明"])
        word_helpers.WordResumeTests._make_docx(second, ["二期说明"])
        runner = WordTaskRunner(
            [
                WordFileItem(path=first, name=first.name, size_kb=1.0),
                WordFileItem(path=second, name=second.name, size_kb=1.0),
            ],
            AppSettings(source_lang="zh", target_lang="en"),
            source_root=main,
        )
        with ExitStack() as stack:
            word_helpers.WordResumeTests._runner_patches(self, stack, root=self.root)
            runner._run()
        outputs = sorted((self.root / "out").rglob("*.docx"))
        self.assertEqual(len(outputs), 2, outputs)
        self.assertEqual(sorted(path.parent.name for path in outputs), sorted(["out", "二期"]))
        for path in outputs:
            texts = [p.text for p in Document(str(path)).paragraphs]
            self.assertIn("TM translation", texts)


if __name__ == "__main__":
    unittest.main()
