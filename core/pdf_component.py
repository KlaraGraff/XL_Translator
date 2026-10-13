"""「PDF 处理组件」：PyMuPDF 不随安装包分发，从用户装好的 pdf2docx 环境里加载。

PyMuPDF（AGPL-3.0）有约 60MB，放进安装包会让 macOS 安装包翻倍。它只有 PDF「自动
组合」里的原位替换、转成 Word 再翻译和逐页判断类型用得到；整页图像翻译、页数统计、
渲染、预览都用随安装包分发的 pypdfium2，不需要它。

用户在「插件」页连接的 pdf2docx（见 ``core.pdf2docx_plugin``）本身依赖 PyMuPDF，装
pdf2docx 时会一并装上，所以两样合成一个组件：连接一次，转 Word 用它的命令行，其余
用它环境里的 PyMuPDF。

加载顺序：
1. 当前解释器本来就能 import（源码运行、装了 PyMuPDF 的测试环境）——直接用。
2. 否则找到 pdf2docx 所在环境的 site-packages，核对 PyMuPDF 的 wheel 标签与本进程的
   Python 版本和处理器架构相符，先在子进程里试加载一次（原生库不匹配时崩的是子进程，
   不会拖垮翻译引擎），再挂一个只认 ``pymupdf`` / ``fitz`` 两个名字的导入器：其它包
   仍只用安装包自带的版本。

本模块在 ``core`` 下，不能从 ``api`` 导入。
"""
from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import threading
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

COMPONENT_NAME = "PDF 处理组件"
MISSING_MESSAGE = "未安装「PDF 处理组件」"
# 原位替换用到的接口（get_texttrace、Font、subset_fonts 等）在 1.24 起都齐了；
# 1.24 之前的版本也没有 ``pymupdf`` 这个包名。
MIN_VERSION = (1, 24)
PROBE_FLAG = "--probe-pdf-component"
_PROBE_TIMEOUT = 90.0
_LOCATE_TIMEOUT = 30.0
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
_MODULE_NAMES = ("pymupdf", "fitz")


class PdfComponentMissing(ImportError):
    """PyMuPDF 不可用：没连接 pdf2docx，或它环境里的 PyMuPDF 加载不了。"""


@dataclass(frozen=True)
class ComponentState:
    available: bool
    source: str = ""        # "bundled"（解释器本来就有）或 site-packages 的路径
    version: str = ""
    message: str = ""


_LOCK = threading.RLock()
_STATE: dict[str, Any] = {"module": None, "state": None, "site_dir": "", "finder": None, "failed": None}


# ---------------------------------------------------------------------------
# 只认 pymupdf / fitz 的导入器
# ---------------------------------------------------------------------------


class _ComponentFinder(importlib.abc.MetaPathFinder):
    def __init__(self, site_dir: Path) -> None:
        self.site_dir = str(site_dir)

    def find_spec(self, fullname, path=None, target=None):  # noqa: ANN001 - importlib protocol
        if "." in fullname or fullname not in _MODULE_NAMES:
            return None
        return importlib.machinery.PathFinder.find_spec(fullname, [self.site_dir])


def _install_finder(site_dir: Path) -> None:
    current = _STATE.get("finder")
    if current is not None:
        if current.site_dir == str(site_dir):
            return
        try:
            sys.meta_path.remove(current)
        except ValueError:
            pass
    finder = _ComponentFinder(site_dir)
    # 放在最后：安装包自带的模块永远优先。
    sys.meta_path.append(finder)
    _STATE["finder"] = finder


def _import_pymupdf():
    try:
        return importlib.import_module("pymupdf")
    except ImportError:
        return importlib.import_module("fitz")


def _module_version(module) -> str:
    return str(getattr(module, "VersionBind", "") or getattr(module, "__version__", "") or "")


def _version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", str(text or ""))[:3]) or (0,)


# ---------------------------------------------------------------------------
# 找到 pdf2docx 所在环境里的 PyMuPDF
# ---------------------------------------------------------------------------


def _has_pymupdf(site_dir: Path) -> bool:
    return (site_dir / "pymupdf" / "__init__.py").is_file()


