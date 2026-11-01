"""记忆库导出的写出端：多行译文、双引号和导入读回。

CODE_AUDIT_2026-08-31 的 M1「没来得及验」第 1 项：TM 导出（xlsx/csv 写出端）
对多行译文的处理没查，只验到 ``get_all_entries_for_export`` /
``get_full_export`` 这一层的数据是对的。CSV 的格式由界面
（``ui/src/views/library.ts`` 的 ``csvCell`` / ``toTmCsv``）决定，Python 侧只
提供词条数据，所以这里跑一遍 ``tests/tm_export_csv.test.mjs`` 拿到真实界面代码
写出的那份 CSV，再用导入侧的解析器读回来：

* 一条多行译文仍然是一条词条，没被换行拆成好几行；
* 译文里的双引号原样回来，不多不少；
* 文件开头的 BOM 被剥掉，Excel 打开不乱码、导入也不带进第一条原文。

这里断言的是 ``toTmCsv()`` 这一层的输出。真正写盘的是 ``exportTm()``，它在外面
又拼了一个 ``\ufeff``，所以导出的文件带两个 BOM——那是本轮撞见的既有缺陷（本轮
只加测试、不改产品代码，也不把错误行为写成断言），详见 REPORT.md。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from config import SUPPORTED_LANGS
from core import tm_manager
from core.tm_import_parse import parse_import_file
from core.tm_text import normalize_tm_text_for_storage

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests" / "tm_export_csv.test.mjs"

MULTILINE = "第一行\n第二行\n第三行"
QUOTED_SOURCE = '带"引号"的原文'
QUOTED_TARGET = 'He said "ok", then left.'

# 与 tests/tm_export_csv.test.mjs 里喂给 toTmCsv 的词条一致。
ENTRIES = [
    {
        "source_text": "多行单元格",
        "target_text": MULTILINE,
        "word_type": "auto",
        "pinned": 0,
        "updated_at": "2026-10-02 10:00:00",
    },
    {
        "source_text": QUOTED_SOURCE,
        "target_text": QUOTED_TARGET,
        "word_type": "manual",
        "pinned": 1,
        "updated_at": "2026-10-02 10:01:00",
    },
    {
        "source_text": "普通",
        "target_text": "plain",
        "word_type": "auto",
        "pinned": 0,
        "updated_at": "2026-10-02 10:02:00",
    },
]


def _node_binary() -> str | None:
    return shutil.which("node")


def _node_can_strip_types(node: str) -> bool:
    """``module.stripTypeScriptTypes`` 是 Node 22.6+ 的实验接口；老版本跳过。"""
    probe = subprocess.run(
        [node, "-e", "process.exit(typeof require('module').stripTypeScriptTypes === 'function' ? 0 : 1)"],
        capture_output=True,
        timeout=60,
    )
    return probe.returncode == 0


@unittest.skipIf(_node_binary() is None, "本机没有 node，跑不了界面代码的导出测试")
class TmCsvExportTests(unittest.TestCase):
    """界面写出 → 导入读回的往返，用的是真实界面函数。"""

    @classmethod
    def setUpClass(cls) -> None:
        node = _node_binary() or "node"
        if not _node_can_strip_types(node):
            raise unittest.SkipTest("本机 node 太旧，不支持 module.stripTypeScriptTypes")
        result = subprocess.run(
            [node, str(SCRIPT)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
        if result.returncode != 0:
            raise AssertionError(
                "界面导出的 CSV 测试失败：\n" + result.stdout[-4000:] + result.stderr[-2000:]
            )
        # 脚本最后一行 JSON 就是界面真正会写进 .csv 文件的内容。
        cls.exported = json.loads(result.stdout.strip().splitlines()[-1])
        cls.csv_text = cls.exported["csv"]
        cls.exported_entries = cls.exported["entries"]

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self._old_db_path = tm_manager.DB_PATH
        self._old_backups_dir = tm_manager.BACKUPS_DIR
        tm_manager.DB_PATH = self.root / "tm.db"
        tm_manager.BACKUPS_DIR = self.root / "backups"
        self.addCleanup(self._restore_paths)
        tm_manager.init_db()

    def _restore_paths(self) -> None:
        tm_manager.DB_PATH = self._old_db_path
        tm_manager.BACKUPS_DIR = self._old_backups_dir

    def test_multiline_and_quoted_rows_survive_a_round_trip_through_the_importer(self) -> None:
        self.assertEqual(
            [row["target_text"] for row in self.exported_entries],
            [entry["target_text"] for entry in ENTRIES],
            "界面写出的每一行都还是原来那条译文",
        )
        data = self.csv_text.encode("utf-8")
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"), "文件必须是带 BOM 的 UTF-8")
        self.assertEqual(data.count(b"\xef\xbb\xbf"), 1)

        result = parse_import_file("translator-tm-zh-en.csv", data, "zh-en")
        self.assertEqual(result["encoding"], "UTF-8")
        # 导出文件带 word_type / pinned 两列，导入端认得出它们（不是当作多余列丢掉）。
        self.assertEqual(
            result["mapping"], {"source": 0, "target": 1, "word_type": 2, "pinned": 3}
        )
        self.assertEqual(result["has_header"], True)
        rows = result["rows"]
        self.assertEqual(len(rows), len(ENTRIES), "多行译文没有被换行拆成额外的行")
        self.assertEqual([row[0] for row in rows], [entry["source_text"] for entry in ENTRIES])
        self.assertEqual([row[1] for row in rows], [entry["target_text"] for entry in ENTRIES])
        # 表头首列没有被 BOM 咬住，认列才认得出「source_text」。
        self.assertEqual(result["headers"][:2], ["source_text", "target_text"])
        self.assertEqual([row[2] for row in rows], [entry["word_type"] for entry in ENTRIES])

    def test_exported_multiline_translation_reaches_the_library_intact(self) -> None:
        data = self.csv_text.encode("utf-8")
        parsed = parse_import_file("translator-tm-zh-en.csv", data, "zh-en")
        mapping = parsed["mapping"]
        imported = tm_manager.import_entries(
            [
                {
                    "source_text": row[mapping["source"]],
                    "target_text": row[mapping["target"]],
                    "word_type": "manual",
                }
                for row in parsed["rows"]
            ],
            "zh-en",
            "overwrite",
        )
        self.assertEqual(imported["inserted"], len(ENTRIES))

        stored = tm_manager.get_all_entries_for_export("zh-en")
        by_source = {row["source_text"]: row["target_text"] for row in stored}
        # 入库会做「保留换行」的标准化，导出前的译文本来就是标准形态，必须逐字相等。
        for entry in ENTRIES:
            self.assertEqual(by_source[entry["source_text"]], entry["target_text"])
        self.assertEqual(by_source["多行单元格"].count("\n"), 2, "三条换行一条不少")
        self.assertIn('"ok"', by_source[QUOTED_SOURCE])

        # 多行译文在库里仍然是一条，二次查询能原样命中。
        self.assertEqual(
            tm_manager.lookup_batch(["多行单元格"], "zh-en")["多行单元格"], MULTILINE
        )
        # 反向的语言对是它自己的问题，这里只确认没被顺便写进去。
        self.assertIsNone(tm_manager.lookup_batch(["多行单元格"], "en-zh")["多行单元格"])

    def test_export_normalizes_only_what_storage_does(self) -> None:
        """导出的译文是库里的存储形态，读回时不再被改写。"""
        tm_manager.insert_batch([("多行", MULTILINE)], "zh-en", 500, "test")
        exported = tm_manager.get_all_entries_for_export("zh-en")
        self.assertEqual(len(exported), 1)
        value = exported[0]["target_text"]
        self.assertEqual(value, normalize_tm_text_for_storage(MULTILINE))
        self.assertEqual(value.count("\n"), 2)

        parsed = parse_import_file(
            "translator-tm-zh-en.csv",
            self.csv_text.encode("utf-8"),
            "zh-en",
        )
        multiline = next(row for row in parsed["rows"] if row[0] == "多行单元格")
        self.assertEqual(multiline[1], normalize_tm_text_for_storage(MULTILINE))


class TmExportPayloadTests(unittest.TestCase):
    """导出接口给出的字段与界面表格的列名一一对上。"""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self._old_db_path = tm_manager.DB_PATH
        self._old_backups_dir = tm_manager.BACKUPS_DIR
        tm_manager.DB_PATH = self.root / "tm.db"
        tm_manager.BACKUPS_DIR = self.root / "backups"
        self.addCleanup(self._restore_paths)
        tm_manager.init_db()

    def _restore_paths(self) -> None:
        tm_manager.DB_PATH = self._old_db_path
        tm_manager.BACKUPS_DIR = self._old_backups_dir

    def test_csv_columns_are_all_present_on_every_exported_row(self) -> None:
        # 界面的 toTmCsv 按固定表头取字段，少一个就整列变成空串。
        headers = ["source_text", "target_text", "word_type", "pinned", "updated_at"]
        tm_manager.insert_manual_entry("带引号\"的\"原文", "He said \"ok\"", "zh-en", sync_reverse=False)
        tm_manager.insert_batch([("多行", MULTILINE)], "zh-en", 500, "test")
        rows = tm_manager.get_all_entries_for_export("zh-en")
        self.assertEqual(len(rows), 2)
        for row in rows:
            for header in headers:
                self.assertIn(header, row, f"导出行缺少 CSV 列 {header}")
                self.assertIsNotNone(row[header], f"{header} 不应是 None")
        multiline = next(row for row in rows if row["source_text"] == "多行")
        self.assertEqual(multiline["target_text"], MULTILINE)

    def test_full_backup_keeps_multiline_text_unchanged(self) -> None:
        tm_manager.insert_batch([("多行", MULTILINE)], "zh-en", 500, "test")
        backup = tm_manager.get_full_export()
        exported = next(row for row in backup["entries"] if row["source_text"] == "多行")
        self.assertEqual(exported["target_text"], MULTILINE)
        # 走一遍 JSON（备份就是 JSON 文件）也不能把换行吃掉。
        restored = json.loads(json.dumps(backup, ensure_ascii=False))
        again = next(row for row in restored["entries"] if row["source_text"] == "多行")
        self.assertEqual(again["target_text"], MULTILINE)

    def test_supported_language_pairs_all_export_plain_rows(self) -> None:
        # 导出不挑语言对：每一对拿到的都是同样的五列 + 备注列。
        pairs = [f"en-{code}" for code in list(dict.fromkeys(SUPPORTED_LANGS.values()))[:3]]
        for pair in pairs:
            tm_manager.insert_batch([("同一原文", "same value")], pair, 500, "test")
        for pair in pairs:
            rows = tm_manager.get_all_entries_for_export(pair)
            self.assertEqual(len(rows), 1, pair)
            self.assertEqual(rows[0]["target_text"], "same value")
            self.assertEqual(
                set(rows[0]),
                {"source_text", "target_text", "word_type", "pinned", "updated_at", "note", "usage", "scope"},
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
