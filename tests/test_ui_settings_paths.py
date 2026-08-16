"""界面写设置用的点路径，必须在 AppSettings 上真的解析得出来。

PUT /api/settings 收到未知键是静默丢弃的（AppSettings 是 extra="ignore"，这条
不能改成 forbid——旧版本的 settings.json 带着已删字段，forbid 会让老配置直接
读不进来）。静默丢弃的后果是：界面上开关照点、绿色提示照出、值落不了盘，用户
只有下次打开软件发现设置没生效才知道。

所以守在这一头：把界面里写死的设置路径扫出来，逐条断言 AppSettings 上解析得
到。它挡的是「Python 侧改了字段名 / 新加设置时 TS 那边拼错一个字母」。

盖不住的部分说清楚：动态拼出来的键（例如 last_${surface}_source_folder）扫不
到，那部分仍然靠人。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from settings import AppSettings

UI_VIEWS_DIR = Path(__file__).resolve().parents[1] / "ui" / "src" / "views"

# saveSettingPath("a.b") / nestedPatch("a.b")——第一个实参是字符串字面量的那些。
# 反引号（模板串）故意不匹配：那是动态拼的，静态扫不出真实的键。
_DIRECT_CALL = re.compile(r"""(?:saveSettingPath|nestedPatch)\(\s*(["'])(.+?)\1""")

# 工作区开关表：{ ..., pathKind: "flat", path: "excel_review.mark_review_items" }
_TOGGLE_ENTRY = re.compile(
    r"""pathKind:\s*(["'])(flat|output)\1\s*,\s*path:\s*(["'])(.+?)\3"""
)

_TOGGLE_BLOCK = re.compile(r"""const\s+(EXCEL|WORD|PDF)_TOGGLES\b""")


def _code_lines(text: str) -> list[tuple[int, str]]:
    """带行号的非注释行——文档注释里写着 nestedPatch("a.b.c") 这种示例，要跳过。"""
    return [
        (lineno, line)
        for lineno, line in enumerate(text.splitlines(), 1)
        if not line.lstrip().startswith(("//", "*", "/*"))
    ]


def _output_prefix(surface: str) -> str:
    """对应 workspace.ts 的 outputSettingPathPrefix()。"""
    return f"{surface}_output" if hasattr(AppSettings(), f"{surface}_output") else "output"


def _collect_paths() -> dict[str, list[str]]:
    """{点路径: [出处, ...]}"""
    found: dict[str, list[str]] = {}

    def record(path: str, origin: str) -> None:
        found.setdefault(path, []).append(origin)

    for file_path in sorted(UI_VIEWS_DIR.glob("*.ts")):
        surface = ""
        for lineno, line in _code_lines(file_path.read_text("utf-8")):
            block = _TOGGLE_BLOCK.search(line)
            if block:
                surface = block.group(1).lower()

            origin = f"{file_path.name}:{lineno}"
            for _, path in _DIRECT_CALL.findall(line):
                record(path, origin)
            for _, kind, _, path in _TOGGLE_ENTRY.findall(line):
                if kind == "flat":
                    record(path, origin)
                elif surface:
                    record(f"{_output_prefix(surface)}.{path}", origin)

    return found


def _resolves(path: str) -> bool:
    current: object = AppSettings()
    for part in path.split("."):
        if not hasattr(current, part):
            return False
        current = getattr(current, part)
    return True


class UiSettingPathTests(unittest.TestCase):
    def test_every_hardcoded_setting_path_exists_on_app_settings(self) -> None:
        broken = {
            path: origins
            for path, origins in _collect_paths().items()
            if not _resolves(path)
        }
        self.assertEqual(broken, {}, f"界面在写 AppSettings 上不存在的设置路径：{broken}")

    def test_the_scan_still_finds_something(self) -> None:
        """扫不到东西的测试永远是绿的。

        真实数量会随界面增减，这里只钉住量级：saveSettingPath / nestedPatch 被
        改名或开关表换写法时，这条先红，提醒去修扫描规则而不是以为一切正常。
        """
        paths = _collect_paths()
        self.assertGreaterEqual(len(paths), 15, sorted(paths))
        # 三种形状各要有代表，少了哪种都说明对应的扫描规则失效了。
        self.assertIn("pdf.target_lang", paths)  # 直接调用
        self.assertIn("excel_review.mark_review_items", paths)  # flat 开关
        self.assertIn("excel_output.keep_original_sheets", paths)  # output 开关

    def test_the_output_prefix_targets_still_exist(self) -> None:
        """outputSettingPathPrefix() 的三个落点。"""
        settings = AppSettings()
        for attr in ("output", "excel_output", "word_output", "pdf_output"):
            with self.subTest(attr=attr):
                self.assertTrue(hasattr(settings, attr))


if __name__ == "__main__":
    unittest.main(verbosity=2)
