"""Legacy Word document conversion helpers."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from docx import Document
from loguru import logger


DOCX_FILE_FORMAT = 16
WORD_CONVERSION_TIMEOUT_SECONDS = 180
LIBREOFFICE_UNO_TIMEOUT_SECONDS = 90


class WordConversionError(Exception):
    """Raised when a legacy Word document cannot be converted to DOCX."""


@dataclass(frozen=True)
class WordConversionResult:
    path: Path
    method: str
    fallback_messages: list[str] = field(default_factory=list)


def is_legacy_word_doc(path: str | Path) -> bool:
    """Return whether a path points to an old binary .doc document."""
    return Path(path).suffix.lower() == ".doc"


def convert_doc_to_docx(
    doc_path: str | Path,
    *,
    prefer_native_word: bool = True,
) -> WordConversionResult:
    """Convert a .doc file to a temporary .docx, with quiet fallback strategies."""
    source_path = Path(doc_path)
    if not is_legacy_word_doc(source_path):
        raise WordConversionError(f"不是旧版 .doc 文件：{source_path}")

    attempts: list[tuple[str, object]] = []
    if prefer_native_word:
        attempts.append(("本地 Word", convert_with_native_word))
    attempts.append(("LibreOffice", convert_with_libreoffice))
    if platform.system() == "Darwin":
        attempts.append(("macOS textutil", convert_with_textutil))

    errors: list[str] = []
    for method_name, converter in attempts:
        try:
            output_path = converter(source_path)
            _validate_docx(output_path)
            logger.info(f".doc 转换成功：{source_path.name} -> {output_path.name} ({method_name})")
            return WordConversionResult(
                path=output_path,
                method=method_name,
                fallback_messages=list(errors),
            )
        except Exception as exc:  # noqa: BLE001 - each strategy falls through.
            errors.append(f"{method_name} 不可用：{exc}")
            logger.info(f".doc 转换策略不可用 {source_path.name} ({method_name}): {exc}")

    detail = "；".join(errors) if errors else "没有可用的转换策略"
    raise WordConversionError(
        "无法将旧版 .doc 转换为 .docx。"
        "请确认本机 Microsoft Word 或 LibreOffice 可用，"
        "或手动另存为 .docx 后再翻译。"
        f" 详情：{detail}"
    )


def convert_with_native_word(doc_path: str | Path) -> Path:
    """Convert .doc to .docx with Microsoft Word when supported locally."""
    system = platform.system()
    source_path = Path(doc_path)
    output_path = _get_temp_docx_path(source_path)
    if system == "Windows":
        _convert_with_windows_word(source_path, output_path)
        return output_path
    if system == "Darwin":
        _convert_with_macos_word(source_path, output_path)
        return output_path
    raise WordConversionError(f"当前平台 {system} 暂不支持本地 Word 自动化。")


def convert_with_libreoffice(doc_path: str | Path) -> Path:
    """Convert .doc to .docx with LibreOffice/soffice when installed."""
    source_path = Path(doc_path)
    soffice_path = _find_soffice()
    if soffice_path is None:
        raise WordConversionError("未找到 LibreOffice/soffice。")

    output_path = _get_temp_docx_path(source_path)
    work_dir = Path(tempfile.mkdtemp(prefix="word_translator_lo_"))
    try:
        command = [
            str(soffice_path),
            "--headless",
            "--convert-to",
            "docx",
            "--outdir",
            str(work_dir),
            str(source_path),
        ]
        _run_command(command, timeout=WORD_CONVERSION_TIMEOUT_SECONDS)
        converted_path = work_dir / f"{source_path.stem}.docx"
        if not converted_path.exists():
            candidates = list(work_dir.glob("*.docx"))
            if len(candidates) == 1:
                converted_path = candidates[0]
        if not converted_path.exists():
            raise WordConversionError("LibreOffice 未生成 .docx 输出。")
        shutil.move(str(converted_path), str(output_path))
        return output_path
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def convert_with_textutil(doc_path: str | Path) -> Path:
    """Convert .doc to .docx with macOS textutil."""
    if platform.system() != "Darwin":
        raise WordConversionError("textutil 仅在 macOS 上可用。")
    textutil_path = shutil.which("textutil") or "/usr/bin/textutil"
    if not Path(textutil_path).exists():
        raise WordConversionError("未找到 textutil。")
    source_path = Path(doc_path)
    output_path = _get_temp_docx_path(source_path)
    command = [
        textutil_path,
        "-convert",
        "docx",
        "-output",
        str(output_path),
        str(source_path),
    ]
    _run_command(command, timeout=WORD_CONVERSION_TIMEOUT_SECONDS)
    return output_path


def convert_numbering_to_text_with_native_apps(
    docx_path: str | Path,
    *,
    prefer_native_word: bool = True,
) -> WordConversionResult:
    """Convert DOCX automatic numbering to literal text using local office apps."""
    source_path = Path(docx_path)
    if source_path.suffix.lower() != ".docx":
        raise WordConversionError(f"不是 .docx 文件：{source_path}")

    attempts: list[tuple[str, object]] = []
    if prefer_native_word:
        attempts.append(("本地 Word", convert_numbering_to_text_with_native_word))
    attempts.append(("LibreOffice", convert_numbering_to_text_with_libreoffice))

    errors: list[str] = []
    for method_name, converter in attempts:
        try:
            output_path = converter(source_path)
            _validate_docx(output_path)
            logger.info(f"Word 编号预处理成功：{source_path.name} -> {output_path.name} ({method_name})")
            return WordConversionResult(
                path=output_path,
                method=method_name,
                fallback_messages=list(errors),
            )
        except Exception as exc:  # noqa: BLE001 - each strategy falls through.
            errors.append(f"{method_name} 不可用：{exc}")
            logger.info(f"Word 编号预处理策略不可用 {source_path.name} ({method_name}): {exc}")

    detail = "；".join(errors) if errors else "没有可用的预处理策略"
    raise WordConversionError(f"无法通过本地 Office 预处理 Word 自动编号。详情：{detail}")


def convert_numbering_to_text_with_native_word(docx_path: str | Path) -> Path:
    """Use Microsoft Word to turn automatic list numbering into document text."""
    system = platform.system()
    source_path = Path(docx_path)
    output_path = _get_temp_docx_path(source_path)
    if system == "Windows":
        _convert_numbering_with_windows_word(source_path, output_path)
        return output_path
    if system == "Darwin":
        _convert_numbering_with_macos_word(source_path, output_path)
        return output_path
    raise WordConversionError(f"当前平台 {system} 暂不支持本地 Word 自动编号预处理。")


def convert_numbering_to_text_with_libreoffice(docx_path: str | Path) -> Path:
    """Use LibreOffice UNO dispatch to convert automatic numbering to text."""
    source_path = Path(docx_path)
    soffice_path = _find_soffice()
    if soffice_path is None:
        raise WordConversionError("未找到 LibreOffice/soffice。")

    output_path = _get_temp_docx_path(source_path)
    work_dir = Path(tempfile.mkdtemp(prefix="word_translator_lo_uno_"))
    profile_dir = work_dir / "profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    port = 20000 + (uuid.uuid4().int % 20000)
    accept_arg = f"socket,host=127.0.0.1,port={port};urp;StarOffice.ComponentContext"
    command = [
        str(soffice_path),
        "--headless",
        "--norestore",
        "--nodefault",
        "--nofirststartwizard",
        f"-env:UserInstallation={profile_dir.as_uri()}",
        f"--accept={accept_arg}",
    ]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        script_path = work_dir / "convert_numbering.py"
        script_path.write_text(_LIBREOFFICE_NUMBERING_SCRIPT, encoding="utf-8")
        python_path = _find_libreoffice_python(soffice_path)
        last_error = ""
        deadline = time.monotonic() + LIBREOFFICE_UNO_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            result = subprocess.run(
                [
                    str(python_path),
                    str(script_path),
                    str(port),
                    source_path.as_uri(),
                    output_path.as_uri(),
                ],
                capture_output=True,
                check=False,
                text=True,
                timeout=20,
            )
            if result.returncode == 0:
                return output_path
            last_error = (result.stderr or result.stdout or "").strip()
            time.sleep(1)
        raise WordConversionError(last_error or "LibreOffice UNO 自动编号预处理超时。")
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except Exception:
            process.kill()
        shutil.rmtree(work_dir, ignore_errors=True)


def _convert_with_windows_word(source_path: Path, output_path: Path) -> None:
    try:
        import pythoncom
        import win32com.client
    except ImportError as exc:
        raise WordConversionError("未安装 pywin32，无法连接本地 Microsoft Word。") from exc

    word = None
    document = None
    pythoncom.CoInitialize()
    try:
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        try:
            word.AutomationSecurity = 3
        except Exception:
            pass
        document = word.Documents.Open(
            str(source_path),
            ReadOnly=True,
            AddToRecentFiles=False,
            ConfirmConversions=False,
            NoEncodingDialog=True,
        )
        document.SaveAs2(str(output_path), FileFormat=DOCX_FILE_FORMAT)
    except Exception as exc:  # noqa: BLE001 - converted to user-facing fallback.
        raise WordConversionError(f"使用本地 Word 转换失败：{exc}") from exc
    finally:
        if document is not None:
            try:
                document.Close(False)
            except Exception:
                pass
        if word is not None:
            try:
                word.Quit()
            except Exception:
                pass
        pythoncom.CoUninitialize()


def _convert_with_macos_word(source_path: Path, output_path: Path) -> None:
    osascript_path = shutil.which("osascript") or "/usr/bin/osascript"
    if not Path(osascript_path).exists():
        raise WordConversionError("未找到 osascript。")

    script = """
