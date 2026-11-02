"""The PyInstaller spec is the only packing list; every path in it must exist.

代码审计 2026-08-31 的 X1 清单把「打包」列为未验证：`packaging/sidecar/translator_sidecar.spec`
确实在仓库里（也早就在，`scripts/build_tauri_sidecar.py` 和四个测试都在读它），但没人
校验过它引用的数据文件。spec 里写错一个相对路径不会让 PyInstaller 报错——它只会让
那份数据悄悄不进包，而 import 照样成功，只有在用户用到时才暴露（词表缺失时原位替换
把每个字都判成「查不到」、OpenCC 表缺失时繁体直接报错、`adapters/` 少了侧栏就少一页）。

这里用 AST 把 spec 里所有 `ROOT / "…" / "…"` 形式的数据源取出来，逐个断言在仓库里
存在；再钉住 build 脚本与 spec 之间的那三个约定（绝对路径、产物路径、排除项）。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "packaging" / "sidecar" / "translator_sidecar.spec"


def _root_relative(node: ast.AST) -> list[str] | None:
    """`ROOT / "a" / "b"` → ["a", "b"]；不是这个形状就返回 None。"""
    parts: list[str] = []
    cursor = node
    while isinstance(cursor, ast.BinOp) and isinstance(cursor.op, ast.Div):
        right = cursor.right
        if not (isinstance(right, ast.Constant) and isinstance(right.value, str)):
            return None
        parts.append(right.value)
        cursor = cursor.left
    if isinstance(cursor, ast.Name) and cursor.id == "ROOT":
        return list(reversed(parts))
    return None


def _spec_sources() -> list[str]:
    """spec 里所有以 ROOT 为根、用 ``/`` 拼出来的路径（相对仓库根，posix 形式）。"""
    tree = ast.parse(SPEC.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        parts = _root_relative(node)
        if parts:
            found.add("/".join(parts))
    return sorted(found)


class SidecarSpecDataSourceTests(unittest.TestCase):
    def test_spec_exists_where_the_build_script_looks_for_it(self) -> None:
        # scripts/build_tauri_sidecar.py 的 SPEC_PATH 是同一个路径；换地方会让构建直接失败。
        import scripts.build_tauri_sidecar as build_tauri_sidecar

        self.assertTrue(SPEC.is_file())
        self.assertEqual(build_tauri_sidecar.SPEC_PATH, SPEC)

    def test_every_data_source_the_spec_names_exists(self) -> None:
        sources = _spec_sources()
        self.assertTrue(sources, "没有从 spec 里解析出任何数据源，测试本身失效了")
        missing = [source for source in sources if not (ROOT / source).exists()]
        self.assertEqual(missing, [], "spec 引用的数据源在仓库里不存在：" + ", ".join(missing))

    def test_the_local_lexicons_and_opencc_table_are_named_explicitly(self) -> None:
        """这三份表漏收不会报错，只会悄悄失去功能，所以必须逐个写进 datas。"""
        sources = _spec_sources()
        for required in (
            "core/pdf_vector/lexicon/eng.words.txt.gz",
            "core/pdf_vector/lexicon/fra.words.txt.gz",
            "core/zh_hant/opencc_s2twp.txt.gz",
        ):
            self.assertIn(required, sources, f"{required} 没有写进 spec 的 datas")
            self.assertGreater((ROOT / required).stat().st_size, 0, required)

    def test_bundled_adapters_are_collected_by_glob_not_by_name(self) -> None:
        """新加的随应用类型要自动进包；写死名字的话下一份扩展会悄悄缺席。"""
        spec = SPEC.read_text(encoding="utf-8")
        self.assertIn('(ROOT / "adapters").glob("*/adapter.*")', spec)
        # 适配器的 samples/ 只给测试用，不进包。
        self.assertNotIn('"samples"', spec)

    def test_adapter_template_is_data_not_a_python_package(self) -> None:
        """`adapter new` 的模板里两份 .py 之外还有 .github/、.gitignore 等数据文件，
        必须按文件逐个收；改用 collect_submodules 之类的机制会把它们丢掉。"""
        spec = SPEC.read_text(encoding="utf-8")
        self.assertIn('_ADAPTER_TEMPLATE = ROOT / "cli" / "adapter_template"', spec)
        self.assertIn("for template_file in sorted(_ADAPTER_TEMPLATE.rglob(\"*\"))", spec)
        template = ROOT / "cli" / "adapter_template"
        self.assertTrue((template / "adapter.json").is_file())
        self.assertTrue((template / "scripts" / "pack.py").is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
