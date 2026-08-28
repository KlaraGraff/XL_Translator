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

from core.user_facing_errors import humanize_error


DOCX_FILE_FORMAT = 16
WORD_CONVERSION_TIMEOUT_SECONDS = 180
LIBREOFFICE_UNO_TIMEOUT_SECONDS = 90
# 起 soffice 之前先花这点时间确认 LibreOffice 自带的 Python 还能不能跑起来。
LIBREOFFICE_PYTHON_PROBE_TIMEOUT_SECONDS = 15
# codesign 只是读签名，不启动任何东西；给个宽裕的上限防它挂死。
LIBREOFFICE_CODESIGN_TIMEOUT_SECONDS = 10


class WordConversionError(Exception):
    """Raised when a legacy Word document cannot be converted to DOCX."""


@dataclass(frozen=True)
class WordConversionResult:
    path: Path
    method: str
    fallback_messages: list[str] = field(default_factory=list)
    # A legacy .doc converted with Microsoft Word is the only high-fidelity
    # path.  LibreOffice and textutil are useful, but are compatibility paths
    # which must never be selected without an explicit task-level approval.
    fidelity: str = "high_fidelity"


def is_legacy_word_doc(path: str | Path) -> bool:
    """Return whether a path points to an old binary .doc document."""
    return Path(path).suffix.lower() == ".doc"


def convert_doc_to_docx(
    doc_path: str | Path,
    *,
    prefer_native_word: bool = True,
    allow_compatibility_fallback: bool = False,
) -> WordConversionResult:
    """Convert a legacy ``.doc`` to a temporary ``.docx``.

    Microsoft Word is the high-fidelity path.  LibreOffice and macOS
    ``textutil`` can alter layout, fields, drawings or macros, so they are
    considered only when the caller has already recorded the user's explicit
    compatibility-mode confirmation.  This boundary is intentionally here,
    rather than merely in the UI, so a future API/CLI caller cannot silently
    degrade a document.
    """
    source_path = Path(doc_path)
    if not is_legacy_word_doc(source_path):
        raise WordConversionError(f"不是旧版 .doc 文件：{source_path}")

    high_fidelity_attempts: list[tuple[str, object]] = []
    if prefer_native_word:
        high_fidelity_attempts.append(("本地 Word", convert_with_native_word))

    compatibility_attempts: list[tuple[str, object]] = []
    if allow_compatibility_fallback:
        compatibility_attempts.append(("LibreOffice", convert_with_libreoffice))
        if platform.system() == "Darwin":
            compatibility_attempts.append(("macOS textutil", convert_with_textutil))

    errors: list[str] = []
    for method_name, converter in high_fidelity_attempts:
        try:
            output_path = converter(source_path)
            _validate_docx(output_path)
            logger.info(f".doc 转换成功：{source_path.name} -> {output_path.name} ({method_name})")
            return WordConversionResult(
                path=output_path,
                method=method_name,
                fallback_messages=list(errors),
                fidelity="high_fidelity",
            )
        except Exception as exc:  # noqa: BLE001 - later path requires consent.
            user_message = humanize_error(exc)
            errors.append(f"{method_name} 不可用：{user_message}")
            logger.info(f".doc 转换策略不可用 {source_path.name} ({method_name}): {user_message}")
            logger.debug(f".doc 转换策略不可用 {source_path.name} ({method_name})，原始信息：{exc}")

    if not allow_compatibility_fallback:
        detail = "；".join(errors) if errors else "未启用本机 Microsoft Word 高保真自动化"
        raise WordConversionError(
            "无法使用本机 Microsoft Word 高保真转换 .doc。"
            "未取得兼容转换确认，因此不会自动改用 LibreOffice 或 macOS textutil。"
            "请安装/授权 Microsoft Word 后重试，或在确认可能损失版式、域、图文和宏后明确允许兼容转换。"
            f" 详情：{detail}"
        )

    for method_name, converter in compatibility_attempts:
        try:
            output_path = converter(source_path)
            _validate_docx(output_path)
            logger.info(f".doc 兼容转换成功：{source_path.name} -> {output_path.name} ({method_name})")
            return WordConversionResult(
                path=output_path,
                method=method_name,
                fallback_messages=list(errors),
                fidelity="compatibility",
            )
        except Exception as exc:  # noqa: BLE001 - each opted-in strategy falls through.
            user_message = humanize_error(exc)
            errors.append(f"{method_name} 不可用：{user_message}")
            logger.info(f".doc 兼容转换策略不可用 {source_path.name} ({method_name}): {user_message}")
            logger.debug(f".doc 兼容转换策略不可用 {source_path.name} ({method_name})，原始信息：{exc}")

    detail = "；".join(errors) if errors else "没有可用的转换策略"
    raise WordConversionError(
        "无法将旧版 .doc 转换为 .docx。"
        "请确认本机 Microsoft Word 或 LibreOffice 可用，"
        "或手动另存为 .docx 后再翻译。"
        f" 详情：{detail}"
    )


