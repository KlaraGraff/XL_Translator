"""换回上一版不许无声删掉压缩 PDF（审计 2026-08-31:1377 遗留项）。

换回被宣传成「不调用模型、失败整体回滚、输出文件不动」。以前压缩版这一轮没装配
出来（装配失败，或压缩开关事后被关掉）时，``_discard_superseded_compressed_pdf``
会把上一版压缩 PDF 真删掉、只留一行 WARN，换回照报成功。

用例全部离线：假图像客户端 + 假 pdfium，零真实 API 调用。
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core.pdf_image_translation import (
    PDF_OUTPUT_STATE_COMPLETED,
    PdfFileItem,
    PdfImageTranslationRunner,
    PdfPageActionError,
)
from tests.test_pdf_image_translation import (
    _FakeImageClient,
    _drain_all_messages,
    _fake_pdfium_module_by_page_count,
    _page_review_settings,
    _png_bytes,
)


class _SequencedImageClient(_FakeImageClient):
    """按调用次序依次返回不同颜色的图，让「哪一版在磁盘上」可断言。"""

    def __init__(self, images: list[bytes]) -> None:
        super().__init__(images[0])
        self.images = list(images)
        self.calls = 0

    def generate_page(self, **_kwargs):
        index = min(self.calls, len(self.images) - 1)
        self.calls += 1
        return self.images[index]


def _await_rerun(runner: PdfImageTranslationRunner) -> None:
    deadline = time.monotonic() + 20.0
    while runner.page_rerun_state()["active"] and time.monotonic() < deadline:
        time.sleep(0.02)


class RestoreKeepsCompressedPdfTests(unittest.TestCase):
    def _run_then_rerun(self, root: Path, fail_compressed: dict):
        source_pdf = root / "source.pdf"
        source_pdf.write_bytes(b"%PDF-1.4\n")
        settings = _page_review_settings(root)
        settings.pdf.page_retry_attempts = 0
        settings.pdf.generate_compressed_pdf = True
        runner = PdfImageTranslationRunner(
            [PdfFileItem(path=source_pdf, name="source", size_kb=1.0, page_count=2)],
            settings,
            source_root=root,
            image_client=_SequencedImageClient(
                [
                    _png_bytes(1200, 1600, "white"),
                    _png_bytes(1200, 1600, "white"),
                    _png_bytes(1200, 1600, "red"),
                ]
            ),
            task_logger_enabled=False,
        )
        real_assemble = PdfImageTranslationRunner._assemble_translated_pdf

        def assemble(self, record, output_pdf, *, compressed=False):
            if compressed and fail_compressed["on"]:
                raise RuntimeError("磁盘写满")
            return real_assemble(self, record, output_pdf, compressed=compressed)

        patches = [
            patch.dict(sys.modules, {"pypdfium2": _fake_pdfium_module_by_page_count({"source.pdf": 2})}),
            patch("core.model_roles.get_key", return_value="secret"),
            patch.object(PdfImageTranslationRunner, "_assemble_translated_pdf", assemble),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        runner._run()
        record = runner._prepared_files[0].record
        self.assertEqual(record.status, PDF_OUTPUT_STATE_COMPLETED)
        self.assertTrue(Path(record.compressed_pdf_path).is_file())
        _drain_all_messages(runner)
        # 先重生成一页，让这一页有「上一版」可换回。
        runner.rerun_page(relative_path="source.pdf", page_number=1)
        _await_rerun(runner)
        self.assertEqual(runner.page_rerun_state()["error"], "")
        return runner, record

    def test_restore_whose_compressed_rebuild_fails_rolls_back_and_keeps_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fail_compressed = {"on": False}
            runner, record = self._run_then_rerun(Path(tmp), fail_compressed)
            compressed_pdf = Path(record.compressed_pdf_path)
            hd_pdf = Path(record.translated_pdf_path)
            compressed_before = compressed_pdf.read_bytes()
            hd_before = hd_pdf.read_bytes()
            page = next(item for item in record.pages if item.page_number == 1)
            current_image = Path(page.translated_image_path).read_bytes()

            fail_compressed["on"] = True
            with self.assertRaises(PdfPageActionError) as caught:
                runner.restore_previous_page(relative_path="source.pdf", page_number=1)

            self.assertIn("本次换回未生效", str(caught.exception))
            self.assertTrue(compressed_pdf.is_file(), "换回失败时上一版压缩 PDF 被删掉了")
            self.assertEqual(compressed_pdf.read_bytes(), compressed_before)
            self.assertEqual(hd_pdf.read_bytes(), hd_before, "换回失败，高清版却没回滚")
            self.assertEqual(record.compressed_pdf_path, str(compressed_pdf))
            self.assertEqual(Path(page.translated_image_path).read_bytes(), current_image)
            self.assertEqual(record.status, PDF_OUTPUT_STATE_COMPLETED)
            self.assertEqual(list(hd_pdf.parent.glob(".*before-restore")), [])

    def test_restore_rebuilds_compressed_even_after_the_switch_is_turned_off(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner, record = self._run_then_rerun(Path(tmp), {"on": False})
            compressed_pdf = Path(record.compressed_pdf_path)
            before = compressed_pdf.stat().st_mtime_ns

            runner._settings.pdf.generate_compressed_pdf = False
            time.sleep(0.01)
            runner.restore_previous_page(relative_path="source.pdf", page_number=1)

            self.assertTrue(compressed_pdf.is_file(), "压缩开关关掉后换回把压缩 PDF 删了")
            self.assertEqual(record.compressed_pdf_path, str(compressed_pdf))
            self.assertNotEqual(compressed_pdf.stat().st_mtime_ns, before, "压缩版没跟着换回重建")
            self.assertEqual(list(compressed_pdf.parent.glob(".*before-restore")), [])


if __name__ == "__main__":
    unittest.main()
