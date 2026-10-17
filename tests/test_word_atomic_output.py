"""A failed Word write must not expose an original or a partial DOCX as output."""

from __future__ import annotations

import io
import shutil
import struct
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path
from unittest.mock import patch

from docx import Document
from docx.document import Document as DocumentClass
from lxml import etree

from core.ooxml_package_check import PackagePartsChanged, changed_untouched_parts

from core.word_coverage import (
    apply_coverage_review_marks,
    build_word_coverage_plan,
    write_untranslated_docx,
)
from core.word_document import write_bilingual_docx


class WordAtomicOutputTests(unittest.TestCase):
    def _paths(self, root: Path) -> tuple[Path, Path]:
        source = root / "source.docx"
        document = Document()
        document.add_paragraph("项目名称")
        document.save(source)
        output_dir = root / "out"
        output_dir.mkdir()
        return source, output_dir

    def _write(self, kind: str, source: Path, output_dir: Path) -> Path:
        options = {
            "source_path": source,
            "output_dir": output_dir,
            "translations": {"项目名称": "Project name"},
            "target_lang": "en",
        }
        if kind == "bilingual":
            return write_bilingual_docx(**options)
        plan = build_word_coverage_plan(source, target_lang="en")
        return write_untranslated_docx(**options, plan=plan)

    def test_partial_save_preserves_existing_output_and_cleans_staging(self) -> None:
        for kind in ("bilingual", "untranslated"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                source, output_dir = self._paths(Path(tmp))
                final = self._write(kind, source, output_dir)
                previous = final.read_bytes()

                def partial_save(_document, path):
                    Path(path).write_bytes(b"partial Word package")
                    raise OSError("disk full during save")

                with patch.object(DocumentClass, "save", partial_save):
                    with self.assertRaisesRegex(OSError, "disk full"):
                        self._write(kind, source, output_dir)

                self.assertEqual(final.read_bytes(), previous)
                self.assertEqual(list(output_dir.iterdir()), [final])

    def test_failed_write_leaves_no_apparent_output(self) -> None:
        for kind in ("bilingual", "untranslated"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                source, output_dir = self._paths(Path(tmp))
                with patch.object(DocumentClass, "save", side_effect=OSError("save failed")):
                    with self.assertRaisesRegex(OSError, "save failed"):
                        self._write(kind, source, output_dir)
                self.assertEqual(list(output_dir.iterdir()), [])

    def test_unopenable_saved_package_does_not_replace_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source, output_dir = self._paths(Path(tmp))
            final = self._write("bilingual", source, output_dir)
            previous = final.read_bytes()

            def corrupt_save(_document, path):
                Path(path).write_bytes(b"not a DOCX")

            with patch.object(DocumentClass, "save", corrupt_save):
                with self.assertRaises(Exception):
                    self._write("bilingual", source, output_dir)

            self.assertEqual(final.read_bytes(), previous)
            self.assertEqual(list(output_dir.iterdir()), [final])

    def test_successful_write_replaces_output_and_removes_staging(self) -> None:
        for kind in ("bilingual", "untranslated"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                source, output_dir = self._paths(Path(tmp))
                final = self._write(kind, source, output_dir)
                self.assertIn("Project name", [p.text for p in Document(final).paragraphs])
                self.assertEqual(list(output_dir.iterdir()), [final])

    def test_logging_callback_failure_does_not_turn_committed_file_into_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source, output_dir = self._paths(Path(tmp))
            final = write_bilingual_docx(
                source_path=source,
                output_dir=output_dir,
                translations={"项目名称": "Project name"},
                target_lang="en",
                log_callback=lambda _message: (_ for _ in ()).throw(RuntimeError("log unavailable")),
            )
            self.assertIn("Project name", [p.text for p in Document(final).paragraphs])

    def test_review_mark_save_failure_preserves_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source, output_dir = self._paths(Path(tmp))
            plan = build_word_coverage_plan(source, target_lang="en")
            final = output_dir / "review.docx"
            final.write_bytes(source.read_bytes())
            previous = final.read_bytes()

            def partial_save(_document, path):
                Path(path).write_bytes(b"partial review package")
                raise OSError("review save failed")

            with patch.object(DocumentClass, "save", partial_save):
                with self.assertRaisesRegex(OSError, "review save failed"):
                    apply_coverage_review_marks(final, plan=plan)

            self.assertEqual(final.read_bytes(), previous)
            self.assertEqual(list(output_dir.iterdir()), [final])


def _png() -> bytes:
    raw = b"".join(b"\x00" + b"\x30\x80\xd0" * 4 for _ in range(4))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _rewrite_entry(package: Path, name: str, blob: bytes | None) -> None:
    """改写（blob 为 None 时删掉）docx 里的一个部件。"""
    with zipfile.ZipFile(package) as original:
        entries = [(info, original.read(info.filename)) for info in original.infolist()]
    with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as rewritten:
        for info, data in entries:
            if info.filename != name:
                rewritten.writestr(info, data)
            elif blob is not None:
                rewritten.writestr(info, blob)


class UntouchedPartsCheckTests(unittest.TestCase):
    """保存后核对：没改的部件（图片、主题……）和原文件不一致时不替换输出、报出部件名。"""

    def _source(self, root: Path) -> Path:
        source = root / "source.docx"
        document = Document()
        document.add_paragraph("项目名称")
        document.add_paragraph().add_run().add_picture(io.BytesIO(_png()))
        document.save(source)
        return source

    def _media_name(self, package: Path) -> str:
        with zipfile.ZipFile(package) as opened:
            return next(name for name in opened.namelist() if name.startswith("word/media/"))

    def test_normal_write_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = self._source(Path(tmp))
            final = write_bilingual_docx(
                source_path=source, output_dir=Path(tmp) / "out", translations={"项目名称": "Project name"},
                target_lang="en", output_translation_only=True,
            )
            self.assertEqual(changed_untouched_parts(source, final, lambda _n, _t: False), ["word/document.xml"])

    def test_changed_image_blocks_the_output(self) -> None:
        for kind in ("bilingual", "untranslated"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                source = self._source(Path(tmp))
                output_dir = Path(tmp) / "out"
                media = self._media_name(source)
                real_save = DocumentClass.save

                def save_and_damage(document, path):
                    real_save(document, path)
                    _rewrite_entry(Path(path), media, b"not the original picture")

                options = {
                    "source_path": source, "output_dir": output_dir,
                    "translations": {"项目名称": "Project name"}, "target_lang": "en",
                }
                if kind == "untranslated":
                    options["plan"] = build_word_coverage_plan(source, target_lang="en")
                writer = write_bilingual_docx if kind == "bilingual" else write_untranslated_docx
                with patch.object(DocumentClass, "save", save_and_damage):
                    with self.assertRaises(PackagePartsChanged) as caught:
                        writer(**options)
                self.assertEqual(caught.exception.parts, [media])
                self.assertIn(media, str(caught.exception))
                self.assertEqual(list(output_dir.iterdir()), [])

    def test_reserialized_xml_counts_as_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = self._source(Path(tmp))
            copy = Path(tmp) / "copy.docx"
            shutil.copy(source, copy)
            with zipfile.ZipFile(copy) as opened:
                theme = opened.read("word/theme/theme1.xml")
            # 换一种 XML 声明写法、属性改用单引号：内容一样
            reserialized = etree.tostring(etree.fromstring(theme), xml_declaration=True, encoding="UTF-8")
            self.assertNotEqual(reserialized, theme)
            _rewrite_entry(copy, "word/theme/theme1.xml", reserialized)
            self.assertEqual(changed_untouched_parts(source, copy, lambda _n, _t: False), [])

    def test_missing_parts_only_count_when_referenced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = self._source(Path(tmp))
            orphan_source = Path(tmp) / "orphan.docx"
            shutil.copy(source, orphan_source)
            with zipfile.ZipFile(orphan_source, "a") as opened:
                opened.writestr("customXml/leftover.xml", b"<x/>")
            dropped = Path(tmp) / "dropped.docx"
            shutil.copy(source, dropped)
            media = self._media_name(source)
            _rewrite_entry(dropped, media, None)
            self.assertEqual(changed_untouched_parts(orphan_source, source, lambda _n, _t: False), [])
            self.assertEqual(changed_untouched_parts(source, dropped, lambda _n, _t: False), [media])


if __name__ == "__main__":
    unittest.main()