on run argv
    set inputPath to POSIX file (item 1 of argv)
    set outputPath to POSIX file (item 2 of argv)
    tell application "Microsoft Word"
        set visible to false
        try
            set display alerts to false
        end try
        try
            set automation security to 3
        end try
        open inputPath
        set activeDoc to active document
        save as activeDoc file name outputPath file format format XML document
        close activeDoc saving no
    end tell
end run
"""
    _run_command(
        [osascript_path, "-e", script, str(source_path), str(output_path)],
        timeout=WORD_CONVERSION_TIMEOUT_SECONDS,
    )


def _convert_numbering_with_windows_word(source_path: Path, output_path: Path) -> None:
    try:
        import pythoncom
        import win32com.client
    except ImportError as exc:
        raise WordConversionError("未安装 pywin32，无法连接本地 Microsoft Word。") from exc

    word = None
    document = None
    pythoncom.CoInitialize()
    try:
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        try:
            word.AutomationSecurity = 3
        except Exception:
            pass
        document = word.Documents.Open(
            str(source_path),
            ReadOnly=False,
            AddToRecentFiles=False,
            ConfirmConversions=False,
            NoEncodingDialog=True,
        )
        try:
            document.ConvertNumbersToText()
        except Exception:
            document.Range().ListFormat.ConvertNumbersToText()
        document.SaveAs2(str(output_path), FileFormat=DOCX_FILE_FORMAT)
    except Exception as exc:  # noqa: BLE001 - converted to user-facing fallback.
        raise WordConversionError(f"使用本地 Word 预处理编号失败：{exc}") from exc
    finally:
        if document is not None:
            try:
                document.Close(False)
            except Exception:
                pass
        if word is not None:
            try:
                word.Quit()
            except Exception:
                pass
        pythoncom.CoUninitialize()


def _convert_numbering_with_macos_word(source_path: Path, output_path: Path) -> None:
    osascript_path = shutil.which("osascript") or "/usr/bin/osascript"
    if not Path(osascript_path).exists():
        raise WordConversionError("未找到 osascript。")

    script = """