def macos_word_automation_privacy_path() -> str:
    """Return the correct user-visible Apple Events permission route."""
    if platform.system() != "Darwin":
        return "系统的自动化隐私设置"
    try:
        major = int(str(platform.mac_ver()[0]).split(".", 1)[0])
    except (TypeError, ValueError):
        major = 13
    if major <= 12:
        return "系统偏好设置 > 安全性与隐私 > 隐私 > 自动化"
    return "系统设置 > 隐私与安全性 > 自动化"


def get_local_word_automation_availability() -> tuple[bool, str]:
    """Check for a local high-fidelity Word automation path without prompting.

    This is deliberately a conservative availability probe.  TCC permission is
    still checked by the actual conversion, where its failure can be recorded
    against the individual file instead of being mistaken for a successful
    preflight.
    """
    system = platform.system()
    if system == "Darwin":
        osascript_path = shutil.which("osascript") or "/usr/bin/osascript"
        if not Path(osascript_path).exists():
            return False, "未找到 macOS osascript。"
        if not Path("/Applications/Microsoft Word.app").exists():
            return False, "未找到 Microsoft Word.app。"
        return True, "已检测到 Microsoft Word；实际转换仍可能要求自动化权限。"
    if system == "Windows":
        try:
            import pythoncom  # noqa: F401
            import win32com.client  # noqa: F401
        except ImportError:
            return False, "未安装 pywin32，无法连接本地 Microsoft Word。"
        return True, "已检测到 Windows Word 自动化依赖；实际转换仍会验证 Word 安装状态。"
    return False, f"当前平台 {system} 不支持本机 Microsoft Word 自动化。"


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
            user_message = humanize_error(exc)
            errors.append(f"{method_name} 不可用：{user_message}")
            logger.info(f"Word 编号预处理策略不可用 {source_path.name} ({method_name}): {user_message}")
            logger.debug(f"Word 编号预处理策略不可用 {source_path.name} ({method_name})，原始信息：{exc}")

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
        # Word for Mac 没有「把自动编号转成文本」的自动化入口：这一步在 Windows 上靠
        # VBA 的 ConvertNumbersToText，而 Mac 版早已移除 `do Visual Basic`，AppleScript
        # 字典里也没有对应命令。以前这里发一段 `do Visual Basic` 脚本，结果是每次都编译
        # 失败（osascript 报 -2741），然后静默退回 Python——用户既等了一次 Word 启动，
        # 又在日志里收到一串 AppleScript 报错。不做不可能成功的尝试，直接让位给
        # LibreOffice / Python 兜底。
        raise WordConversionError("Word for Mac 不提供自动编号转文本的自动化接口。")
    raise WordConversionError(f"当前平台 {system} 暂不支持本地 Word 自动编号预处理。")


def convert_numbering_to_text_with_libreoffice(docx_path: str | Path) -> Path:
    """Use LibreOffice UNO dispatch to convert automatic numbering to text."""
    source_path = Path(docx_path)
    soffice_path = _find_soffice()
    if soffice_path is None:
        raise WordConversionError("未找到 LibreOffice/soffice。")

    # 先确认这台机器上 LibreOffice 自带的 Python 还能起来，再决定要不要拉起 soffice。
    # 顺序很重要：soffice 启动要好几秒，而这条路真正会挂的地方是它的 Python——
    # 探活失败就直接让位给 Python 兜底，省下那几秒，也不会留一个空转的 headless 进程。
    python_path = _find_libreoffice_python(soffice_path)
    _ensure_libreoffice_python_runnable(python_path)

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
        last_error = ""
        deadline = time.monotonic() + LIBREOFFICE_UNO_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            result = subprocess.run(
                [
                    str(python_path),
                    "-B",
                    str(script_path),
                    str(port),
                    source_path.as_uri(),
                    output_path.as_uri(),
                ],
                capture_output=True,
                check=False,
                text=True,
                timeout=20,
                env=_libreoffice_python_env(),
            )
            if result.returncode == 0:
                return output_path
            last_error = (result.stderr or result.stdout or "").strip()
            # 这个循环本来是给「soffice 还没监听上端口」留的重试余地：那种失败下一秒就好了。
            # 但「进程被信号杀掉」是另一回事——代码签名被系统拒绝、被 OOM kill，都属于
            # 重试一百次也不会变的状态。原来不分青红皂白地每秒重试一次、连试 90 秒，
            # 等于把一次启动失败放大成 90 次崩溃：macOS 会为每一次生成一份崩溃报告，
            # 并把「XX 意外退出」的对话框叠在应用窗口上，而这 90 秒买不到任何东西——
            # 最后照样退回 Python 兜底。认出信号杀就立刻停手。
            if result.returncode < 0:
                raise WordConversionError(
                    _libreoffice_python_killed_message(python_path, -result.returncode)
                )
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
        try
            save as activeDoc file name outputPath file format format XML document
        on error errMsg
            -- A failed save must still close the document, or it lingers in
            -- Word and breaks the next conversion of the same file name.
            try
                close activeDoc saving no
            end try
            error errMsg
        end try
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
    # 先看标准安装位置，再退回 PATH。顺序不能反过来：PATH 上的 soffice 常常是包管理器
    # 生成的包装脚本（本机的 /opt/homebrew/bin/soffice 就是），它不在应用包里，而
    # _find_libreoffice_python 要靠这个路径往上推 ../Resources/python 才能找到
    # LibreOffice 自带的解释器。拿包装脚本去推，推出来的目录根本不存在。
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

    for executable in ("soffice", "libreoffice"):
        found = shutil.which(executable)
        if found:
            return Path(found)
    return None


