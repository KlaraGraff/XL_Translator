from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from docx import Document

from core.word_converter import (
    WordConversionError,
    convert_doc_to_docx,
)


class WordConverterTests(unittest.TestCase):
    def test_doc_conversion_falls_back_after_native_word_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "source.doc"
            converted_path = root / "converted.docx"
            source_path.write_bytes(b"legacy word payload")
            self._build_docx(converted_path)

            def _missing_word(_path):
                raise WordConversionError("Word missing")

            with (
                patch.dict(
                    convert_doc_to_docx.__globals__,
                    {
                        "convert_with_native_word": _missing_word,
                        "convert_with_libreoffice": lambda _path: converted_path,
                    },
                ),
                patch.object(
                    convert_doc_to_docx.__globals__["platform"],
                    "system",
                    return_value="Windows",
                ),
            ):
                result = convert_doc_to_docx(source_path, prefer_native_word=True)

            self.assertEqual(result.path, converted_path)
            self.assertEqual(result.method, "LibreOffice")
            self.assertIn("Word missing", result.fallback_messages[0])

    def test_doc_conversion_can_skip_native_word(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "source.doc"
            converted_path = root / "converted.docx"
            source_path.write_bytes(b"legacy word payload")
            self._build_docx(converted_path)

            native_calls = []

            def _native_word(_path):
                native_calls.append(_path)
                return converted_path

            with (
                patch.dict(
                    convert_doc_to_docx.__globals__,
                    {
                        "convert_with_native_word": _native_word,
                        "convert_with_libreoffice": lambda _path: converted_path,
                    },
                ),
                patch.object(
                    convert_doc_to_docx.__globals__["platform"],
                    "system",
                    return_value="Windows",
                ),
            ):
                result = convert_doc_to_docx(source_path, prefer_native_word=False)

            self.assertEqual(result.method, "LibreOffice")
            self.assertEqual(native_calls, [])

    @staticmethod
    def _build_docx(path: Path) -> None:
        doc = Document()
        doc.add_paragraph("项目名称：测试工程")
        doc.save(str(path))


if __name__ == "__main__":
    unittest.main(verbosity=2)
