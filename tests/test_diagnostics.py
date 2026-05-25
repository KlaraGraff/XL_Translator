import json
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

from core import diagnostics
from core.file_scanner import FileItem
from core.word_document import WordFileItem
from settings import AppSettings


class DiagnosticsTests(unittest.TestCase):
    def test_archive_redacts_secrets_and_records_excel_location(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workbook_path = root / "source.xlsx"
            wb = Workbook()
            ws = wb.active
            ws.title = "SheetA"
            ws["A1"] = "需要定位的失败文本"
            wb.save(workbook_path)

            log_path = root / "app.log"
            log_path.write_text(
                "2026-05-20 [ERROR] [task:task123] Authorization: Bearer secret-token\n",
                encoding="utf-8",
            )

            settings = AppSettings()
            settings.engine.cloud_base_url = "https://example.test/v1?api_key=secret-token"
            done_payload = {
                "output_dir": str(root / "out"),
                "file_results": [{"name": "source", "success": True}],
                "issues": [
                    {
                        "type": "api_unavailable",
                        "severity": "needs_action",
                        "message": "API 调用失败",
                        "failed_sources": [
                            {
                                "source": "需要定位的失败文本",
                                "error": "Authorization: Bearer secret-token",
                            }
                        ],
                    }
                ],
            }

            with (
                patch.object(diagnostics, "DIAGNOSTIC_RECORDS_DIR", root / "records"),
                patch.object(diagnostics, "LOG_PATH", log_path),
            ):
                record_dir = diagnostics.archive_task_diagnostics(
                    surface="excel",
                    phase="done",
                    task_id="task123",
                    settings=settings,
                    selected_files=[
                        FileItem(
                            path=workbook_path,
                            name="source",
                            size_kb=1.0,
                            sheets=["SheetA"],
                        )
                    ],
                    logs=[
                        {
                            "level": "ERROR",
                            "message": "Authorization: Bearer secret-token",
                            "ts": "12:00:00",
                        }
                    ],
                    done=done_payload,
                    source_root=root,
                )

                quality_text = (record_dir / "task" / "quality_issues.json").read_text(
                    encoding="utf-8",
                )
                log_text = (record_dir / "logs" / "ui_runtime_log.jsonl").read_text(
                    encoding="utf-8",
                )
                settings_text = (
                    record_dir / "task" / "settings.redacted.json"
                ).read_text(encoding="utf-8")
                self.assertNotIn("secret-token", quality_text)
                self.assertNotIn("secret-token", log_text)
                self.assertNotIn("secret-token", settings_text)

                location_text = (
                    record_dir / "locate" / "excel_cell_locations.csv"
                ).read_text(encoding="utf-8-sig")
                self.assertIn("SheetA", location_text)
                self.assertIn("A1", location_text)

                manifest = json.loads((record_dir / "manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(manifest["quality_issue_count"], 1)
                self.assertEqual(manifest["excel_location_count"], 1)

                data, filename = diagnostics.build_diagnostic_zip_bytes(record_dir)
                self.assertTrue(filename.endswith(".zip"))
                with zipfile.ZipFile(BytesIO(data)) as archive:
                    names = set(archive.namelist())
                self.assertIn(f"{record_dir.name}/manifest.json", names)

                history_data, history_filename, count = diagnostics.build_diagnostics_history_zip_bytes()
                self.assertEqual(count, 1)
                self.assertTrue(history_filename.endswith(".zip"))
                with zipfile.ZipFile(BytesIO(history_data)) as archive:
                    self.assertIn("history_summary.csv", archive.namelist())

    def test_word_archive_records_runtime_events_and_locations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_path = root / "方案.docx"
            source_path.write_bytes(b"placeholder")
            log_path = root / "app.log"
            log_path.write_text("", encoding="utf-8")
            done_payload = {
                "output_dir": str(root / "out"),
                "file_results": [{"name": "方案.docx", "success": True}],
                "issues": [
                    {
                        "file": "方案.docx",
                        "kind": "paragraph",
                        "location": "p8",
                        "location_label": "正文段落 8",
                        "section_path": "一、工程概况",
                        "severity": "resolved",
                        "problem": "规则校验未通过，语义仲裁自动接受",
                        "status": "本段已写入译文。",
                        "snippet": "需要语义仲裁的段落",
                    }
                ],
            }
            logs = [
                {
                    "level": "INFO",
                    "message": "方案.docx · 一、工程概况 · 正文段落 8 正在语义仲裁",
                    "ts": "12:01:00",
                },
                {
                    "level": "OK",
                    "message": "方案.docx · 一、工程概况 · 正文段落 8 语义仲裁接受",
                    "ts": "12:01:02",
                },
            ]

            with (
                patch.object(diagnostics, "DIAGNOSTIC_RECORDS_DIR", root / "records"),
                patch.object(diagnostics, "LOG_PATH", log_path),
            ):
                record_dir = diagnostics.archive_task_diagnostics(
                    surface="word",
                    phase="done",
                    task_id="word-task",
                    settings=AppSettings(),
                    selected_files=[
                        WordFileItem(
                            path=source_path,
                            name="方案.docx",
                            size_kb=1.0,
                            paragraph_count=1,
                        )
                    ],
                    logs=logs,
                    done=done_payload,
                    source_root=root,
                )

                manifest = json.loads((record_dir / "manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(manifest["runtime_log_count"], 2)
                self.assertEqual(manifest["word_location_count"], 1)
                self.assertEqual(manifest["word_runtime_event_count"], 2)

                runtime_text = (
                    record_dir / "locate" / "word_runtime_events.csv"
                ).read_text(encoding="utf-8-sig")
                self.assertIn("正文段落 8", runtime_text)
                self.assertIn("正在语义仲裁", runtime_text)
                self.assertIn("语义仲裁接受", runtime_text)

                location_text = (
                    record_dir / "locate" / "word_segment_locations.csv"
                ).read_text(encoding="utf-8-sig")
                self.assertIn("规则校验未通过，语义仲裁自动接受", location_text)


if __name__ == "__main__":
    unittest.main()
