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


if __name__ == "__main__":
    unittest.main()
