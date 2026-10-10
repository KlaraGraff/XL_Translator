from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.pdf_image_translation import (
    PDF_OUTPUT_STATE_COMPLETED,
    PdfTaskSummary,
    read_pdf_resume_manifest,
    write_pdf_manifest_and_report,
)
from core.process_artifacts import ProcessArtifacts


class PdfProcessArtifactIntegrationTests(unittest.TestCase):
    def test_metadata_and_resume_checkpoint_stay_outside_delivery_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_data = root / "app-data"
            delivery = root / "source_翻译输出_20261010_120000"
            delivery.mkdir()
            with patch("core.process_artifacts.APP_DATA_DIR", app_data):
                artifacts = ProcessArtifacts.create("pdf", delivery, run_id="test-run")
                summary = PdfTaskSummary(
                    status=PDF_OUTPUT_STATE_COMPLETED,
                    output_dir=str(delivery),
                    target_lang="en",
                    target_lang_label="英文",
                    started_at="2026-10-10T12:00:00",
                    completed_at="2026-10-10T12:00:01",
                    elapsed_sec=1.0,
                    file_count=0,
                    total_page_count=0,
                    generated_pdf_count=0,
                    placeholder_page_count=0,
                    emergency_ratio_normalized_count=0,
                    retry_count=0,
                )
                manifest, report = write_pdf_manifest_and_report(
                    summary, artifact_dir=artifacts.work_dir
                )
                artifacts.finish("succeeded")

                self.assertTrue(manifest.is_file())
                self.assertTrue(report.is_file())
                self.assertFalse((delivery / manifest.name).exists())
                self.assertIsNotNone(read_pdf_resume_manifest(delivery))
                self.assertEqual(list(delivery.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
