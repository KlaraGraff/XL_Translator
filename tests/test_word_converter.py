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
            # 提示语按平台给：「代码签名」只在 macOS 上说得通，这里固定成 macOS 场景。
            patch.object(word_converter.platform, "system", return_value="Darwin"),
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

    def test_libreoffice_convert_to_uses_isolated_user_profile(self) -> None:
        """``--convert-to`` 必须带一份独立的 ``-env:UserInstallation``。

        两个 soffice 进程不能共享用户配置目录：用户开着 LibreOffice 图形界面时，
        headless 转换会静默失败。同一文件里的 UNO 编号路径、xls_converter、
        ppt_converter 都已经是这个做法，只有这里的 ``--convert-to`` 漏了。
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "source.doc"
            source_path.write_bytes(b"legacy word payload")

            commands: list[list[str]] = []
            profiles: list[Path] = []

            def _fake_run(command, **_kwargs):
                commands.append([str(part) for part in command])
                profile_args = [
                    part for part in command if part.startswith("-env:UserInstallation=")
                ]
                # 没有配置隔离时也照常「转换成功」，好让断言报出真正的原因。
                if profile_args:
                    profile = Path(profile_args[0].split("=", 1)[1][len("file://"):])
                    profiles.append(profile)
                    # 隔离出来的配置目录必须先被建出来，soffice 才肯用它。
                    self.assertTrue(profile.is_dir(), f"临时配置目录不存在：{profile}")
                outdir = Path(command[command.index("--outdir") + 1])
                (outdir / f"{source_path.stem}.docx").write_bytes(b"PK\x03\x04 fake docx")
                return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

            # 连续转两次：每次都该拿到自己的一份配置，而不是复用上一份。
            with (
                patch.object(word_converter, "_find_soffice", return_value=root / "soffice"),
                patch.object(word_converter, "_run_command", side_effect=_fake_run),
            ):
                for _ in range(2):
                    word_converter.convert_with_libreoffice(source_path)

        self.assertEqual(len(commands), 2)
        for command in commands:
            self.assertTrue(
                any(part.startswith("-env:UserInstallation=") for part in command),
                f"缺少配置隔离：{command}",
            )
            self.assertIn("--convert-to", command)
        # 每次转换用的是自己新建的临时目录，用完整棵树都删掉。
        self.assertIn("word_translator_lo_", str(profiles[0]))
        self.assertNotEqual(profiles[0].parent, profiles[1].parent, "两次转换复用了同一份配置目录")
        # 临时配置目录属于这次转换，用完必须删掉，不能堆在临时目录里。
        for profile in profiles:
            self.assertFalse(profile.exists(), f"临时配置目录没有清理：{profile}")

    @staticmethod
    def _build_docx(path: Path) -> None:
        doc = Document()
        doc.add_paragraph("项目名称：测试工程")
        doc.save(str(path))


if __name__ == "__main__":
    unittest.main(verbosity=2)
