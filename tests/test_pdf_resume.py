from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from core.model_roles import SOURCE_INDEPENDENT
from core.pdf_image_translation import (
    PDF_MANIFEST_FILENAME,
    PDF_OUTPUT_STATE_COMPLETED,
    SOURCE_TYPE_IMAGE,
    SOURCE_TYPE_PDF,
    PdfFileItem,
    PdfFileRecord,
    PdfImageTranslationRunner,
    PdfPageRecord,
    PdfTaskSummary,
    page_image_name,
    read_pdf_resume_manifest,
    resolve_pdf_page_archive_dirs,
    reusable_pdf_pages,
    write_pdf_manifest_and_report,
)
from core.task_runner import DoneMsg, LogMsg
from settings import AppSettings

_PAGE_WIDTH_PT, _PAGE_HEIGHT_PT = 432.0, 576.0
_RELATIVE_PDF = "docs/source.pdf"


def _png_bytes(width: int, height: int, color: str = "white") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _write_source_pdf(path: Path, page_count: int) -> None:
    """A real multi-page PDF so the run renders through real pypdfium2."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pages = [Image.new("RGB", (1200, 1600), "white") for _ in range(page_count)]
    pages[0].save(
        path,
        format="PDF",
        resolution=200.0,
        save_all=True,
        append_images=pages[1:],
    )


def _history_page(
    page_number: int,
    *,
    status: str = "success",
    placeholder: bool = False,
    quality_flags: list[str] | None = None,
    review_status: str = "skipped",
    page_width_pt: float = _PAGE_WIDTH_PT,
    page_height_pt: float = _PAGE_HEIGHT_PT,
    translated_image_path: str = "",
) -> PdfPageRecord:
    return PdfPageRecord(
        page_number=page_number,
        source_image_path="",
        file_name="source.pdf",
        translated_image_path=translated_image_path,
        status=status,
        attempts=1,
        placeholder=placeholder,
        quality_flags=list(quality_flags or []),
        review_status=review_status,
        source_width_px=1800,
        source_height_px=2400,
        output_width_px=1200,
        output_height_px=1600,
        page_width_pt=page_width_pt,
        page_height_pt=page_height_pt,
        render_dpi=300.0,
    )


def _write_history_package(
    root: Path,
    *,
    pages: list[PdfPageRecord],
    page_count: int,
    target_lang: str = "en",
    source_size_bytes: int = 0,
    source_type: str = SOURCE_TYPE_PDF,
    relative_path: str = _RELATIVE_PDF,
) -> Path:
    """Write a manifest with the production writer, so the fixture cannot drift."""
    history = root / "docs_翻译输出_20260828_154200"
    history.mkdir(parents=True, exist_ok=True)
    record = PdfFileRecord(
        name=Path(relative_path).name,
        source_path=str(root / relative_path),
        relative_path=relative_path,
        source_type=source_type,
        page_count=page_count,
        source_pdf_size_bytes=source_size_bytes,
        pages=pages,
    )
    summary = PdfTaskSummary(
        status=PDF_OUTPUT_STATE_COMPLETED,
        output_dir=str(history),
        target_lang=target_lang,
        target_lang_label="英文",
        started_at="2026-08-28T15:40:00",
        completed_at="2026-08-28T15:42:00",
        elapsed_sec=120.0,
        file_count=1,
        total_page_count=page_count,
        generated_pdf_count=1,
        placeholder_page_count=sum(1 for page in pages if page.placeholder),
        emergency_ratio_normalized_count=0,
        retry_count=0,
        files=[record],
    )
    write_pdf_manifest_and_report(summary)
    return history


def _place_history_page(
    history: Path,
    page_number: int,
    page_count: int,
    *,
    failed: bool = False,
    data: bytes | None = None,
    relative_path: str = _RELATIVE_PDF,
) -> Path:
    _, translated_dir = resolve_pdf_page_archive_dirs(history, Path(relative_path))
    translated_dir.mkdir(parents=True, exist_ok=True)
    target = translated_dir / page_image_name(page_number, page_count, failed=failed)
    target.write_bytes(data if data is not None else _png_bytes(1200, 1600))
    return target


def _tree_fingerprint(directory: Path) -> dict[str, tuple[int, int]]:
    return {
        str(path.relative_to(directory)): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _pdf_settings(target_lang: str = "en") -> AppSettings:
    settings = AppSettings(target_lang=target_lang)
    settings.pdf.target_lang = target_lang
    settings.pdf.page_retry_attempts = 0
    settings.image_model_role.source_role = SOURCE_INDEPENDENT
    settings.image_model_role.cloud_provider = "custom_openai"
    settings.image_model_role.cloud_model = "image-model"
    settings.image_model_role.cloud_base_url = "https://images.example/v1"
    return settings


class _CountingImageClient:
    def __init__(self, image_bytes: bytes) -> None:
        self.image_bytes = image_bytes
        self.calls = 0

    def generate_page(self, **_kwargs) -> bytes:
        self.calls += 1
        return self.image_bytes


def _drain_all(runner: PdfImageTranslationRunner) -> list:
    # 队列只能被消费一次：同一个 runner 上需要多种消息时，必须先一次性取空，
    # 再按类型过滤，否则第二次 _drain 只会拿到空列表。
    messages = []
    while True:
        message = runner.get_message(timeout=0.01)
        if message is None:
            break
        messages.append(message)
    return messages


def _drain(runner: PdfImageTranslationRunner, message_type):
    return [m for m in _drain_all(runner) if isinstance(m, message_type)]


class ResumeArchiveReadingTests(unittest.TestCase):
    """读档层：什么页可以复用、什么目录直接判不可用。"""

    def test_unmanaged_or_missing_dir_reads_no_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIsNone(read_pdf_resume_manifest(root / "nope"))
            plain = root / "plain"
            plain.mkdir()
            self.assertIsNone(read_pdf_resume_manifest(plain))

    def test_corrupt_manifest_reads_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = Path(tmp) / "docs_翻译输出_20260828_154200"
            history.mkdir()
            (history / PDF_MANIFEST_FILENAME).write_text("{not json", encoding="utf-8")
            self.assertIsNone(read_pdf_resume_manifest(history))

            (history / PDF_MANIFEST_FILENAME).write_text("[]", encoding="utf-8")
            self.assertIsNone(read_pdf_resume_manifest(history))

    def test_only_success_pages_with_real_images_are_reusable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = _write_history_package(
                root,
                page_count=5,
                pages=[
                    _history_page(1),
                    # 中止时被烧成的占位页：零模型调用就进了上一份产物。
                    _history_page(2, status="placeholder", placeholder=True),
                    # 清单说成功，页图却不在了。
                    _history_page(3),
                    _history_page(4, quality_flags=["near_blank"]),
                    _history_page(5, review_status="failed"),
                ],
            )
            _place_history_page(history, 1, 5)
            _place_history_page(history, 2, 5, failed=True)
            _place_history_page(history, 4, 5)
            _place_history_page(history, 5, 5)

            manifest = read_pdf_resume_manifest(history)
            self.assertIsNotNone(manifest)
            reusable = reusable_pdf_pages(manifest, history)

            self.assertEqual(
                [page.page_number for page in reusable[_RELATIVE_PDF]],
                [1],
            )

    def test_truncated_page_image_is_rejected_only_when_verified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = _write_history_package(
                root,
                page_count=1,
                pages=[_history_page(1)],
            )
            data = _png_bytes(1200, 1600)
            _place_history_page(history, 1, 1, data=data[: len(data) // 2])
            manifest = read_pdf_resume_manifest(history)

            self.assertEqual(reusable_pdf_pages(manifest, history), {})
            # 扫描期的「上次跑到哪了」只数页数，不能为此把每张图都解码一遍。
            self.assertEqual(
                [
                    page.page_number
                    for page in reusable_pdf_pages(
                        manifest,
                        history,
                        verify_images=False,
                    )[_RELATIVE_PDF]
                ],
                [1],
            )

    def test_page_image_outside_the_archive_is_never_followed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside.png"
            outside.write_bytes(_png_bytes(1200, 1600))
            history = _write_history_package(
                root,
                page_count=1,
                pages=[_history_page(1, translated_image_path=str(outside))],
            )
            manifest = read_pdf_resume_manifest(history)

            self.assertEqual(reusable_pdf_pages(manifest, history), {})

            # 存档里真有这一页时，清单里那条越界路径也不该被用上。
            archived = _place_history_page(history, 1, 1)
            picked = reusable_pdf_pages(manifest, history)[_RELATIVE_PDF][0]
            self.assertEqual(picked.translated_image_path, archived)

    def test_pdf_page_without_geometry_is_not_reusable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = _write_history_package(
                root,
                page_count=1,
                pages=[_history_page(1, page_width_pt=0.0, page_height_pt=0.0)],
            )
            _place_history_page(history, 1, 1)
            self.assertEqual(reusable_pdf_pages(read_pdf_resume_manifest(history), history), {})

    def test_image_file_page_without_geometry_is_still_reusable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = _write_history_package(
                root,
                page_count=1,
                source_type=SOURCE_TYPE_IMAGE,
                relative_path="images/diagram.png",
                pages=[_history_page(1, page_width_pt=0.0, page_height_pt=0.0)],
            )
            _place_history_page(history, 1, 1, relative_path="images/diagram.png")
            reusable = reusable_pdf_pages(read_pdf_resume_manifest(history), history)
            self.assertEqual([page.page_number for page in reusable["images/diagram.png"]], [1])


class PdfResumeRunTests(unittest.TestCase):
    """执行层：复用页不再调模型，历史目录一个字节都不动。"""

    def _run_resume(
        self,
        root: Path,
        *,
        resume_output_dir: str | None,
        page_count: int = 3,
        target_lang: str = "en",
    ) -> tuple[PdfImageTranslationRunner, _CountingImageClient]:
        source_pdf = root / _RELATIVE_PDF
        _write_source_pdf(source_pdf, page_count)
        settings = _pdf_settings(target_lang)
        settings.pdf_output.use_custom_output_dir = True
        settings.pdf_output.custom_output_dir = str(root / "out")
        client = _CountingImageClient(_png_bytes(1200, 1600))
        runner = PdfImageTranslationRunner(
            [
                PdfFileItem(
                    path=source_pdf,
                    name="source",
                    size_kb=1.0,
                    page_count=page_count,
                )
            ],
            settings,
            source_root=root,
            image_client=client,
            task_logger_enabled=False,
            resume_output_dir=resume_output_dir,
        )
        with patch("core.model_roles.get_key", return_value="secret"):
            runner._run()
        return runner, client

    def test_finished_pages_are_reused_and_never_billed_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_pdf = root / _RELATIVE_PDF
            _write_source_pdf(source_pdf, 3)
            history = _write_history_package(
                root / "history",
                page_count=3,
                source_size_bytes=source_pdf.stat().st_size,
                pages=[
                    _history_page(1),
                    _history_page(2, status="placeholder", placeholder=True),
                    _history_page(3),
                ],
            )
            _place_history_page(history, 1, 3)
            _place_history_page(history, 2, 3, failed=True)
            before = _tree_fingerprint(history)

            runner, client = self._run_resume(root, resume_output_dir=str(history))

            messages = _drain_all(runner)
            done = [m for m in messages if isinstance(m, DoneMsg)][-1]
            logs = "\n".join(m.message for m in messages if isinstance(m, LogMsg))
            self.assertEqual(client.calls, 2)
            self.assertEqual(done.api_call_count, 2)
            self.assertIn("复用上次已完成页 1 页", logs)
            self.assertIn("本次只需生成 2 页", logs)

            output_dir = Path(done.output_dir)
            manifest = json.loads(
                (output_dir / PDF_MANIFEST_FILENAME).read_text(encoding="utf-8")
            )
            file_entry = manifest["files"][0]
            self.assertEqual(file_entry["generated_page_count"], 3)
            self.assertEqual(file_entry["placeholder_page_count"], 0)
            self.assertEqual(file_entry["retry_count"], 0)
            reused_page = file_entry["pages"][0]
            self.assertEqual(reused_page["status"], "success")
            self.assertEqual(reused_page["attempts"], 1)
            # 复用页的所有路径都必须落在本次任务自己的目录里。
            self.assertTrue(
                Path(reused_page["translated_image_path"]).is_relative_to(output_dir)
            )
            self.assertTrue(Path(reused_page["translated_image_path"]).is_file())
            self.assertTrue(Path(file_entry["translated_pdf_path"]).is_file())
            self.assertEqual(_tree_fingerprint(history), before)

    def test_unreadable_resume_dir_falls_back_to_a_full_translation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = root / "history" / "docs_翻译输出_20260828_154200"
            history.mkdir(parents=True)
            (history / PDF_MANIFEST_FILENAME).write_text("{broken", encoding="utf-8")

            runner, client = self._run_resume(root, resume_output_dir=str(history))

            messages = _drain_all(runner)
            done = [m for m in messages if isinstance(m, DoneMsg)][-1]
            logs = "\n".join(m.message for m in messages if isinstance(m, LogMsg))
            self.assertEqual(client.calls, 3)
            self.assertIn("本次全部重新翻译", logs)
            self.assertTrue(Path(done.output_dir).is_dir())

    def test_source_page_count_change_reuses_nothing_from_that_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = _write_history_package(
                root / "history",
                page_count=2,
                pages=[_history_page(1), _history_page(2)],
            )
            _place_history_page(history, 1, 2)
            _place_history_page(history, 2, 2)

            runner, client = self._run_resume(root, resume_output_dir=str(history))

            logs = "\n".join(message.message for message in _drain(runner, LogMsg))
            self.assertEqual(client.calls, 3)
            self.assertIn("整份重新翻译", logs)

    def test_target_language_change_reuses_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = _write_history_package(
                root / "history",
                page_count=3,
                target_lang="fr",
                pages=[_history_page(1), _history_page(2), _history_page(3)],
            )
            for page_number in (1, 2, 3):
                _place_history_page(history, page_number, 3)

            runner, client = self._run_resume(root, resume_output_dir=str(history))

            logs = "\n".join(message.message for message in _drain(runner, LogMsg))
            self.assertEqual(client.calls, 3)
            self.assertIn("目标语言", logs)

    def test_no_resume_dir_keeps_the_normal_full_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner, client = self._run_resume(root, resume_output_dir=None)

            messages = _drain_all(runner)
            done = [m for m in messages if isinstance(m, DoneMsg)][-1]
            logs = "\n".join(m.message for m in messages if isinstance(m, LogMsg))
            self.assertEqual(client.calls, 3)
            self.assertNotIn("续译", logs)
            self.assertTrue(Path(done.output_dir).is_dir())


if __name__ == "__main__":
    unittest.main()
