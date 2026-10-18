"""旧版 PowerPoint（.ppt / .pps）转换为 .pptx。

PPT 翻译只认 .pptx 这一种结构（core/pptx_text.py 直接改 OOXML）；旧版二进制格式
先在这里转成临时 .pptx 再交给它，输出也就是 .pptx。三级和 core/word_converter.py
一致：

1. 本机 PowerPoint：Windows 走 COM，Mac 走 AppleScript。版式、动画、备注原样保留，
   是唯一的高保真路径。
2. LibreOffice（headless，独立的临时用户配置）：没有 PowerPoint 时接手，文字和大多数
   版式能保留，复杂动画、SmartArt、个别字体效果可能有差异。
3. 都不行：整句提示「请先在 PowerPoint 里另存为 .pptx」，不做任何更低保真的尝试。

和 Word 的 .doc 不同，LibreOffice 这一级不需要用户先确认：PPT 页面只有「双语对照」
和「翻译演讲者备注」两个设置，多一道兼容授权就多一个概念；改用的是哪条路会写进
结果说明（``PptConversionResult.note``），用户能看到。

一个任务里同一个文件只转一次：扫描、翻译、写出都经 ``PptConversionCache``，
任务结束时由它删掉临时文件。PowerPoint 打开的是原文件的临时副本，原文件本身不被
打开、不被改动；用户正开着同一份演示文稿时也不会被关掉。
"""

from __future__ import annotations

import hashlib
import platform
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Self
from xml.etree import ElementTree

from loguru import logger

from core.user_facing_errors import humanize_error

LEGACY_PPT_SUFFIXES = (".ppt", ".pps")

# PowerPoint 对象模型里的常量（PpSaveAsFileType、PpAlertLevel、MsoTriState、
# MsoAutomationSecurity），按微软文档核对过：
# ppSaveAsOpenXMLPresentation = 24，ppAlertsNone = 1，msoTrue = -1，msoFalse = 0，
# msoAutomationSecurityForceDisable = 3。
PP_SAVE_AS_OPEN_XML_PRESENTATION = 24
PP_ALERTS_NONE = 1
MSO_TRUE = -1
MSO_FALSE = 0
MSO_AUTOMATION_SECURITY_FORCE_DISABLE = 3

POWERPOINT_CONVERSION_TIMEOUT_SECONDS = 180
#: Mac 上转换完退出 PowerPoint 后，等它真正退出的最长时间。
POWERPOINT_EXIT_WAIT_SECONDS = 15
LIBREOFFICE_PPT_CONVERSION_TIMEOUT_SECONDS = 180
MACOS_POWERPOINT_APP = Path("/Applications/Microsoft PowerPoint.app")

METHOD_POWERPOINT = "PowerPoint"
METHOD_LIBREOFFICE = "LibreOffice"
FIDELITY_HIGH = "high_fidelity"
FIDELITY_COMPATIBILITY = "compatibility"

SAVE_AS_PPTX_HINT = "请先在 PowerPoint 里将它另存为 .pptx，再翻译另存后的文件。"

_PRESENTATION_REL_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
)
_STRICT_PRESENTATION_REL_TYPE = "http://purl.oclc.org/ooxml/officeDocument/relationships/officeDocument"


class PptConversionError(Exception):
    """旧版 .ppt 没能转成 .pptx。``str()`` 就是给用户看的整句。"""

    def __init__(self, message: str, *, attempts: list[str] | None = None) -> None:
        super().__init__(message)
        self.attempts = list(attempts or [])


class _ToolMissing(PptConversionError):
    """这条路在本机根本不存在（没装、平台不支持），不是转换本身失败。"""


@dataclass(frozen=True)
class PptConversionResult:
    source: Path
    path: Path
    method: str
    fallback_messages: list[str] = field(default_factory=list)
    fidelity: str = FIDELITY_HIGH

    @property
    def note(self) -> str:
        """读取后显示在文件行上的提示：原文件是什么格式、用什么转的、输出是什么格式。"""
        suffix = self.source.suffix.lower() or ".ppt"
        sentence = f"旧版 {suffix} 已用 {self.method} 转换为 .pptx，输出为 .pptx"
        if self.fidelity == FIDELITY_COMPATIBILITY:
            sentence += "，版式可能与原文件略有差异"
        return sentence