def _nearby_site_dirs(exe: Path) -> list[Path]:
    """常见布局：虚拟环境 bin/ 或 Scripts\\ 的上一级、Windows 全局安装 Scripts\\ 的上一级、
    macOS「pip install --user」的 ~/Library/Python/3.x/bin 的上一级。"""
    found: list[Path] = []
    for base in (exe.parent.parent, exe.parent):
        for pattern in ("lib/python3*/site-packages", "Lib/site-packages", "lib/python/site-packages",
                        "lib/site-packages", "site-packages"):
            for candidate in sorted(base.glob(pattern)):
                if candidate.is_dir() and candidate not in found:
                    found.append(candidate)
    return found


def _shebang_interpreter(exe: Path) -> Path | None:
    """pip 生成的启动程序里记着装它的那个 Python。"""
    try:
        if exe.suffix.lower() == ".exe":
            data = exe.read_bytes()[-262144:]
            matches = re.findall(rb'#!\s*"?([^"\r\n]*?python[^"\r\n]*?\.exe)"?\s*\r?\n', data, re.I)
            return Path(matches[-1].decode("utf-8", "replace")) if matches else None
        with exe.open("rb") as handle:
            head = handle.read(4096).decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    if not head or not head[0].startswith("#!"):
        return None
    command = head[0][2:].strip()
    if command in ("/bin/sh", "/bin/bash") and len(head) > 1:
        # 路径太长或带空格时 pip 写成：'''exec' "/path/to/python" "$0" "$@"
        match = re.search(r"""exec'\s+(?:"([^"]+)"|(\S+))""", head[1])
        if match:
            return Path(match.group(1) or match.group(2))
        return None
    parts = command.split()
    if not parts:
        return None
    if Path(parts[0]).name == "env" and len(parts) > 1:
        import shutil

        located = shutil.which(parts[-1])
        return Path(located) if located else None
    return Path(parts[0])


def _interpreter_candidates(exe: Path) -> list[Path]:
    candidates: list[Path] = []
    shebang = _shebang_interpreter(exe)
    if shebang is not None:
        candidates.append(shebang)
    names = ("python.exe",) if os.name == "nt" else ("python3", "python")
    for folder in (exe.parent, exe.parent.parent):
        for name in names:
            candidates.append(folder / name)
    unique: list[Path] = []
    for candidate in candidates:
        if candidate.is_file() and candidate not in unique:
            unique.append(candidate)
    return unique


_LOCATE_CODE = (
    "import json, os, sys\n"
    "try:\n"
    "    import pymupdf as m\n"
    "except ImportError:\n"
    "    import fitz as m\n"
    "print(json.dumps({'file': os.path.abspath(m.__file__)}))\n"
)


