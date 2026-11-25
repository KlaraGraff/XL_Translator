"""按文件名拼路径时 Windows 的三条限制（反馈 #32：以点结尾的文件名报「文件不存在」）。

最后一个用例按生产路径真跑一份名叫「DEVIS CHINA CONSTRUCTION...pdf」的 PDF。
它在 Windows 上才真正有意义：PR 检查里的 Windows 任务专门跑这个文件。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from core.model_roles import ROLE_IMAGE, SOURCE_INDEPENDENT, resolve_effective_model_config
from core.pdf_image_translation import (
    PDF_OUTPUT_STATE_COMPLETED,
    PdfFileItem,
    PdfImageTranslationRunner,
    max_page_generation_attempts,
    resolve_pdf_page_archive_dirs,
    resolve_pdf_review_candidates_dir,
)
from core.safe_filename import (
    clean_filename_fragment,
    is_windows_reserved_name,
    safe_path_component,
    safe_relative_path,
)
from settings import AppSettings
from tests.test_pdf_image_translation import _FakeImageClient, _png_bytes, _run_single_file

DOTTED_NAME = "DEVIS CHINA CONSTRUCTION...pdf"


def _windows_safe(part: str) -> bool:
    return (
        part == part.rstrip(". ")
        and not is_windows_reserved_name(part)
        and not any(ch in part for ch in '<>:"/\\|?*')
    )


class SafeFilenameTests(unittest.TestCase):
    def test_trailing_dots_and_spaces_become_underscores(self) -> None:
        self.assertEqual(safe_path_component("DEVIS CHINA CONSTRUCTION.."), "DEVIS CHINA CONSTRUCTION__")
        self.assertEqual(safe_path_component("报价 "), "报价_")
        # 「a..」和「a.」不能挤进同一个文件夹。
        self.assertNotEqual(safe_path_component("a.."), safe_path_component("a."))
        self.assertEqual(safe_path_component("normal name"), "normal name")
        self.assertEqual(safe_path_component("v1.2 final"), "v1.2 final")

    def test_illegal_characters_and_empty_names(self) -> None:
        self.assertEqual(safe_path_component('a<b>:c"d|e?f*'), "a_b_c_d_e_f_")
        for value in ("", "   ", ".", "..", "..."):
            self.assertEqual(safe_path_component(value, fallback="未命名"), "未命名")

    def test_reserved_device_names(self) -> None:
        for name in ("CON", "con", "Nul", "COM1", "lpt9", "AUX.txt", "PRN.tar.gz"):
            with self.subTest(name=name):
                self.assertTrue(is_windows_reserved_name(name))
                self.assertFalse(is_windows_reserved_name(safe_path_component(name)))
        self.assertEqual(safe_path_component("CON"), "CON_")
        self.assertEqual(safe_path_component("aux.txt"), "aux_.txt")
        for name in ("CONTRACT", "COM10", "console", "NULL"):
            self.assertFalse(is_windows_reserved_name(name))
            self.assertEqual(safe_path_component(name), name)

    def test_relative_path_is_cleaned_part_by_part(self) -> None:
        self.assertEqual(
            safe_relative_path(Path("合同. ") / "CON" / "DEVIS CHINA CONSTRUCTION.."),
            Path("合同__") / "CON_" / "DEVIS CHINA CONSTRUCTION__",
        )
        self.assertEqual(safe_relative_path(Path("")), Path("_"))

    def test_fragment_cleanup_keeps_old_behaviour(self) -> None:
        self.assertEqual(clean_filename_fragment("法语. ", "目标语言"), "法语")
        self.assertEqual(clean_filename_fragment("  ", "目标语言"), "目标语言")
        self.assertEqual(clean_filename_fragment("a/b", "x"), "a_b")


class PdfPageArchiveNameTests(unittest.TestCase):
    def test_archive_dirs_never_end_with_dot_or_space(self) -> None:
        output_dir = Path("/out/package")
        relative = Path("报价 .") / DOTTED_NAME
        dirs = (
            *resolve_pdf_page_archive_dirs(output_dir, relative),
            resolve_pdf_review_candidates_dir(output_dir, relative),
        )
        for directory in dirs:
            parts = directory.relative_to(output_dir).parts
            self.assertTrue(all(_windows_safe(part) for part in parts), parts)
            self.assertEqual(directory.name, "DEVIS CHINA CONSTRUCTION__")

    def test_ordinary_names_keep_their_old_folder(self) -> None:
        source_dir, _ = resolve_pdf_page_archive_dirs(Path("/out"), Path("section/source.pdf"))
        self.assertEqual(source_dir, Path("/out/_pdf_pages/source_pages/section/source"))

    @unittest.skipIf(os.name == "nt", "Windows 上旧名字本来就建不出来")
    def test_old_unsanitized_archive_from_macos_is_still_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            legacy = output_dir / "_pdf_pages" / "source_pages" / "DEVIS CHINA CONSTRUCTION.."
            legacy.mkdir(parents=True)
            source_dir, translated_dir = resolve_pdf_page_archive_dirs(output_dir, Path(DOTTED_NAME))
            self.assertEqual(source_dir, legacy)
            # 没有旧文件夹的那一类照样用新名字。
            self.assertEqual(translated_dir.name, "DEVIS CHINA CONSTRUCTION__")


class DottedFileNamePipelineTests(unittest.TestCase):
    def test_pdf_named_with_trailing_dots_translates_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_pdf = root / DOTTED_NAME
            Image.new("RGB", (1224, 1584), "white").save(source_pdf, format="PDF", resolution=144.0)
            settings = AppSettings(target_lang="fr")
            settings.image_model_role.source_role = SOURCE_INDEPENDENT
            settings.image_model_role.cloud_provider = "custom_openai"
            settings.image_model_role.cloud_model = "image-model"
            settings.image_model_role.cloud_base_url = "https://images.example/v1"
            runner = PdfImageTranslationRunner(
                [PdfFileItem(path=source_pdf, name=source_pdf.stem, size_kb=1.0, page_count=1)],
                settings,
                source_root=root,
                image_client=_FakeImageClient(_png_bytes(1224, 1584)),
                task_logger_enabled=False,
            )
            output_dir = root / "out"

            with patch("core.model_roles.get_key", return_value="secret"):
                record = _run_single_file(
                    runner,
                    output_dir=output_dir,
                    max_attempts=max_page_generation_attempts(0),
                    model_config=resolve_effective_model_config(settings, ROLE_IMAGE),
                )

            self.assertEqual(record.status, PDF_OUTPUT_STATE_COMPLETED, record.error)
            self.assertTrue(Path(record.translated_pdf_path).is_file())
            self.assertTrue(Path(record.compressed_pdf_path).is_file())
            pages = [Path(page.source_image_path) for page in record.pages]
            self.assertTrue(pages and all(page.is_file() for page in pages), pages)
            for path in root.rglob("*"):
                if path.is_dir():
                    self.assertTrue(_windows_safe(path.name), path)


if __name__ == "__main__":
    unittest.main()