def is_legacy_ppt(path: str | Path) -> bool:
    """是不是需要先转换的旧版二进制演示文稿。"""
    return Path(path).suffix.lower() in LEGACY_PPT_SUFFIXES


def converted_name(name: str) -> str:
    """旧版文件转换后的文件名：只换扩展名，``报告.ppt`` → ``报告.pptx``。"""
    path = Path(name)
    return path.with_suffix(".pptx").name if is_legacy_ppt(path) else path.name


# ── 本机有什么可用 ─────────────────────────────────────────────────────


def get_local_powerpoint_availability() -> tuple[bool, str]:
    """不启动 PowerPoint、不弹任何授权提示，判断本机有没有 PowerPoint 可用。

    Mac 上自动化授权要等真正转换时才知道；Windows 上看 COM 注册，比只看 pywin32
    能不能导入准确（装了 pywin32 但没装 Office 的机器很常见）。
    """
    system = platform.system()
    if system == "Darwin":
        osascript_path = shutil.which("osascript") or "/usr/bin/osascript"
        if not Path(osascript_path).exists():
            return False, "本机缺少连接 PowerPoint 所需的系统组件。"
        if not MACOS_POWERPOINT_APP.exists():
            return False, "未找到 Microsoft PowerPoint，可能尚未安装。"
        return True, "已检测到 Microsoft PowerPoint；实际转换时系统可能要求自动化权限。"
    if system == "Windows":
        try:
            import pythoncom  # noqa: F401
            import win32com.client  # noqa: F401
        except ImportError:
            return False, "本机缺少连接 PowerPoint 所需的组件。"
        if not _windows_powerpoint_registered():
            return False, "未找到 Microsoft PowerPoint，可能尚未安装。"
        return True, "已检测到 Microsoft PowerPoint。"
    return False, "当前系统不支持使用本机 PowerPoint 进行转换。"


def _windows_powerpoint_registered() -> bool:
    try:
        import winreg
    except ImportError:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"PowerPoint.Application\CLSID"):
            return True
    except OSError:
        return False


# ── 转换入口 ───────────────────────────────────────────────────────────


Converter = Callable[[Path], Path]


def convert_ppt_to_pptx(
    ppt_path: str | Path,
    *,
    prefer_native_powerpoint: bool = True,
    allow_libreoffice: bool = True,
) -> PptConversionResult:
    """把旧版 .ppt / .pps 转成临时 .pptx；调用方用完负责删掉（或交给 ``PptConversionCache``）。"""
    source_path = Path(ppt_path)
    if not is_legacy_ppt(source_path):
        raise PptConversionError(f"不是旧版 .ppt 文件：{source_path.name}")
    if not source_path.is_file():
        raise PptConversionError("文件不存在或已被移动，请重新选择来源。")
    if not _looks_like_presentation(source_path):
        # 不是演示文稿（Office 的「~$」锁文件、改了扩展名的别的文件）：交给 PowerPoint
        # 它会对着打不开的文件等上两分钟，LibreOffice 也只会报错。
        raise PptConversionError("这不是有效的 PowerPoint 文件，可能已损坏或只是临时文件。")

    attempts: list[tuple[str, str, Converter]] = []
    if prefer_native_powerpoint:
        attempts.append((METHOD_POWERPOINT, FIDELITY_HIGH, convert_with_native_powerpoint))
    if allow_libreoffice:
        attempts.append((METHOD_LIBREOFFICE, FIDELITY_COMPATIBILITY, convert_with_libreoffice))

    errors: list[str] = []
    unavailable: list[str] = []
    for method, fidelity, converter in attempts:
        try:
            output_path = converter(source_path)
        except _ToolMissing as exc:
            unavailable.append(method)
            errors.append(f"{method}：{exc}")
            logger.info(f"旧版 PPT 转换跳过 {method}（{source_path.name}）：{exc}")
            continue
        except Exception as exc:  # noqa: BLE001 - 下一条路接手。
            if isinstance(exc, PptConversionError):
                message = str(exc)  # 各条路自己已经写成整句。
            else:
                message = humanize_error(exc, fallback=f"{method} 未能完成本次转换。")
            errors.append(f"{method}：{message}")
            logger.info(f"旧版 PPT 转换失败 {source_path.name}（{method}）：{message}")
            logger.debug(f"旧版 PPT 转换失败 {source_path.name}（{method}），原始信息：{exc!r}")
            continue
        try:
            validate_pptx(output_path)
        except PptConversionError as exc:
            _discard(output_path)
            errors.append(f"{method}：{exc}")
            logger.info(f"旧版 PPT 转换结果无效 {source_path.name}（{method}）：{exc}")
            continue
        logger.info(f"旧版 PPT 已转换：{source_path.name} -> {output_path.name}（{method}）")
        return PptConversionResult(
            source=source_path,
            path=output_path,
            method=method,
            fallback_messages=list(errors),
            fidelity=fidelity,
        )

    if attempts and len(unavailable) == len(attempts):
        message = "本机未找到 PowerPoint 或 LibreOffice，无法转换旧版文件。" + SAVE_AS_PPTX_HINT
    elif not attempts:
        message = "没有可用的转换方式。" + SAVE_AS_PPTX_HINT
    else:
        message = "旧版文件转换失败（" + "；".join(errors) + "）。" + SAVE_AS_PPTX_HINT
    raise PptConversionError(message, attempts=errors)