def _site_dir_from_interpreter(python: Path) -> Path | None:
    try:
        result = subprocess.run(
            [str(python), "-c", _LOCATE_CODE],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_LOCATE_TIMEOUT,
            check=False,
            cwd=tempfile.gettempdir(),
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if result.returncode != 0:
        return None
    for line in reversed((result.stdout or "").splitlines()):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        file = Path(str(data.get("file") or ""))
        if file.name == "__init__.py":
            return file.parent.parent
    return None


def locate_site_dir(exe: Path) -> Path | None:
    """pdf2docx 可执行文件所在环境里、装着 ``pymupdf`` 包的 site-packages。"""
    try:
        exe = exe.resolve()
    except OSError:
        pass
    for site_dir in _nearby_site_dirs(exe):
        if _has_pymupdf(site_dir):
            return site_dir
    for python in _interpreter_candidates(exe):
        site_dir = _site_dir_from_interpreter(python)
        if site_dir is not None and site_dir.is_dir():
            return site_dir
    return None


# ---------------------------------------------------------------------------
# 核对 wheel 标签
# ---------------------------------------------------------------------------


def _dist_info(site_dir: Path) -> Path | None:
    try:
        entries = sorted(site_dir.iterdir())
    except OSError:
        return None
    for entry in entries:
        name = entry.name.lower()
        if name.startswith("pymupdf-") and name.endswith(".dist-info") and entry.is_dir():
            return entry
    return None


def _arch_words() -> tuple[str, ...]:
    machine = platform.machine().lower()
    if sys.platform == "darwin":
        return ("arm64", "universal2") if machine == "arm64" else ("x86_64", "universal2", "intel")
    if sys.platform == "win32":
        return ("win_amd64",) if machine in ("amd64", "x86_64") else ("win_arm64",) if "arm" in machine else ("win32",)
    return (machine,)


def _tag_compatible(tag: str) -> bool:
    parts = tag.strip().split("-")
    if len(parts) != 3:
        return False
    interpreter, abi, plat = parts
    ours = sys.version_info[0] * 100 + sys.version_info[1]
    ok_python = False
    for item in interpreter.split("."):
        match = re.fullmatch(r"cp(\d)(\d+)", item)
        if not match:
            continue
        theirs = int(match.group(1)) * 100 + int(match.group(2))
        if (abi == "abi3" and theirs <= ours) or (abi == item and theirs == ours):
            ok_python = True
    if not ok_python:
        return False
    plat = plat.lower()
    return any(word in plat for word in _arch_words())


def check_wheel(site_dir: Path) -> tuple[str, str]:
    """(version, problem)。problem 为空串表示版本和标签都对得上；读不到记录时不判。"""
    info = _dist_info(site_dir)
    if info is None:
        return "", ""
    version = info.name[len("pymupdf-"):-len(".dist-info")]
    if _version_tuple(version) < MIN_VERSION:
        return version, f"组件里的 PyMuPDF 版本过旧（{version}），请更新 pdf2docx 后重新检测"
    try:
        wheel = (info / "WHEEL").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return version, ""
    tags = [line.split(":", 1)[1].strip() for line in wheel.splitlines() if line.lower().startswith("tag:")]
    if tags and not any(_tag_compatible(tag) for tag in tags):
        need = f"{sys.version_info[0]}.{sys.version_info[1]}"
        return version, (
            f"组件里的 PyMuPDF 与本软件不兼容（{', '.join(tags)}），"
            f"请用 Python {need} 或更新版本、与本机相同架构的 Python 重新安装 pdf2docx"
        )
    return version, ""


# ---------------------------------------------------------------------------
# 子进程试加载
# ---------------------------------------------------------------------------


def _sidecar_command(*args: str) -> list[str]:
    """再起一个翻译引擎进程跑一件事：冻结的安装包里是它自己，源码运行时是 launch_sidecar.py。"""
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    launcher = Path(__file__).resolve().parents[1] / "scripts" / "launch_sidecar.py"
    return [sys.executable, str(launcher), *args]


def probe_in_subprocess(site_dir: Path, *, convert: bool = False) -> tuple[bool, str, str]:
    """(ok, version, detail)。convert=True 时还用组件里的 pdf2docx 转一页 PDF。"""
    command = _sidecar_command(PROBE_FLAG, str(site_dir), *(["--convert"] if convert else []))
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_PROBE_TIMEOUT * (3 if convert else 1),
            check=False,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return False, "", f"could not start the probe: {exc}"
    for line in reversed((result.stdout or "").splitlines()):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and "ok" in data:
            return bool(data.get("ok")), str(data.get("version") or ""), str(data.get("error") or "")
    tail = ((result.stderr or "") + (result.stdout or "")).strip()[-400:]
    return False, "", f"probe exited with code {result.returncode}: {tail}"


def exercise(module) -> bytes:
    """用一份内存里的小 PDF 走一遍原位替换会用到的几样：写字、存盘、读文字、渲染。"""
    doc = module.open()
    try:
        page = doc.new_page(width=300, height=200)
        page.insert_text((30, 60), "Probe 123 component check", fontsize=14)
        data = doc.tobytes()
    finally:
        doc.close()
    with module.open(stream=data, filetype="pdf") as check:
        page = check[0]
        if "Probe" not in page.get_text():
            raise RuntimeError("text written to the probe PDF could not be read back")
        page.get_texttrace()
        page.get_drawings()
        page.get_pixmap(dpi=36)
    return data


def probe_main(site_dir: str, convert: bool = False) -> int:
    """子进程入口（``launch_sidecar.py --probe-pdf-component <dir> [--convert]``）：结果写一行 JSON。"""
    try:
        _install_finder(Path(site_dir))
        module = _import_pymupdf()
        data = exercise(module)
        if convert:
            with tempfile.TemporaryDirectory(prefix="translator-pdf-probe-") as folder:
                pdf = Path(folder) / "probe.pdf"
                docx = Path(folder) / "probe.docx"
                pdf.write_bytes(data)
                _convert_in_process(Path(site_dir), pdf, docx)
                import zipfile

                with zipfile.ZipFile(docx) as archive:
                    if "Probe" not in archive.read("word/document.xml").decode("utf-8", "replace"):
                        raise RuntimeError("the converted Word file does not contain the probe text")
        print(json.dumps({"ok": True, "version": _module_version(module)}), flush=True)
        return 0
    except BaseException as exc:  # noqa: BLE001 - report every failure to the parent
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"[:400]}), flush=True)
        return 1


# ---------------------------------------------------------------------------
# PDF 转 Word：一键安装的组件在单独的子进程里跑 pdf2docx
# ---------------------------------------------------------------------------

CONVERT_FLAG = "--pdf2docx-convert"


def _convert_in_process(site_dir: Path, pdf: Path, docx: Path) -> None:
    # 整个组件目录放到 sys.path 最后：安装包自带的模块（python-docx、lxml……）优先，
    # NumPy、OpenCV、fontTools、pdf2docx 只在这里有。
    if str(site_dir) not in sys.path:
        sys.path.append(str(site_dir))
    from pdf2docx import Converter

    converter = Converter(str(pdf))
    try:
        converter.convert(str(docx))
    finally:
        converter.close()


def convert_main(site_dir: str, pdf: str, docx: str) -> int:
    """子进程入口（``launch_sidecar.py --pdf2docx-convert <dir> <pdf> <docx>``）。"""
    try:
        _convert_in_process(Path(site_dir), Path(pdf), Path(docx))
    except BaseException as exc:  # noqa: BLE001 - the parent reads stderr and the exit code
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


def managed_convert_command(site_dir: Path, pdf: Path, docx: Path) -> list[str]:
    return _sidecar_command(CONVERT_FLAG, str(site_dir), str(pdf), str(docx))


# ---------------------------------------------------------------------------
# 组件从哪里来：一键安装的 / 用户自己装的 pdf2docx
# ---------------------------------------------------------------------------

SOURCE_MANAGED = "managed"
SOURCE_MANUAL = "manual"


def managed_site_dir() -> Path | None:
    from core.pdf_component_install import installed_site_dir

    try:
        return installed_site_dir()
    except Exception as exc:  # noqa: BLE001 - a damaged record counts as not installed
        logger.debug(f"[PDF 处理组件] 读取一键安装记录失败：{exc!r}")
        return None


def _pdf2docx_executable() -> Path | None:
    from core.pdf2docx_plugin import resolve_pdf2docx

    try:
        return resolve_pdf2docx()
    except Exception as exc:  # noqa: BLE001 - missing optional component
        logger.debug(f"[PDF 处理组件] 查找 pdf2docx 失败：{exc!r}")
        return None


def _preferred_source() -> str:
    from core.pdf2docx_plugin import preferred_source

    try:
        return preferred_source()
    except Exception:  # noqa: BLE001 - unreadable config: default order
        return ""


def active_converter() -> tuple[str, Path] | None:
    """PDF 转 Word 用哪一个：("managed", site_dir) 或 ("manual", pdf2docx 可执行文件)。

    默认先用一键安装的；用户在插件页「选择安装位置」之后先用他选的那个。
    """
    managed = managed_site_dir()
    if _preferred_source() == SOURCE_MANUAL:
        exe = _pdf2docx_executable()
        if exe is not None:
            return SOURCE_MANUAL, exe
        return (SOURCE_MANAGED, managed) if managed is not None else None
    if managed is not None:
        return SOURCE_MANAGED, managed
    exe = _pdf2docx_executable()
    return (SOURCE_MANUAL, exe) if exe is not None else None


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------


def _bundled_module():
    """当前解释器本来就能 import（源码运行、测试环境）。冻结的安装包里排除了它。"""
    if _STATE.get("finder") is not None:
        return None
    for name in _MODULE_NAMES:
        try:
            if importlib.util.find_spec(name) is None:
                continue
        except (ImportError, ValueError):
            continue
        try:
            return _import_pymupdf()
        except Exception as exc:  # noqa: BLE001 - a broken install counts as missing
            logger.debug(f"[PDF 处理组件] 本机自带的 PyMuPDF 无法加载：{exc!r}")
            return None
    return None


def _candidate_site_dir() -> tuple[Path | None, str]:
    """(site-packages, 找不到时的原因)。"""
    active = active_converter()
    if active is None:
        return None, MISSING_MESSAGE
    source, where = active
    if source == SOURCE_MANAGED:
        return where, ""
    site_dir = locate_site_dir(where)
    if site_dir is None or not _has_pymupdf(site_dir):
        return None, "在 pdf2docx 的安装位置没有找到 PyMuPDF，请重新安装 pdf2docx 后重新检测"
    return site_dir, ""


def _load(refresh: bool) -> ComponentState:
    if _STATE.get("module") is not None:
        # 已经加载进进程里的库卸不掉；用户断开或卸载时由 component_state 另外判断。
        return _STATE["state"]
    if not refresh and _STATE.get("state") is not None:
        return _STATE["state"]

    module = _bundled_module()
    if module is not None:
        state = ComponentState(True, "bundled", _module_version(module))
        _STATE.update(module=module, state=state)
        return state

    site_dir, reason = _candidate_site_dir()
    if site_dir is None:
        state = ComponentState(False, message=reason)
        _STATE.update(state=state)
        return state
    key = _failure_key(site_dir)
    cached = _STATE.get("failed")
    if cached is not None and cached[0] == key:
        # 同一份没加载成功的 PyMuPDF：不再起子进程重试，等它换了版本再说。
        _STATE.update(state=cached[1])
        return cached[1]
    state = _try_site_dir(site_dir)
    if not state.available:
        _STATE.update(state=state, failed=(key, state))
    return state


def _failure_key(site_dir: Path) -> tuple[str, float]:
    try:
        return str(site_dir), (site_dir / "pymupdf" / "__init__.py").stat().st_mtime
    except OSError:
        return str(site_dir), 0.0


_UNLOADABLE = "组件里的 PyMuPDF 无法在本软件中加载，请重新安装后重新检测"


def _try_site_dir(site_dir: Path) -> ComponentState:
    version, problem = check_wheel(site_dir)
    if problem:
        return ComponentState(False, str(site_dir), version, problem)
    ok, probed_version, detail = probe_in_subprocess(site_dir)
    if not ok:
        logger.warning(f"[PDF 处理组件] PyMuPDF 试加载失败（{site_dir}）：{detail}")
        return ComponentState(False, str(site_dir), version, _UNLOADABLE)
    try:
        _install_finder(site_dir)
        module = _import_pymupdf()
    except Exception as exc:  # noqa: BLE001 - the probe passed, so this is unexpected
        logger.warning(f"[PDF 处理组件] PyMuPDF 加载失败（{site_dir}）：{exc!r}")
        return ComponentState(False, str(site_dir), version, _UNLOADABLE)
    state = ComponentState(True, str(site_dir), probed_version or _module_version(module) or version)
    _STATE.update(module=module, state=state, site_dir=str(site_dir))
    logger.info(f"[PDF 处理组件] 已从 {site_dir} 加载 PyMuPDF {state.version}")
    return state


def component_state(*, refresh: bool = False) -> ComponentState:
    """组件现在能不能用。refresh=True 时重新检测（插件页、设置页打开时）。"""
    with _LOCK:
        state = _load(refresh)
        if state.available and state.source != "bundled" and active_converter() is None:
            # 用户在插件页断开或卸载了：已加载的库留在进程里，但不再使用。
            return ComponentState(False, message=MISSING_MESSAGE)
        return state


def forget_failures() -> None:
    """装好、换了位置之后重新检测，不沿用之前「加载不了」的结论。"""
    with _LOCK:
        _STATE.update(failed=None)
        if _STATE.get("module") is None:
            _STATE.update(state=None)


def is_available(*, refresh: bool = False) -> bool:
    try:
        return component_state(refresh=refresh).available
    except Exception as exc:  # noqa: BLE001 - never let a probe break a caller
        logger.debug(f"[PDF 处理组件] 检测失败：{exc!r}")
        return False


def require_fitz():
    """PyMuPDF 模块；不可用时抛 PdfComponentMissing（是 ImportError 的子类）。

    已加载过就直接返回，不再检测：要不要用这个组件（比如用户刚在插件页断开）由调用方
    在任务开始时用 ``is_available()`` 判断，这里每次 ``fitz.xxx`` 都会走到。"""
    module = _STATE.get("module")
    if module is not None:
        return module
    with _LOCK:
        state = _load(False)
        module = _STATE.get("module")
    if not state.available or module is None:
        raise PdfComponentMissing(state.message or MISSING_MESSAGE)
    return module


class _LazyFitz(types.ModuleType):
    """``fitz = lazy_fitz()``：模块照常 import，第一次用到 ``fitz.xxx`` 时才加载组件。"""

    def __getattr__(self, name: str):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return getattr(require_fitz(), name)


def lazy_fitz() -> types.ModuleType:
    return _LazyFitz("fitz")


def reset_for_tests() -> None:
    with _LOCK:
        finder = _STATE.get("finder")
        if finder is not None:
            try:
                sys.meta_path.remove(finder)
            except ValueError:
                pass
        _STATE.update(module=None, state=None, site_dir="", finder=None, failed=None)
