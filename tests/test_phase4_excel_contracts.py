"""Phase 4 Excel contracts that must remain safe without local Office.

All task-runner cases use mocked translation/Excel automation.  The suite is
therefore suitable for the isolated macOS development gate while the real
Office and macOS 12 release checks stay separately deferred.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill

from core.api_config_check import ApiConfigCheckResult
from core import tm_manager, xls_converter
from core.file_scanner import FileItem, scan_excel_sources
from core.language_preflight import TranslationLanguageResult
from core.mixed_language import MIXED_MARK_UNRESOLVED
from core.task_runner import DoneMsg, StoppedMsg, TaskRunner
from settings import AppSettings, EngineSettings
from tests.rate_pins import pinned_surface_rate
from tests.xls_password_fixture import write_password_protected_xls


class _PreflightEngine:
    engine_name = "phase4/mock"

    def __init__(self, result_for_sample: dict[str, str] | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self._result_for_sample = dict(result_for_sample or {})

    def chat(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        samples = json.loads(user)["samples"]
        first = str(samples[0]) if samples else ""
        return self._result_for_sample.get(first, '{"source_langs":["zh"]}')


def _settings(*, source_lang: str = "zh", target_lang: str = "en") -> AppSettings:
    settings = AppSettings(
        engine=EngineSettings(
            mode="cloud",
            cloud_provider="custom_openai",
            cloud_model="phase4-test-model",
            cloud_base_url="https://example.invalid/v1",
            concurrency=1,
            batch_size=10,
        ),
        source_lang=source_lang,
        target_lang=target_lang,
        # 语种预检数模型调用次数；「自动识别场景」的文档说明请求另有测试。
        excel_domain_preset="无",
        excel_smart_context=False,
    )
    # This contract suite uses mocked writers and tests the legacy sheet set.
    settings.excel_output.translate_sheet_names = False
    return settings


def _done_message(runner: TaskRunner) -> DoneMsg:
    messages = [message for message in list(runner._queue.queue) if isinstance(message, DoneMsg)]
    if len(messages) != 1:
        raise AssertionError(f"expected one DoneMsg, got {len(messages)}")
    return messages[0]


class ExcelWorkbookContractTests(unittest.TestCase):
    def test_writer_keeps_source_untouched_formula_merge_style_and_existing_fill(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.xlsx"
            wb = Workbook()
            ws = wb.active
            ws.title = "报价"
            ws["A1"] = "施工内容"
            ws["A1"].font = Font(name="Arial", bold=True, italic=True)
            ws["A1"].fill = PatternFill("solid", fgColor="00B050")
            ws["B1"] = "=SUM(C1:C2)"
            ws.merge_cells("A2:B2")
            ws["A2"] = "合并单元格内容"
            ws.row_dimensions[1].height = 25
            wb.save(source)
            wb.close()
            source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
            review_positions: list[dict[str, str]] = []

            from core.bilingual_writer import write_bilingual_file

            output = write_bilingual_file(
                source_path=source,
                output_dir=root / "out",
                translations={"施工内容": "Construction scope"},
                target_lang="en",
                source_lang="zh",
                keep_original_sheets=True,
                formula_display_value_backfill=False,
                review_marks={"施工内容": MIXED_MARK_UNRESOLVED},
                review_mark_colors={MIXED_MARK_UNRESOLVED: "FF0000"},
                mark_review_items=True,
                existing_fill_policy="skip",
                review_positions=review_positions,
            )

            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), source_sha256)
            written = load_workbook(output, data_only=False)
            try:
                sheet = written["报价"]
                self.assertEqual(sheet["A1"].value, "施工内容\nConstruction scope")
                self.assertEqual(sheet["B1"].value, "=SUM(C1:C2)")
                self.assertIn("A2:B2", {str(item) for item in sheet.merged_cells.ranges})
                self.assertTrue(sheet["A1"].font.bold)
                self.assertTrue(sheet["A1"].font.italic)
                self.assertEqual(sheet["A1"].fill.fgColor.rgb, "0000B050")
                self.assertIn("报价_原文", written.sheetnames)
                self.assertEqual(written["报价_原文"]["A1"].value, "施工内容")
            finally:
                written.close()
            self.assertEqual(
                review_positions,
                [
                    {
                        "worksheet": "报价",
                        "cell": "A1",
                        "category": MIXED_MARK_UNRESOLVED,
                        "action": "preserved_existing_fill",
                    }
                ],
            )


class ExcelScanningContractTests(unittest.TestCase):
    def test_scan_reports_usable_files_skips_broken_sources_and_excludes_old_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            good = root / "nested" / "source.xlsx"
            good.parent.mkdir()
            workbook = Workbook()
            workbook.active.title = "First"
            workbook.create_sheet("Second")
            workbook.save(good)
            workbook.close()
            (root / "~$source.xlsx").touch()
            (root / "broken.xlsx").write_text("not an Excel workbook", encoding="utf-8")
            old_output = root / "_翻译输出_20260724" / "old.xlsx"
            old_output.parent.mkdir()
            old_output.write_bytes(good.read_bytes())

            result = scan_excel_sources(root)

            self.assertEqual([item.path for item in result.items], [good])
            self.assertEqual(result.items[0].relative_path, "nested/source.xlsx")
            self.assertEqual(result.items[0].format, "xlsx")
            self.assertEqual(len(result.items[0].sheets), 2)
            self.assertEqual(result.summary["scanned_count"], 1)
            self.assertEqual(result.summary["selected_count"], 1)
            self.assertEqual(result.summary["sheet_count"], 2)
            self.assertEqual(result.summary["xls_count"], 0)
            self.assertEqual(len(result.skipped), 1)
            self.assertEqual(result.skipped[0].path.name, "broken.xlsx")
            self.assertIn("读取失败", result.skipped[0].reason)


class ExcelCompatibilityContractTests(unittest.TestCase):
    def test_scan_risk_message_is_a_string_in_both_states(self) -> None:
        from core.file_scanner import ExcelScanResult
        from core.file_scanner import FileItem as ScanFileItem

        # A dangling comma once turned the message into a 1-tuple, so the
        # frontend received an array and the empty state was still truthy.
        empty = ExcelScanResult(root=Path("."))
        self.assertIsInstance(empty.risk["message"], str)
        self.assertEqual(empty.risk["message"], "")

        with_xls = ExcelScanResult(
            root=Path("."),
            items=[
                ScanFileItem(path=Path("legacy.xls"), name="legacy", size_kb=1.0, format="xls")
            ],
        )
        self.assertIsInstance(with_xls.risk["message"], str)
        self.assertIn(".xls", with_xls.risk["message"])

    def test_macos_automation_help_uses_the_real_monterey_settings_path(self) -> None:
        with (
            patch.object(xls_converter.platform, "system", return_value="Darwin"),
            patch.object(xls_converter.platform, "mac_ver", return_value=("12.7.6", (), "")),
        ):
            self.assertEqual(
                xls_converter.macos_excel_automation_privacy_path(),
                "系统偏好设置 > 安全性与隐私 > 隐私 > 自动化",
            )
        with (
            patch.object(xls_converter.platform, "system", return_value="Darwin"),
            patch.object(xls_converter.platform, "mac_ver", return_value=("13.0", (), "")),
        ):
            self.assertEqual(
                xls_converter.macos_excel_automation_privacy_path(),
                "系统设置 > 隐私与安全性 > 自动化",
            )


class _ProfileEngine:
    """「自动识别场景」下只会收到文档说明这一次请求（源语言已指定，不做语种预检）。"""

    engine_name = "phase4/mock"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def chat(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return "建筑施工图。「甲」指甲级防火门，常译 Class A。"


class ExcelSmartModeContractTests(unittest.TestCase):
    """「自动识别场景」：先写文档说明带进提示词；短词的旧译法只作候选，不直接套用。

    记忆库用隔离的真实数据库：整段规则要读词条的来源和备注，桩出来的查询没有这些。
    """

    def test_profile_reaches_the_prompt_and_short_auto_memory_is_only_a_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "doors.xlsx"
            texts = {source: ["甲", "防火门耐火等级说明", "甲", "FM", "门窗表"]}
            engine = _ProfileEngine()
            settings = _settings(source_lang="zh", target_lang="en")
            settings.excel_smart_context = True
            with ExitStack() as stack:
                stack.enter_context(patch.object(tm_manager, "DB_PATH", root / "tm.db"))
                stack.enter_context(patch.object(tm_manager, "BACKUPS_DIR", root / "backups"))
                tm_manager.init_db()
                # 自动存过的「甲 → Party A」和长句；用户手动录入过「FM → Fire door」。
                tm_manager.insert_auto_entries(
                    [
                        {"source_text": "甲", "translation": "Party A", "source_lang": "zh", "tm_eligible": True},
                        {"source_text": "防火门耐火等级说明", "translation": "Fire rating note", "source_lang": "zh", "tm_eligible": True},
                    ],
                    "en",
                    200,
                    "mock",
                )
                tm_manager.insert_manual_entry_detailed("FM", "Fire door", "zh-en")
                ExcelTaskRunnerContractTests._run_patches(
                    self,
                    stack,
                    root=root,
                    engine=engine,
                    texts_by_path=texts,
                    lookup_side_effect=tm_manager.lookup_batch,
                )
                translate = stack.enter_context(
                    patch(
                        "core.task_runner.translate_texts",
                        return_value={"甲": "Class A", "门窗表": "Door and window schedule"},
                    )
                )
                insert = stack.enter_context(patch("core.task_runner.tm_manager.insert_batch", return_value=0))
                runner = TaskRunner([FileItem(path=source, name="doors", size_kb=1.0)], settings, source_root=root)
                runner._run()
                saved = [row for row in tm_manager.list_entries_for_target("en") if row["note"]]

            # 第一次是通读文档，第二次是给自动保存的短词写场景说明。
            self.assertEqual(len(engine.calls), 2)
            self.assertIn("防火门耐火等级说明", engine.calls[0][1])
            translate.assert_called_once()
            misses, wrapped_engine, _lang, system_prompt = translate.call_args.args[:4]
            self.assertEqual(sorted(misses), ["甲", "门窗表"])
            self.assertIn("甲级防火门", system_prompt)
            self.assertTrue(system_prompt.startswith("system"))
            # 批次经过词条包装器：「甲」随批附上旧译法作候选。
            self.assertIn("Party A", wrapped_engine._block([wrapped_engine._scan(["甲"])[0]]))
            stored = [pair for call in insert.call_args_list for pair in call.args[0]]
            # 长文字照常存进记忆库，短词不存成通用词条，而是存成带备注的场景译法。
            self.assertEqual([source_text for source_text, _target in stored], ["门窗表"])
            self.assertEqual([(row["source_text"], row["target_text"]) for row in saved], [("甲", "Class A")])
            terms = runner._finish_terms()
            self.assertEqual(terms["hits"], {"always": 1, "scene": 0, "segment": 1})
            self.assertEqual(terms["auto_saved"][0]["term"], "甲")


class ExcelTaskRunnerContractTests(unittest.TestCase):
    def _run_patches(
        self,
        stack: ExitStack,
        *,
        root: Path,
        engine: _PreflightEngine,
        texts_by_path: dict[Path, list[str]],
        lookup_side_effect=None,
        writer_side_effect=None,
    ) -> MagicMock:
        writer = stack.enter_context(
            patch(
                "core.task_runner.bilingual_writer.write_bilingual_file",
                side_effect=writer_side_effect
                or (lambda **kwargs: root / "out" / f"{kwargs['source_path'].stem}.xlsx"),
            )
        )
        stack.enter_context(
            patch("core.task_runner.TaskLogger", return_value=MagicMock(task_id="phase4-contract"))
        )
        stack.enter_context(
            patch("core.task_runner.check_translation_api_config", return_value=ApiConfigCheckResult(ok=True))
        )
        stack.enter_context(patch("core.task_runner.build_engine", return_value=engine))
        stack.enter_context(patch("core.task_runner.get_system_prompt", return_value="system"))
        stack.enter_context(patch("core.task_runner.resolve_effective_model_config", return_value=object()))
        stack.enter_context(
            patch(
                "core.task_runner.resolve_surface_rate",
                side_effect=pinned_surface_rate(batch_items=10, concurrency=1),
            )
        )
        stack.enter_context(
            patch(
                "core.task_runner.bilingual_writer.build_output_dir",
                return_value=root / "out",
            )
        )

        def collect(path: Path, *_args, **_kwargs):
            return list(texts_by_path[path]), 1

        stack.enter_context(patch.object(TaskRunner, "_collect_texts", side_effect=collect))
        stack.enter_context(
            patch(
                "core.task_runner.tm_manager.lookup_batch",
                side_effect=lookup_side_effect
                or (lambda texts, _pair: {text: "TM translation" for text in texts}),
            )
        )
        stack.enter_context(patch("core.task_runner.tm_manager.insert_batch", return_value=0))
        stack.enter_context(patch("core.task_runner.translate_texts", return_value={}))
        return writer

    def test_auto_preflight_is_once_per_file_bounded_and_uses_actual_tm_pairs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            zh_file = root / "zh.xlsx"
            en_file = root / "en.xlsx"
            long_secret = "仅用于验证不发送完整文件" * 800
            texts = {
                zh_file: [long_secret, "项目名称"],
                en_file: ["The concrete slab requires inspection."],
            }
            engine = _PreflightEngine(
                {
                    long_secret[:240]: '{"source_langs":["zh"]}',
                    "The concrete slab requires inspection.": '{"source_langs":["en"]}',
                }
            )
            lookup_values_by_pair: dict[str, set[str]] = {}

            def lookup(values: list[str], pair: str) -> dict[str, str]:
                lookup_values_by_pair.setdefault(pair, set()).update(values)
                return {value: "TM translation" for value in values}

            with ExitStack() as stack:
                writer = self._run_patches(
                    stack,
                    root=root,
                    engine=engine,
                    texts_by_path=texts,
                    lookup_side_effect=lookup,
                )
                runner = TaskRunner(
                    [
                        FileItem(path=zh_file, name="zh", size_kb=1.0),
                        FileItem(path=en_file, name="en", size_kb=1.0),
                    ],
                    _settings(source_lang="auto", target_lang="fr"),
                    source_root=root,
                )
                runner._run()

            self.assertEqual(len(engine.calls), 2)
            payloads = [json.loads(user) for _system, user in engine.calls]
            all_samples = {
                sample for payload in payloads for sample in payload["samples"]
            }
            self.assertIn(long_secret[:240], all_samples)
            self.assertIn("The concrete slab requires inspection.", all_samples)
            self.assertTrue(all(long_secret not in user for _system, user in engine.calls))
            self.assertTrue(all(len(user) < 3000 for _system, user in engine.calls))
            self.assertEqual(set(lookup_values_by_pair), {"zh-fr", "en-fr"})
            self.assertEqual(
                lookup_values_by_pair["zh-fr"], {long_secret, "项目名称"}
            )
            self.assertEqual(
                lookup_values_by_pair["en-fr"],
                {"The concrete slab requires inspection."},
            )
            self.assertFalse(any(pair.startswith("auto-") for pair in lookup_values_by_pair))
            self.assertEqual(writer.call_count, 2)
            done = _done_message(runner)
            self.assertEqual(len(done.file_results), 2)
            self.assertEqual([item["status"] for item in done.files], ["succeeded", "succeeded"])
            self.assertEqual(done.kpi["selected_file_count"], 2)
            self.assertEqual(done.kpi["succeeded_file_count"], 2)
            self.assertEqual(done.kpi["failed_file_count"], 0)
            self.assertEqual(done.kpi["unstarted_file_count"], 0)
            self.assertEqual(done.kpi["model_translation_text_count"], 0)
            self.assertEqual(done.language["mode"], "automatic")
            self.assertEqual(
                {item["preflight"]["source_langs"][0] for item in done.language["files"]},
                {"zh", "en"},
            )

    def test_high_fidelity_xls_permission_failure_never_silently_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = root / "legacy.xls"
            healthy = root / "healthy.xlsx"
            legacy.touch()
            healthy.touch()
            engine = _PreflightEngine()
            with ExitStack() as stack:
                writer = self._run_patches(
                    stack,
                    root=root,
                    engine=engine,
                    texts_by_path={healthy: ["施工内容"]},
                )
                excel_app = MagicMock()
                excel_app.books.open.side_effect = RuntimeError(
                    "not authorized to send apple events"
                )
                stack.enter_context(patch("core.task_runner.initialize_excel_thread", return_value=None))
                stack.enter_context(patch("core.task_runner.finalize_excel_thread"))
                stack.enter_context(patch("core.task_runner.create_excel_app", return_value=excel_app))
                fallback = stack.enter_context(patch("core.xls_converter.convert_with_fallback"))
                runner = TaskRunner(
                    [
                        FileItem(path=legacy, name="legacy", size_kb=1.0),
                        FileItem(path=healthy, name="healthy", size_kb=1.0),
                    ],
                    _settings(),
                    source_root=root,
                    allow_xls_fallback=False,
                )
                runner._run()

            fallback.assert_not_called()
            writer.assert_called_once()
            by_source = {
                item["source_path"]: item for item in _done_message(runner).file_results
            }
            self.assertFalse(by_source[str(legacy)]["success"])
            self.assertIn("自动化", by_source[str(legacy)]["error"])
            self.assertTrue(by_source[str(healthy)]["success"])

    def test_xls_compatibility_conversion_requires_explicit_confirmation(self) -> None:
        """本机没有 LibreOffice 时，兼容转换应退到 xlrd 纯值化——这是接入 LO 之前
        就有的桩测，这里补上 _find_soffice 探测桩，让它继续只测 xlrd 这条路径；
        LO 命中那条路径由下面 test_xls_compatibility_conversion_prefers_libreoffice_when_available 覆盖。"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = root / "legacy.xls"
            converted = root / "converted.xlsx"
            legacy.touch()
            converted.touch()
            engine = _PreflightEngine()
            with ExitStack() as stack:
                writer = self._run_patches(
                    stack,
                    root=root,
                    engine=engine,
                    texts_by_path={converted: ["施工内容"]},
                )
                stack.enter_context(
                    patch("core.word_converter._find_soffice", return_value=None)
                )
                fallback = stack.enter_context(
                    patch("core.xls_converter.convert_with_fallback", return_value=converted)
                )
                runner = TaskRunner(
                    [FileItem(path=legacy, name="legacy", size_kb=1.0)],
                    _settings(),
                    source_root=root,
                    allow_xls_fallback=True,
                )
                runner._run()

            fallback.assert_called_once_with(legacy)
            writer.assert_called_once()
            done = _done_message(runner)
            self.assertTrue(done.file_results[0]["success"])
            self.assertEqual(done.file_results[0]["conversion_mode"], "compatibility_fallback")

    def test_xls_compatibility_conversion_prefers_libreoffice_when_available(self) -> None:
        """本机装了 LibreOffice 时，兼容转换应该先走 LO，xlrd 兜底完全不该被调用。"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = root / "legacy.xls"
            converted = root / "converted.xlsx"
            legacy.touch()
            converted.touch()
            engine = _PreflightEngine()
            with ExitStack() as stack:
                writer = self._run_patches(
                    stack,
                    root=root,
                    engine=engine,
                    texts_by_path={converted: ["施工内容"]},
                )
                stack.enter_context(
                    patch(
                        "core.word_converter._find_soffice",
                        return_value="/Applications/LibreOffice.app/Contents/MacOS/soffice",
                    )
                )
                libreoffice = stack.enter_context(
                    patch("core.xls_converter.convert_with_libreoffice", return_value=converted)
                )
                fallback = stack.enter_context(patch("core.xls_converter.convert_with_fallback"))
                runner = TaskRunner(
                    [FileItem(path=legacy, name="legacy", size_kb=1.0)],
                    _settings(),
                    source_root=root,
                    allow_xls_fallback=True,
                )
                runner._run()

            libreoffice.assert_called_once_with(legacy)
            fallback.assert_not_called()
            writer.assert_called_once()
            done = _done_message(runner)
            self.assertTrue(done.file_results[0]["success"])
            self.assertEqual(done.file_results[0]["conversion_mode"], "libreoffice_conversion")

    def test_xls_compatibility_conversion_falls_back_when_libreoffice_conversion_fails(self) -> None:
        """LO 探测到了，但真正转换时失败（比如遇到损坏文件）：必须退回 xlrd，
        不能让这一个文件直接判死——这是「兼容转换」这个二元授权对用户的承诺。"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = root / "legacy.xls"
            converted = root / "converted.xlsx"
            legacy.touch()
            converted.touch()
            engine = _PreflightEngine()
            with ExitStack() as stack:
                writer = self._run_patches(
                    stack,
                    root=root,
                    engine=engine,
                    texts_by_path={converted: ["施工内容"]},
                )
                stack.enter_context(
                    patch(
                        "core.word_converter._find_soffice",
                        return_value="/Applications/LibreOffice.app/Contents/MacOS/soffice",
                    )
                )
                libreoffice = stack.enter_context(
                    patch(
                        "core.xls_converter.convert_with_libreoffice",
                        side_effect=xls_converter.LibreOfficeConversionError("模拟转换失败"),
                    )
                )
                fallback = stack.enter_context(
                    patch("core.xls_converter.convert_with_fallback", return_value=converted)
                )
                runner = TaskRunner(
                    [FileItem(path=legacy, name="legacy", size_kb=1.0)],
                    _settings(),
                    source_root=root,
                    allow_xls_fallback=True,
                )
                runner._run()

            libreoffice.assert_called_once_with(legacy)
            fallback.assert_called_once_with(legacy)
            writer.assert_called_once()
            done = _done_message(runner)
            self.assertTrue(done.file_results[0]["success"])
            self.assertEqual(done.file_results[0]["conversion_mode"], "compatibility_fallback")

    def test_password_protected_xls_fails_with_the_password_sentence_not_a_crash(self) -> None:
        """加密的 .xls 走完两条兼容转换路线后要停在「受密码保护」这句话上。

        doc:97 要求补上损坏/加密文件的报错形态。加密文件这一半可以离线复现：夹具
        由 ``tests/xls_password_fixture.py`` 现写，带真的 FILEPASS 记录。两个转换函数
        都不打桩，真的让 xlrd 去读，它抛 ``Workbook is encrypted``——这条原始英文不能
        原样摆到用户面前，任务也不能把这一个文件静静跳过或者整个崩掉。
        （装了 LibreOffice 时同一份文件走 soffice，返回码 1，由
        ``tests/test_libreoffice_xls_route.py`` 的真机用例覆盖。）
        """
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = write_password_protected_xls(root / "protected.xls")
            with ExitStack() as stack:
                writer = self._run_patches(
                    stack,
                    root=root,
                    engine=_PreflightEngine(),
                    texts_by_path={},
                )
                # 固定走「本机没有 LibreOffice」这一支：两种情况最终都要落到同一句
                # 用户提示，但只有这一支在哪台机器上都一样跑。
                stack.enter_context(
                    patch("core.word_converter._find_soffice", return_value=None)
                )
                runner = TaskRunner(
                    [FileItem(path=legacy, name="protected", size_kb=1.0, format="xls")],
                    _settings(),
                    source_root=root,
                    allow_xls_fallback=True,
                )
                runner._run()

            writer.assert_not_called()
            done = _done_message(runner)
            result = done.file_results[0]
            self.assertFalse(result["success"])
            self.assertEqual(result["status"], "failed")
            self.assertIn("受密码保护", result["error"])
            self.assertNotIn("encrypted", result["error"])

    def test_truncated_xls_fails_with_the_corrupted_sentence_and_skips_libreoffice(self) -> None:
        """截断的 .xls：LibreOffice 会静默转出乱码表，所以根本不交给它。

        两个转换函数都不打桩、soffice 也假装装着：文件坏了要在转换前就被认出来，
        任务里这个文件判失败并说「已损坏或不完整」，不报成「兼容转换不可用」再退回
        xlrd，更不能把乱码当译文来源。
        """
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = write_password_protected_xls(root / "truncated.xls", encrypted=False)
            raw = legacy.read_bytes()
            legacy.write_bytes(raw[: len(raw) // 2])
            with ExitStack() as stack:
                writer = self._run_patches(
                    stack,
                    root=root,
                    engine=_PreflightEngine(),
                    texts_by_path={},
                )
                stack.enter_context(
                    patch(
                        "core.word_converter._find_soffice",
                        return_value="/Applications/LibreOffice.app/Contents/MacOS/soffice",
                    )
                )
                run = stack.enter_context(patch.object(xls_converter.subprocess, "run"))
                runner = TaskRunner(
                    [FileItem(path=legacy, name="truncated", size_kb=1.0, format="xls")],
                    _settings(),
                    source_root=root,
                    allow_xls_fallback=True,
                )
                runner._run()

            run.assert_not_called()
            writer.assert_not_called()
            result = _done_message(runner).file_results[0]
            self.assertFalse(result["success"])
            self.assertIn("已损坏或不完整", result["error"])

    def test_excel_review_colors_are_not_read_from_word_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.xlsx"
            source.touch()
            settings = _settings()
            expected_excel_colors = {MIXED_MARK_UNRESOLVED: "123456"}
            settings_payload = settings.model_dump()
            settings_payload["excel_review"] = {
                **settings.excel_review.model_dump(),
                "mark_colors": expected_excel_colors,
            }
            settings_payload["word_review"] = {
                "mark_colors": {MIXED_MARK_UNRESOLVED: "ABCDEF"}
            }
            settings = AppSettings(**settings_payload)
            engine = _PreflightEngine()
            with ExitStack() as stack:
                writer = self._run_patches(
                    stack,
                    root=root,
                    engine=engine,
                    texts_by_path={source: ["施工内容"]},
                )
                runner = TaskRunner(
                    [FileItem(path=source, name="source", size_kb=1.0)],
                    settings,
                    source_root=root,
                )
                runner._run()

            applied_colors = writer.call_args.kwargs["review_mark_colors"]
            self.assertEqual(
                applied_colors[MIXED_MARK_UNRESOLVED],
                expected_excel_colors[MIXED_MARK_UNRESOLVED],
            )
            self.assertNotEqual(
                applied_colors[MIXED_MARK_UNRESOLVED],
                settings.word_review.mark_colors[MIXED_MARK_UNRESOLVED],
            )

    def test_auto_tm_writes_only_model_reported_language_inside_file_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "fr.xlsx"
            source.touch()
            french_one = "Bonjour le monde"
            french_two = "Texte complémentaire"
            engine = _PreflightEngine(
                {
                    french_one: '{"source_langs":["fr"]}',
                    french_two: '{"source_langs":["fr"]}',
                }
            )
            inserted: list[tuple[str, list[tuple[str, str]]]] = []

            def capture_insert(entries, pair, *_args, **_kwargs):
                inserted.append((pair, list(entries)))
                return len(entries)

            with ExitStack() as stack:
                self._run_patches(
                    stack,
                    root=root,
                    engine=engine,
                    texts_by_path={source: [french_one, french_two]},
                    lookup_side_effect=lambda texts, _pair: {text: None for text in texts},
                )
                stack.enter_context(
                    patch(
                        "core.task_runner.translate_texts_with_sources",
                        return_value={
                            french_one: TranslationLanguageResult(
                                french_one,
                                "Hello world",
                                source_lang="fr",
                                target_lang="zh",
                                tm_eligible=True,
                            ),
                            french_two: TranslationLanguageResult(
                                french_two,
                                "Supplementary text",
                                source_lang="en",
                                target_lang="zh",
                                tm_eligible=True,
                            ),
                        },
                    )
                )
                stack.enter_context(
                    patch("core.tm_manager.insert_batch", side_effect=capture_insert)
                )
                runner = TaskRunner(
                    [FileItem(path=source, name="fr", size_kb=1.0)],
                    _settings(source_lang="auto", target_lang="zh"),
                    source_root=root,
                )
                runner._run()

            self.assertEqual(inserted, [("fr-zh", [(french_one, "Hello world")])])
            self.assertTrue(_done_message(runner).file_results[0]["success"])

    def test_stop_before_scanning_preserves_sources_and_records_unstarted_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.xlsx"
            second = root / "second.xlsx"
            first.write_bytes(b"first source bytes")
            second.write_bytes(b"second source bytes")
            source_hashes = {
                path: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (first, second)
            }
            engine = _PreflightEngine()
            with ExitStack() as stack:
                self._run_patches(
                    stack,
                    root=root,
                    engine=engine,
                    texts_by_path={},
                )
                runner = TaskRunner(
                    [
                        FileItem(path=first, name="first", size_kb=1.0),
                        FileItem(path=second, name="second", size_kb=1.0),
                    ],
                    _settings(),
                    source_root=root,
                )
                runner.stop()
                runner._run()

            stopped = [
                message
                for message in list(runner._queue.queue)
                if isinstance(message, StoppedMsg)
            ]
            self.assertEqual(len(stopped), 1)
            self.assertEqual(
                [item["status"] for item in stopped[0].files],
                ["unstarted", "unstarted"],
            )
            self.assertEqual(
                [item["source_path"] for item in stopped[0].files],
                [str(first), str(second)],
            )
            self.assertEqual(stopped[0].kpi["unstarted_file_count"], 2)
            self.assertEqual(
                {
                    path: hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in (first, second)
                },
                source_hashes,
            )

    def test_result_contract_aggregates_file_review_locations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.xlsx"
            source.touch()
            engine = _PreflightEngine()

            def writer_with_review_position(**kwargs):
                kwargs["review_positions"].append(
                    {
                        "worksheet": "报价",
                        "cell": "C7",
                        "category": MIXED_MARK_UNRESOLVED,
                        "action": "preserved_existing_fill",
                    }
                )
                return root / "out" / "source.xlsx"

            with ExitStack() as stack:
                self._run_patches(
                    stack,
                    root=root,
                    engine=engine,
                    texts_by_path={source: ["施工内容"]},
                    writer_side_effect=writer_with_review_position,
                )
                runner = TaskRunner(
                    [FileItem(path=source, name="source", size_kb=1.0)],
                    _settings(),
                    source_root=root,
                )
                runner._run()

            done = _done_message(runner)
            self.assertEqual(done.file_results[0]["review_count"], 1)
            self.assertEqual(
                done.review["items"],
                [
                    {
                        "file": "source.xlsx",
                        "worksheet": "报价",
                        "cell": "C7",
                        "category": MIXED_MARK_UNRESOLVED,
                        "action": "preserved_existing_fill",
                    }
                ],
            )
            self.assertEqual(done.review["counts"], {MIXED_MARK_UNRESOLVED: 1})

    def test_result_contract_exposes_anchor_frozen_count(self) -> None:
        """写入器报上来的锚点冻结数要原样进到文件级结果里，供界面展示。"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.xlsx"
            source.touch()
            engine = _PreflightEngine()
            seen_kwargs: dict = {}

            def writer_with_anchor_stats(**kwargs):
                seen_kwargs.update(kwargs)
                kwargs["stats"]["anchor_frozen_count"] = 7
                return root / "out" / "source.xlsx"

            with ExitStack() as stack:
                self._run_patches(
                    stack,
                    root=root,
                    engine=engine,
                    texts_by_path={source: ["施工内容"]},
                    writer_side_effect=writer_with_anchor_stats,
                )
                # 打开精调会让任务真的去拉起本机 Microsoft Excel。这条用例查的是
                # 「写入器报上来的统计有没有原样进到结果里」，不是 AutoFit 本身，
                # 所以把启动 Excel 和批量精调这两步换掉——否则在没装 Excel 的
                # 构建机上必然报 ApplicationNotFoundError。
                stack.enter_context(
                    patch("core.task_runner.create_excel_app", return_value=MagicMock())
                )
                stack.enter_context(
                    patch(
                        "core.task_runner.bilingual_writer.autofit_files_batch",
                        return_value=True,
                    )
                )
                settings = _settings()
                # 界面上「Excel 精调行高」默认就是开的（ui/src/views/workspace.ts）
                settings.excel_output.enable_excel_autofit = True
                settings.excel_output.lock_row_height = False
                runner = TaskRunner(
                    [FileItem(path=source, name="source", size_kb=1.0)],
                    settings,
                    source_root=root,
                )
                runner._run()

            done = _done_message(runner)
            self.assertEqual(done.file_results[0]["anchor_frozen_count"], 7)
            # 开着 Excel 精调、没锁行高 → 必须告诉写入器整表冻结
            self.assertIs(seen_kwargs["external_autofit_planned"], True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