#: 旧版 .ppt / .pps 是 OLE 复合文档；改了扩展名的 .pptx 是 zip，PowerPoint 也能打开。
_PRESENTATION_SIGNATURES = (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", b"PK\x03\x04")


def _looks_like_presentation(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            head = handle.read(8)
    except OSError:
        return False
    return any(head.startswith(signature) for signature in _PRESENTATION_SIGNATURES)


# ── 本机 PowerPoint ─────────────────────────────────────────────────────


#: PowerPoint 只有一个实例，两次转换同时进行会互相关掉对方的文档、抢着退出。
_POWERPOINT_LOCK = threading.Lock()


def convert_with_native_powerpoint(ppt_path: str | Path) -> Path:
    source_path = Path(ppt_path)
    system = platform.system()
    if system not in {"Windows", "Darwin"}:
        raise _ToolMissing("当前系统不支持使用本机 PowerPoint 进行转换。")
    available, reason = get_local_powerpoint_availability()
    if not available:
        raise _ToolMissing(reason)
    output_path = _temp_pptx_path(source_path)
    # 打开的是副本：原文件不被 PowerPoint 碰到（修改时间不变，读取时转的这份任务里
    # 接着用）；用户在 PowerPoint 里开着同一份文件时，关掉的也只是副本。副本和产物放在
    # 同一个临时目录里，Mac 上 PowerPoint 能写这里，也就能读这里。
    copy_path = output_path.with_name(f"{output_path.stem}_source{source_path.suffix.lower()}")
    try:
        shutil.copyfile(source_path, copy_path)
        with _POWERPOINT_LOCK:
            if system == "Windows":
                _convert_with_windows_powerpoint(copy_path, output_path)
            else:
                _convert_with_macos_powerpoint(copy_path, output_path)
    except Exception:
        _discard(output_path)
        raise
    finally:
        _discard(copy_path)
    return output_path


def _convert_with_windows_powerpoint(source_path: Path, output_path: Path) -> None:
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    app = None
    presentation = None
    # PowerPoint 只有一个实例：DispatchEx 也会连上用户已经开着的那个。所以这里
    # 改的提示级别要还原，用户原来开着演示文稿时也绝不能 Quit——那会把用户的文件关掉。
    opened_before = 0
    previous_alerts = None
    previous_security = None
    try:
        app = win32com.client.DispatchEx("PowerPoint.Application")
        opened_before = _safe_count(app)
        # 不设 Visible = False：PowerPoint 不允许隐藏主窗口，会直接报错；
        # 不弹窗口靠的是 Open 的 WithWindow=False。
        try:
            previous_alerts = app.DisplayAlerts
            app.DisplayAlerts = PP_ALERTS_NONE
        except Exception:  # noqa: BLE001 - 老版本没有这个属性也能转。
            previous_alerts = None
        try:
            previous_security = app.AutomationSecurity
            app.AutomationSecurity = MSO_AUTOMATION_SECURITY_FORCE_DISABLE
        except Exception:  # noqa: BLE001
            previous_security = None
        presentation = app.Presentations.Open(
            str(source_path.resolve()),
            ReadOnly=MSO_TRUE,
            Untitled=MSO_FALSE,
            WithWindow=MSO_FALSE,
        )
        presentation.SaveAs(str(output_path.resolve()), PP_SAVE_AS_OPEN_XML_PRESENTATION)
    except Exception as exc:  # 交给入口统一换成用户能看懂的话。
        logger.debug(f"使用本机 PowerPoint 转换失败原始错误：{exc!r}")
        raise PptConversionError(
            humanize_error(exc, fallback="PowerPoint 未能完成本次转换。")
        ) from exc
    finally:
        if presentation is not None:
            _quietly(presentation.Close, "关闭演示文稿")
        if app is not None:
            if opened_before == 0 and _safe_count(app) == 0:
                _quietly(app.Quit, "退出 PowerPoint")
            else:
                if previous_alerts is not None:
                    _quietly(lambda: setattr(app, "DisplayAlerts", previous_alerts), "还原提示级别")
                if previous_security is not None:
                    _quietly(lambda: setattr(app, "AutomationSecurity", previous_security), "还原宏安全级别")
        pythoncom.CoUninitialize()


def _quietly(action: Callable[[], object], what: str) -> None:
    """收尾动作失败不能盖住真正的转换结果，记一笔就够了。"""
    try:
        action()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"PowerPoint {what}失败：{exc!r}")


def _safe_count(app: object) -> int:
    try:
        return int(app.Presentations.Count)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - 读不到就当有人在用，宁可不退出。
        return 1


# 和 Word 的 AppleScript 同样的结构：路径在 tell 之外转成 POSIX file，保存失败也要
# 关掉文档，否则它留在 PowerPoint 里，下一次转同名文件会出错。退出的条件和 Windows
# 一致：PowerPoint 本来没开，或开着但一份演示文稿都没有、关掉副本后也没有，才退出；
# 用户开着文件时绝不退出。退出了就输出 quit，调用方等它真正退出后才返回——否则紧接
# 着的下一次转换会把正在退出的 PowerPoint 当成用户开着的，转完不退、留下空窗口。
_MACOS_POWERPOINT_SCRIPT = """
on run argv
    set inputFile to POSIX file (item 1 of argv)
    set outputFile to POSIX file (item 2 of argv)
    set wasRunning to application "Microsoft PowerPoint" is running
    set saveFailed to false
    set saveMessage to ""
    set saveNumber to 0
    set shouldQuit to not wasRunning
    tell application "Microsoft PowerPoint"
        set openBefore to 0
        if wasRunning then
            try
                set openBefore to count of presentations
            on error
                set openBefore to 1
            end try
        end if
        open inputFile
        set thePresentation to active presentation
        try
            save thePresentation in outputFile as save as Open XML presentation
        on error errMsg number errNum
            set saveFailed to true
            set saveMessage to errMsg
            set saveNumber to errNum
        end try
        try
            close thePresentation saving no
        end try
        if openBefore is 0 then
            try
                if (count of presentations) is 0 then set shouldQuit to true
            end try
        end if
        if shouldQuit then quit saving no
    end tell
    if saveFailed then error saveMessage number saveNumber
    if shouldQuit then return "quit"
    return "kept"
end run
"""


def _convert_with_macos_powerpoint(source_path: Path, output_path: Path) -> None:
    osascript_path = shutil.which("osascript") or "/usr/bin/osascript"
    was_running = _macos_powerpoint_running()
    try:
        result = subprocess.run(
            [osascript_path, "-e", _MACOS_POWERPOINT_SCRIPT, str(source_path.resolve()), str(output_path)],
            capture_output=True,
            check=False,
            text=True,
            timeout=POWERPOINT_CONVERSION_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise PptConversionError(
            f"PowerPoint 转换超时（{POWERPOINT_CONVERSION_TIMEOUT_SECONDS} 秒），文件可能设置了打开密码。"
        ) from exc
    quit_requested = (result.stdout or "").strip().endswith("quit")
    if quit_requested or (result.returncode != 0 and not was_running):
        # 保存失败时脚本在报错前也会让它拉起的 PowerPoint 退出。
        _wait_for_macos_powerpoint_exit()
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        logger.debug(f"PowerPoint AppleScript 返回码 {result.returncode}：{detail or '（无输出）'}")
        raise PptConversionError(humanize_error(detail, fallback="PowerPoint 未能完成本次转换。"))


def _macos_powerpoint_running() -> bool:
    pgrep_path = shutil.which("pgrep") or "/usr/bin/pgrep"
    try:
        result = subprocess.run(
            [pgrep_path, "-x", "Microsoft PowerPoint"], capture_output=True, check=False, timeout=5
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _wait_for_macos_powerpoint_exit() -> None:
    deadline = time.monotonic() + POWERPOINT_EXIT_WAIT_SECONDS
    while _macos_powerpoint_running():
        if time.monotonic() >= deadline:
            logger.info(f"PowerPoint 在 {POWERPOINT_EXIT_WAIT_SECONDS} 秒内没有退出")
            return
        time.sleep(0.25)


# ── LibreOffice ────────────────────────────────────────────────────────


def convert_with_libreoffice(ppt_path: str | Path) -> Path:
    """用 LibreOffice headless 转换；每次单独一份临时用户配置，用户开着 LibreOffice 也不冲突。"""
    from core.word_converter import _find_soffice

    source_path = Path(ppt_path)
    soffice_path = _find_soffice()
    if soffice_path is None:
        raise _ToolMissing("本机未找到 LibreOffice。")

    output_path = _temp_pptx_path(source_path)
    work_dir = Path(tempfile.mkdtemp(prefix="xl_translator_lo_ppt_"))
    profile_dir = work_dir / "profile"
    convert_dir = work_dir / "out"
    profile_dir.mkdir(parents=True, exist_ok=True)
    convert_dir.mkdir(parents=True, exist_ok=True)
    command = [
        str(soffice_path),
        "--headless",
        "--norestore",
        "--nodefault",
        "--nofirststartwizard",
        f"-env:UserInstallation={profile_dir.as_uri()}",
        "--convert-to",
        "pptx:Impress MS PowerPoint 2007 XML",
        "--outdir",
        str(convert_dir),
        str(source_path.resolve()),
    ]
    try:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                check=False,
                text=True,
                timeout=LIBREOFFICE_PPT_CONVERSION_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise PptConversionError(
                f"LibreOffice 转换超时（{LIBREOFFICE_PPT_CONVERSION_TIMEOUT_SECONDS} 秒）。"
            ) from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            logger.debug(f"LibreOffice 转换返回码 {result.returncode}：{detail or '（无输出）'}")
            raise PptConversionError("LibreOffice 未能转换此文件。")
        converted = convert_dir / f"{source_path.stem}.pptx"
        if not converted.exists():
            # 原名带 soffice 不认的字符时它会改名落盘；目录里只有一个产物就认它。
            candidates = list(convert_dir.glob("*.pptx"))
            if len(candidates) == 1:
                converted = candidates[0]
        if not converted.exists():
            # soffice 打不开文件（损坏、加密）时返回码照样是 0，只是什么都不写。
            raise PptConversionError("LibreOffice 未生成 .pptx，文件可能已损坏或设置了打开密码。")
        shutil.move(str(converted), str(output_path))
        return output_path
    except Exception:
        _discard(output_path)
        raise
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# ── 结果校验 ───────────────────────────────────────────────────────────


def validate_pptx(path: str | Path) -> None:
    """确认转换产物是能打开的 .pptx：是 zip、有内容类型表、主文档关系指向的部件存在且是演示文稿。"""
    path = Path(path)
    if not path.exists() or path.stat().st_size <= 0:
        raise PptConversionError("转换结果为空。")
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if "[Content_Types].xml" not in names or "_rels/.rels" not in names:
                raise PptConversionError("转换结果不是有效的 .pptx。")
            main_part = _main_part_name(archive.read("_rels/.rels"))
            if main_part is None or main_part not in names:
                raise PptConversionError("转换结果不是有效的 .pptx。")
            root = ElementTree.fromstring(archive.read(main_part))
    except PptConversionError:
        raise
    except (zipfile.BadZipFile, ElementTree.ParseError, KeyError, OSError) as exc:
        raise PptConversionError("转换结果不是有效的 .pptx。") from exc
    if not root.tag.endswith("}presentation"):
        raise PptConversionError("转换结果不是演示文稿。")


def _main_part_name(rels_xml: bytes) -> str | None:
    root = ElementTree.fromstring(rels_xml)
    for rel in root:
        if rel.get("Type") in {_PRESENTATION_REL_TYPE, _STRICT_PRESENTATION_REL_TYPE}:
            target = (rel.get("Target") or "").lstrip("/")
            return str(PurePosixPath(target)) if target else None
    return None


# ── 一个任务里只转一次 ─────────────────────────────────────────────────


@dataclass
class _CacheEntry:
    size: int
    mtime_ns: int
    digest: str
    outcome: PptConversionResult | PptConversionError


class PptConversionCache:
    """同一个文件只转一次：读取时转的那份，开始翻译时取字、写出接着用。

    按文件路径记；文件的大小和修改时间没变就直接用，变了再比内容（SHA-256）——同步盘、
    杀毒软件只碰了修改时间时内容没变，不该再拉起一遍 PowerPoint；内容真变了才重转。
    失败也记住：没有 PowerPoint 的机器上，扫描已经失败过一次的文件，翻译时不必再
    拉起一遍 LibreOffice 等它再失败。用完调 ``cleanup()``（或用 with）删掉临时文件。
    """

    def __init__(self, converter: Callable[[Path], PptConversionResult] | None = None) -> None:
        self._converter = converter or convert_ppt_to_pptx
        self._lock = threading.Lock()
        self._entries: dict[str, _CacheEntry] = {}

    def convert(self, path: str | Path) -> PptConversionResult:
        source = Path(path)
        key, size, mtime_ns = self._stat(source)
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and (entry.size, entry.mtime_ns) != (size, mtime_ns):
                if entry.size == size and _file_digest(source) == entry.digest:
                    entry.mtime_ns = mtime_ns
                else:
                    logger.info(f"旧版 PPT 内容有变化，重新转换：{source.name}")
                    self._drop(key)
                    entry = None
            if entry is None:
                digest = _file_digest(source)
                try:
                    outcome: PptConversionResult | PptConversionError = self._converter(source)
                except PptConversionError as exc:
                    outcome = exc
                entry = _CacheEntry(size, mtime_ns, digest, outcome)
                self._entries[key] = entry
            else:
                logger.debug(f"旧版 PPT 沿用已转换的副本：{source.name}")
        if isinstance(entry.outcome, PptConversionError):
            raise entry.outcome
        return entry.outcome

    def release(self, path: str | Path) -> None:
        """这个文件用完了（写出后，或转换失败要让下次重试）：忘掉它，删掉临时文件。"""
        with self._lock:
            self._drop(str(Path(path).resolve()))

    def cleanup(self) -> None:
        with self._lock:
            for key in list(self._entries):
                self._drop(key)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.cleanup()

    def _drop(self, key: str) -> None:
        entry = self._entries.pop(key, None)
        if entry is not None and isinstance(entry.outcome, PptConversionResult):
            _discard(entry.outcome.path)

    @staticmethod
    def _stat(source: Path) -> tuple[str, int, int]:
        try:
            stat = source.stat()
        except OSError as exc:
            raise PptConversionError("文件不存在或已被移动，请重新选择来源。") from exc
        return str(source.resolve()), stat.st_size, stat.st_mtime_ns


# ── 小工具 ─────────────────────────────────────────────────────────────


def _temp_pptx_path(original: Path) -> Path:
    temp_dir = Path(tempfile.gettempdir()) / "xl_translator_temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    return temp_dir / f"{original.stem}_{uuid.uuid4().hex[:8]}.pptx"


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise PptConversionError("文件不存在或已被移动，请重新选择来源。") from exc
    return digest.hexdigest()


def _discard(path: Path) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError as exc:
        logger.debug(f"清理临时 .pptx 失败（{path}）：{exc}")
