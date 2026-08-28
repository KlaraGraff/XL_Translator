from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from docx import Document

from core import word_converter
from core.word_converter import (
    WordConversionError,
    convert_doc_to_docx,
    convert_numbering_to_text_with_native_apps,
)


class WordConverterTests(unittest.TestCase):
    def test_doc_conversion_falls_back_only_after_explicit_confirmation(self) -> None:
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
                with self.assertRaises(WordConversionError):
                    convert_doc_to_docx(
                        source_path,
                        prefer_native_word=True,
                        allow_compatibility_fallback=False,
                    )
                result = convert_doc_to_docx(
                    source_path,
                    prefer_native_word=True,
                    allow_compatibility_fallback=True,
                )

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
                result = convert_doc_to_docx(
                    source_path,
                    prefer_native_word=False,
                    allow_compatibility_fallback=True,
                )

            self.assertEqual(result.method, "LibreOffice")
            self.assertEqual(native_calls, [])

    def test_numbering_preprocess_falls_back_after_native_word_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "source.docx"
            converted_path = root / "numbering.docx"
            self._build_docx(source_path)
            self._build_docx(converted_path)

            def _missing_word(_path):
                raise WordConversionError("Word missing")

            with patch.dict(
                convert_numbering_to_text_with_native_apps.__globals__,
                {
                    "convert_numbering_to_text_with_native_word": _missing_word,
                    "convert_numbering_to_text_with_libreoffice": lambda _path: converted_path,
                },
            ):
                result = convert_numbering_to_text_with_native_apps(
                    source_path,
                    prefer_native_word=True,
                )

            self.assertEqual(result.path, converted_path)
            self.assertEqual(result.method, "LibreOffice")
            self.assertIn("Word missing", result.fallback_messages[0])

    def test_numbering_preprocess_can_skip_native_word(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "source.docx"
            converted_path = root / "numbering.docx"
            self._build_docx(source_path)
            self._build_docx(converted_path)
            native_calls = []

            def _native_word(_path):
                native_calls.append(_path)
                return converted_path

            with patch.dict(
                convert_numbering_to_text_with_native_apps.__globals__,
                {
                    "convert_numbering_to_text_with_native_word": _native_word,
                    "convert_numbering_to_text_with_libreoffice": lambda _path: converted_path,
                },
            ):
                result = convert_numbering_to_text_with_native_apps(
                    source_path,
                    prefer_native_word=False,
                )

            self.assertEqual(result.method, "LibreOffice")
            self.assertEqual(native_calls, [])

    def test_killed_libreoffice_python_is_not_retried_and_is_cached(self) -> None:
        """被信号杀掉的解释器只探一次：不重试、不重复探。

        真实场景是 macOS 以代码签名为由 SIGKILL 掉 LibreOffice 自带的 Python。
        原来的实现每秒重试一次、连试 90 秒，把一次启动失败放大成几十份系统崩溃报告
        和一叠「意外退出」对话框，而这些等待最终什么也换不到——照样退回 Python 兜底。
        """
        word_converter._LIBREOFFICE_PYTHON_PROBE_CACHE.clear()
        self.addCleanup(word_converter._LIBREOFFICE_PYTHON_PROBE_CACHE.clear)

        calls: list[list[str]] = []

        class _Killed:
            returncode = -9
            stdout = ""
            stderr = ""

        def _fake_run(command, **_kwargs):
            calls.append([str(part) for part in command])
            return _Killed()

        python_path = Path("/Applications/LibreOffice.app/Contents/Resources/python")
        # 把 codesign 预检按掉，模拟「签名里没读出限制、真启动后才被杀」的路径。
        with (
            patch.object(word_converter.subprocess, "run", _fake_run),
            patch.object(
                word_converter,
                "_libreoffice_python_launch_constraint_message",
                return_value=None,
            ),
        ):
            for _ in range(4):
                with self.assertRaises(WordConversionError) as ctx:
                    word_converter._ensure_libreoffice_python_runnable(python_path)

        # 四次调用只真正启动过一次解释器，其余三次走缓存。
        self.assertEqual(len(calls), 1)
        self.assertIn("代码签名", str(ctx.exception))

    def test_launch_constrained_python_is_refused_without_launching(self) -> None:
        """签名里带启动限制的解释器：一次都不能启动，读签名就判死，并缓存结论。

        新版 LibreOffice 给内置 Python 签了「父进程必须是 LibreOffice」的启动限制，
        外部程序一启动它就被 macOS SIGKILL——每启动一次，用户桌面就多弹一个
        「LibreOfficePython 意外退出」对话框。codesign 读签名不启动任何东西。
        """
        word_converter._LIBREOFFICE_PYTHON_PROBE_CACHE.clear()
        self.addCleanup(word_converter._LIBREOFFICE_PYTHON_PROBE_CACHE.clear)

        calls: list[list[str]] = []

        class _CodesignListing:
            returncode = 0
            stdout = ""
            # codesign --display 的清单走 stderr；真机上的关键行就是这句。
            stderr = "Launch Constraints:\n\tHas Parent Launch Constraints\n"

        def _fake_run(command, **_kwargs):
            command_parts = [str(part) for part in command]
            calls.append(command_parts)
            if command_parts[0] != "/usr/bin/codesign":
                self.fail(f"不该启动解释器，却启动了：{command_parts}")
            return _CodesignListing()

        python_path = Path("/Applications/LibreOffice.app/Contents/Resources/python")
        bundle = Path(
            "/Applications/LibreOffice.app/Contents/Frameworks/"
            "LibreOfficePython.framework/Versions/Current/Resources/Python.app"
        )
        with (
            patch.object(word_converter.subprocess, "run", _fake_run),
            patch.object(word_converter.platform, "system", return_value="Darwin"),
            patch.object(
                word_converter,
                "_find_libreoffice_python_app_bundle",
                return_value=bundle,
            ),
        ):
            for _ in range(4):
                with self.assertRaises(WordConversionError) as ctx:
                    word_converter._ensure_libreoffice_python_runnable(python_path)

        # 四次调用只问过一次 codesign，其余三次走缓存；解释器一次都没被启动。
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "/usr/bin/codesign")
        self.assertIn("启动限制", str(ctx.exception))

    @staticmethod
    def _build_docx(path: Path) -> None:
        doc = Document()
        doc.add_paragraph("项目名称：测试工程")
        doc.save(str(path))


if __name__ == "__main__":
    unittest.main(verbosity=2)