def _find_libreoffice_python(soffice_path: Path) -> Path:
    """找 LibreOffice 自带的那个 Python——只有它装了 uno 模块。

    这里绝不能退回系统的 python3。通用解释器永远 import 不到 uno，拿它去跑桥接脚本，
    换来的是二十轮重试、九十秒之后一句 ModuleNotFoundError；找不到就当场说找不到，
    调用方立刻让位给纯 Python 兜底，用户少等一分半。
    """
    resolved = Path(soffice_path).resolve()
    candidates: list[Path] = []
    if platform.system() == "Darwin":
        # 用 resolved 而不是原路径：PATH 上的 soffice 可能是指向应用包内的符号链接。
        candidates.append(resolved.parent.parent / "Resources" / "python")
        candidates.append(Path("/Applications/LibreOffice.app/Contents/Resources/python"))
    elif platform.system() == "Windows":
        candidates.append(resolved.parent / "python.exe")

    for candidate in candidates:
        if candidate.exists():
            return candidate

    raise WordConversionError(
        f"没找到 LibreOffice 自带的 Python（从 {soffice_path} 推断）。"
        "编号转文本这一步需要它，本次改用纯 Python 兜底。"
    )


def _libreoffice_python_env() -> dict[str, str]:
    """跑 LibreOffice 自带的 Python 时，禁止它往自己的应用包里写字节码缓存。

    那些 `__pycache__/*.pyc` 是 LibreOffice 签名时封进去的文件。解释器一旦按常规习惯
    重新生成它们，包的封条就对不上，macOS 会开始拒绝启动包里的嵌套程序——正是本机
    现在这个症状。这台机器上的 27 个失配文件时间戳全是安装那一刻，不是我们写的；
    但我们没有理由成为下一个写它的人，所以 `-B` 加环境变量双保险。
    """
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _libreoffice_python_killed_message(python_path: Path, signal_number: int) -> str:
    """被系统杀掉时给一句能照着做的话，而不是一个空的 stderr。"""
    if signal_number == 9 and platform.system() == "Darwin":
        # 实际遇到过的两种形态都表现为 Launch Constraint Violation 的 SIGKILL：
        # 一种是 LibreOffice.app 安装损坏；另一种更常见——新版 LibreOffice 给内置
        # Python 签了「父进程必须是 LibreOffice」的启动限制，外部程序一启动它就被杀，
        # 安装是完好的，重装也没用。正常路径下启动限制在 codesign 预检里就被认出来、
        # 根本不会走到这里（见 _libreoffice_python_launch_constraint_message）；
        # 走到这里说明预检没读出来，所以话不能说死，更不能劝人白白重装一遍。
        return (
            "LibreOffice 自带的 Python 被 macOS 直接终止（代码签名或启动限制未通过）。"
            "新版 LibreOffice 只允许它自己调用这个 Python，外部程序调用会被系统拦下。"
            "已自动改用内置方式处理编号，译文不受影响。"
        )
    return (
        f"LibreOffice 自带的 Python 被信号 {signal_number} 终止：{python_path}。"
        "会自动改用内置方式处理编号。"
    )


# 探活结论按解释器路径缓存一次：{路径: None 表示可用, str 表示不可用的原因}。
# 缓存的理由是探活本身有代价——解释器要是被系统杀掉，每探一次 macOS 就多生成一份
# 崩溃报告、多弹一个「意外退出」对话框。一批文档挨个探，用户就会收到一叠对话框。
# 这台机器上 LibreOffice 装坏了是个稳定状态，一次结论管到进程结束即可；用户重装了
# LibreOffice 也重启了应用，缓存自然跟着没了。
_LIBREOFFICE_PYTHON_PROBE_CACHE: dict[str, str | None] = {}