on run argv
    set inputPath to POSIX file (item 1 of argv)
    set outputPath to POSIX file (item 2 of argv)
    tell application "Microsoft Word"
        set visible to false
        try
            set display alerts to false
        end try
        try
            set automation security to 3
        end try
        open inputPath
        set activeDoc to active document
        try
            do Visual Basic "ActiveDocument.ConvertNumbersToText"
        on error errMsg
            try
                do Visual Basic "ActiveDocument.Range.ListFormat.ConvertNumbersToText"
            on error errMsg2
                close activeDoc saving no
                error errMsg2
            end try
        end try
        save as activeDoc file name outputPath file format format XML document
        close activeDoc saving no
    end tell
end run
"""
    _run_command(
        [osascript_path, "-e", script, str(source_path), str(output_path)],
        timeout=WORD_CONVERSION_TIMEOUT_SECONDS,
    )


_LIBREOFFICE_NUMBERING_SCRIPT = r'''
from __future__ import annotations

import sys
import time

import uno
from com.sun.star.beans import PropertyValue


def prop(name, value):
    item = PropertyValue()
    item.Name = name
    item.Value = value
    return item


def main() -> int:
    port = int(sys.argv[1])
    source_url = sys.argv[2]
    output_url = sys.argv[3]
    local_context = uno.getComponentContext()
    resolver = local_context.ServiceManager.createInstanceWithContext(
        "com.sun.star.bridge.UnoUrlResolver",
        local_context,
    )
    context = resolver.resolve(
        f"uno:socket,host=127.0.0.1,port={port};urp;StarOffice.ComponentContext"
    )
    service_manager = context.ServiceManager
    desktop = service_manager.createInstanceWithContext(
        "com.sun.star.frame.Desktop",
        context,
    )
    document = desktop.loadComponentFromURL(
        source_url,
        "_blank",
        0,
        (prop("Hidden", True),),
    )
    if document is None:
        raise RuntimeError("LibreOffice 无法打开文档。")
    try:
        controller = document.getCurrentController()
        frame = controller.getFrame()
        dispatcher = service_manager.createInstanceWithContext(
            "com.sun.star.frame.DispatchHelper",
            context,
        )
        controller.select(document.getText())
        dispatcher.executeDispatch(frame, ".uno:ConvertNumberingToText", "", 0, ())
        document.storeAsURL(
            output_url,
            (
                prop("FilterName", "Office Open XML Text"),
                prop("Overwrite", True),
            ),
        )
    finally:
        document.close(True)
    return 0


if __name__ == "__main__":
    for attempt in range(20):
        try:
            raise SystemExit(main())
        except Exception as exc:
            if attempt >= 19:
                print(exc, file=sys.stderr)
                raise SystemExit(1)
            time.sleep(0.5)
'''


def _find_soffice() -> Path | None:
    for executable in ("soffice", "libreoffice"):
        found = shutil.which(executable)
        if found:
            return Path(found)

    candidates: list[Path] = []
    if platform.system() == "Darwin":
        candidates.append(Path("/Applications/LibreOffice.app/Contents/MacOS/soffice"))
    elif platform.system() == "Windows":
        for env_name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            base = os.environ.get(env_name)
            if not base:
                continue
            candidates.append(Path(base) / "LibreOffice" / "program" / "soffice.exe")

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _find_libreoffice_python(soffice_path: Path) -> Path:
    candidates: list[Path] = []
    if platform.system() == "Darwin":
        candidates.append(soffice_path.parent.parent / "Resources" / "python")
    elif platform.system() == "Windows":
        candidates.append(soffice_path.parent / "python.exe")

    for candidate in candidates:
        if candidate.exists():
            return candidate

    found = shutil.which("python3") or shutil.which("python")
    if found:
        return Path(found)
    raise WordConversionError("未找到可用于 LibreOffice UNO 的 Python。")


def _get_temp_docx_path(original_path: Path) -> Path:
    temp_dir = Path(tempfile.gettempdir()) / "word_translator_temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    return temp_dir / f"{original_path.stem}_{uuid.uuid4().hex[:8]}.docx"


def _run_command(command: list[str], *, timeout: int) -> None:
    result = subprocess.run(
        command,
        capture_output=True,
        check=False,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise WordConversionError(detail or f"命令返回码 {result.returncode}")


def _validate_docx(path: Path) -> None:
    if not path.exists() or path.stat().st_size <= 0:
        raise WordConversionError("转换结果为空。")
    try:
        Document(str(path))
    except Exception as exc:
        raise WordConversionError(f"转换结果不是有效 .docx：{exc}") from exc
