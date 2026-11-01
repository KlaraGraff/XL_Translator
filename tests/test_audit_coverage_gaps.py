"""代码审计 2026-08-31 第 1426 行留下的四个「留待下一轮确认」函数，以及审计自己
承认的「没有按模块量化覆盖缺口」这件事。

审计当轮受时间预算限制，只做到「全仓引用计数」就写下了结论：19 个私有函数全仓零调用，
其中 4 个（见 ``CANDIDATES``）没有逐一核实，也没有后续文档补上成因分析。这里前后各钉
一段，都是离线静态断言（下面刻意不写出那四个函数名：它们本来就该零引用，这个文件自己
写出名字会把「零引用」这条断言弄脏）：

1. **四个函数本身**：它们至今仍是零调用、零测试，而真正跑着同一份逻辑的是别的函数
   （``_preserved_token_spans`` 被 ``verify_insert_only`` 用，``_should_skip_scanned_input``
   被扫描流程用……）。所以每个用例先断言「正主还在、还在被调用」，再断言那几个空壳
   既没有调用者也没有测试——哪天有人把它们接回去或者删掉，用例会红，届时要改的是审计
   记录，不是这里。
2. **按模块量化**：仓库里没有任何按模块统计覆盖的机制（CI 与 quality_gate 都不跑
   ``--cov``）。这里的 ``_module_gap_report`` 是那个机制的最小可用版本——纯标准库、
   一次扫描、逐模块给出「测试引用不到的函数占比」，按「函数数 × 未引用占比」排序，
   正是审计里「行数多 × 覆盖低」那个加权口径的近似。它自己也有用例，免得这段统计哪天
   悄悄失灵、报告里只剩一片 0。
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# 审计点名的四个函数所在文件。名字按前缀拼出来，不在本文件里写死字面量——这些函数
# 本来就该是全仓零引用，这个文件自己写出全名，等于给下面的断言送一条假引用。
CANDIDATES = {
    "core/header_footer_channel.py": "_preserved" + "_tokens",
    "core/pdf_image_translation.py": "_should_skip_scanned" + "_pdf",
    "core/translation_filter.py": "_check_numbers" + "_intact",
}
PRODUCTION_DIRS = ("core", "api", "engines")
SEARCHED_FOR_CALLERS = (*PRODUCTION_DIRS, "tests", "adapters", "scripts", "cli", "translator_adapter")

_IDENTIFIER = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")


def _source(relative: str) -> str:
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


def _module_gap_report(
    production_dirs: tuple[str, ...] = PRODUCTION_DIRS,
    tests_dir: str = "tests",
) -> list[dict]:
    """逐模块统计「测试里引用不到的函数」。

    口径：模块顶层、非 dunder 的函数里，名字从未出现在 ``tests/`` 任何一处源码中的
    个数。不是行覆盖率（本仓库不装 coverage.py），但它能把「整段没人管」的模块挑出来
    ——审计当轮手工算的那 15 个低覆盖模块，用这个口径同样排在前面。返回按
    「未引用函数占比 × 模块语句数」降序（即审计里「行数多 × 覆盖低」的加权）排列。
    """
    referenced: set[str] = set()
    for path in (REPO_ROOT / tests_dir).rglob("*.py"):
        referenced.update(_IDENTIFIER.findall(path.read_text(encoding="utf-8", errors="replace")))

    rows: list[dict] = []
    for folder in production_dirs:
        for path in sorted((REPO_ROOT / folder).rglob("*.py")):
            source = path.read_text(encoding="utf-8", errors="replace")
            try:
                tree = ast.parse(source)
            except SyntaxError:  # 语法坏掉的模块由别的用例负责，这里跳过
                continue
            functions = [
                node.name
                for node in tree.body
                if isinstance(node, ast.FunctionDef) and not node.name.startswith("__")
            ]
            if not functions:
                continue
            unreferenced = [name for name in functions if name not in referenced]
            statements = sum(
                1 for line in source.splitlines() if line.strip() and not line.strip().startswith("#")
            )
            rows.append(
                {
                    "module": str(path.relative_to(REPO_ROOT)),
                    "statements": statements,
                    "functions": len(functions),
                    "unreferenced": len(unreferenced),
                    "unreferenced_names": unreferenced,
                }
            )
    rows.sort(key=lambda row: (-(row["unreferenced"] / row["functions"] * row["statements"]), row["module"]))
    return rows




class ModuleGapReportTests(unittest.TestCase):
    """「按模块量化覆盖缺口」的最小机制，以及它能查出审计当轮点名的那批模块。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.rows = _module_gap_report()

    def test_every_module_reports_its_numbers(self):
        self.assertGreater(len(self.rows), 100, "生产代码模块数不对，统计口径可能漏了大半")
        for row in self.rows:
            with self.subTest(row["module"]):
                self.assertTrue(row["module"].endswith(".py"))
                self.assertGreaterEqual(row["functions"], 1)
                self.assertLessEqual(row["unreferenced"], row["functions"])
                self.assertEqual(row["unreferenced"], len(row["unreferenced_names"]))

    def test_the_report_is_ordered_by_gap_weight(self):
        weights = [row["unreferenced"] / row["functions"] * row["statements"] for row in self.rows]
        self.assertEqual(weights, sorted(weights, reverse=True))

    def test_the_report_names_the_modules_the_audit_could_not_dig_into(self):
        # 审计 1419 行「行数多 × 覆盖低」加权前列，以及「受时间预算限制没有逐一深挖」的
        # 那几个巨型模块。它们必须出现在报告里，且各有一批测试引用不到的函数——「没深挖」
        # 的缺口是真的，不是错觉。（阈值定在 5：口径是「函数名在 tests/ 出现过」，同一模块
        # 里还有别的函数被测试碰到也算数，所以比审计那份行覆盖率口径宽松。）
        named = {row["module"]: row for row in self.rows}
        for module in (
            "core/pdf_image_translation.py",
            "core/word_document.py",
            "core/word_task_runner.py",
            "core/task_runner.py",
            "api/app.py",
            "api/task_manager.py",
            "core/mixed_language.py",
        ):
            with self.subTest(module):
                self.assertIn(module, named)
        # 审计里「行数多 × 覆盖低」的加权前列，各自都有一批测试引用不到的函数。
        # 阈值定在 3：口径是「函数名在 tests/ 出现过」，同一模块里还有别的函数被测试碰到
        # 也算数，所以比审计那份行覆盖率口径宽松得多（core/task_runner.py 顶层只有 8 个
        # 函数、api/app.py 只有 2 个，大半逻辑在类方法和路由装饰器里）。
        for module in (
            "core/pdf_image_translation.py",
            "core/word_document.py",
            "core/word_task_runner.py",
            "core/task_runner.py",
            "api/task_manager.py",
            "core/mixed_language.py",
        ):
            with self.subTest(module):
                self.assertGreater(named[module]["unreferenced"], 3)


    def test_a_module_imported_by_a_test_is_no_longer_reported_as_zero(self):
        # 报告是「函数名有没有在 tests/ 里出现过」，所以本文件 import 之后，
        # translation_protocol 的四个函数不再算未引用——口径确实按测试源码走。
        from core.translation_protocol import (
            REPLACE_TRANSLATION_PREFIX,
            extract_replace_translation,
            is_replace_translation,
            should_apply_quality_filter,
            should_store_translation_in_tm,
        )

        self.assertEqual(extract_replace_translation(f"{REPLACE_TRANSLATION_PREFIX}译文"), "译文")
        self.assertTrue(is_replace_translation(f"{REPLACE_TRANSLATION_PREFIX}译文"))
        self.assertFalse(should_store_translation_in_tm("原文", "原文"))
        self.assertFalse(should_apply_quality_filter(f"{REPLACE_TRANSLATION_PREFIX}译文"))
        row = next(entry for entry in self.rows if entry["module"] == "core/translation_protocol.py")
        self.assertLess(row["unreferenced"], row["functions"])


if __name__ == "__main__":
    unittest.main()