def _find_libreoffice_python_app_bundle(python_path: Path) -> Path | None:
    """从 Resources/python 包装脚本推断真正被启动的 Python.app（macOS）。

    subprocess 启动的是一个 shell 包装脚本，它 exec 到
    Contents/Frameworks/LibreOfficePython.framework 里的 Python.app——签名和
    启动限制都在后者身上，codesign 必须问它才问得到真话。
    """
    for parent in Path(python_path).resolve().parents:
        framework = parent / "Contents" / "Frameworks" / "LibreOfficePython.framework"
        if not framework.exists():
            continue
        current = framework / "Versions" / "Current" / "Resources" / "Python.app"
        if current.exists():
            return current
        candidates = sorted(framework.glob("Versions/*/Resources/Python.app"))
        return candidates[-1] if candidates else None
    return None


def _libreoffice_python_launch_constraint_message(python_path: Path) -> str | None:
    """不启动解释器，先从签名里读出「它会不会被 macOS 拦下」。

    新版 LibreOffice 给内置 Python 签了父进程启动限制（launch constraint）：
    只有 LibreOffice 自己能启动它，别的进程一启动就被 SIGKILL——安装完好，
    重装无济于事。真把它跑起来探测，代价是每次一份系统崩溃报告加一个
    「LibreOfficePython 意外退出」对话框叠在应用窗口上；codesign 读签名则
    什么都不启动。读到限制就直接判不可用，探活根本不该发生。
    """
    if platform.system() != "Darwin":
        return None
    bundle = _find_libreoffice_python_app_bundle(python_path)
    if bundle is None:
        return None
    try:
        result = subprocess.run(
            ["/usr/bin/codesign", "--display", "--verbose=3", str(bundle)],
            capture_output=True,
            check=False,
            text=True,
            timeout=LIBREOFFICE_CODESIGN_TIMEOUT_SECONDS,
        )
    except Exception:
        # codesign 本身出问题就当没查过，落回原来的探活——它自己会认出被杀的情况。
        return None
    # codesign --display 的清单写在 stderr；万一哪个版本改道 stdout，两边都看。
    listing = f"{result.stdout}\n{result.stderr}"
    if "Parent Launch Constraint" in listing:
        return (
            "新版 LibreOffice 只允许它自己调用内置的 Python（代码签名里带有启动限制），"
            "外部程序一调用就会被 macOS 终止。已自动改用内置方式处理编号，译文不受影响。"
        )
    return None


def _ensure_libreoffice_python_runnable(python_path: Path) -> None:
    """确认这个 Python 解释器能被启动；不能就抛错，让调用方走兜底。

    只测「起不起得来」，不测 `import uno`——后者失败是另一类问题（UNO 环境没配好），
    该由主流程的真实调用去暴露，探活越窄，误杀越少。
    """
    cache_key = str(python_path)
    if cache_key in _LIBREOFFICE_PYTHON_PROBE_CACHE:
        cached = _LIBREOFFICE_PYTHON_PROBE_CACHE[cache_key]
        if cached is None:
            return
        raise WordConversionError(cached)
    # 顺序：先读签名，再考虑真启动。签名里已经写明会被拦的，一次都不要启动。
    constraint_message = _libreoffice_python_launch_constraint_message(python_path)
    if constraint_message is not None:
        _LIBREOFFICE_PYTHON_PROBE_CACHE[cache_key] = constraint_message
        raise WordConversionError(constraint_message)
    try:
        _run_libreoffice_python_probe(python_path)
    except WordConversionError as exc:
        _LIBREOFFICE_PYTHON_PROBE_CACHE[cache_key] = str(exc)
        raise
    _LIBREOFFICE_PYTHON_PROBE_CACHE[cache_key] = None


def _run_libreoffice_python_probe(python_path: Path) -> None:
    try:
        result = subprocess.run(
            [str(python_path), "-B", "-c", ""],
            capture_output=True,
            check=False,
            text=True,
            timeout=LIBREOFFICE_PYTHON_PROBE_TIMEOUT_SECONDS,
            env=_libreoffice_python_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise WordConversionError(
            f"LibreOffice 自带的 Python 启动超时：{python_path}。"
        ) from exc
    if result.returncode < 0:
        raise WordConversionError(
            _libreoffice_python_killed_message(python_path, -result.returncode)
        )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise WordConversionError(
            f"LibreOffice 自带的 Python 无法启动（退出码 {result.returncode}）"
            + (f"：{detail}" if detail else "。")
        )


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
