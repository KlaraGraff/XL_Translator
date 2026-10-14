"""第三方格式适配器的检查、安装、登记与卸载（扩展性阶段 E）。

一个扩展就是一个格式适配器目录（``adapter.json`` + ``adapter.py``，见 ``core/format_adapter.py``），
用户从文件夹或 ``.zip`` 添加，或从扩展列表下载（``core/adapter_registry.py``）。这里只管文件：

- ``inspect_package`` / ``preview``：读清单并做安装前检查，供确认弹窗和 ``adapter test`` 用；
- ``install``：确认后拷进 ``<应用数据>/plugins/adapters/<id>/<版本>-<随机>/``，登记进同目录的
  ``installed.json``；同一 id 已装时换成新目录，成功后再删旧的；
- ``uninstall``：删掉登记和我们自己的拷贝（用户原来的文件夹、zip 不动）。

把翻译类型注册到侧栏、选用哪种宿主在 ``api/extension_install_job.py``（本模块在 ``core`` 下，
不能从 ``api`` 导入）。

检查只拦「明显越界」：只收纯 Python 和文本文件、体积上限、拒绝原生库和可执行文件、静态扫描
网络、启动其他程序、加载原生或动态代码的写法。它**不是沙箱**：扩展仍以用户权限运行、能读写
用户的文件，所以安装前必须由用户确认来源可信。
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import time
import uuid
import zipfile
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from loguru import logger

from core.app_paths import get_app_data_dir
from core.format_adapter import (
    ADAPTER_MODULE_FILE,
    BUNDLED_ADAPTERS_DIR,
    MANIFEST_FILE,
    AdapterManifest,
    AdapterManifestError,
    discover_adapters,
    parse_manifest,
)

#: 一个扩展包（含样例）解压后的总体积上限。
MAX_PACKAGE_BYTES = 2 * 1024 * 1024
#: 文件数上限：正常的适配器只有几个文件，再多就不是适配器了。
MAX_PACKAGE_FILES = 200
#: 添加本地 zip 时 zip 文件本身的上限（里面可以有被忽略的大目录，只解需要的）。
MAX_ZIP_BYTES = 32 * 1024 * 1024

#: 样例目录：只给 ``adapter test`` 和扩展列表的检查用，不装进应用数据目录。
SAMPLES_DIR = "samples"
#: 包根目录下不进扩展包的目录：打包产物、作者的脚本与测试。
EXCLUDED_TOP_DIRS = frozenset({"dist", "build", "scripts", "tests", "node_modules"})
#: 扩展包里允许的文件类型（``samples/`` 之外）。
ALLOWED_SUFFIXES = frozenset({".py", ".json", ".md", ".txt"})
#: 没有扩展名也允许的文件：许可证、作者说明。
ALLOWED_PLAIN_NAMES = frozenset({"LICENSE", "LICENCE", "NOTICE", "COPYING", "AUTHORS"})
#: 任何位置都拒绝的文件：原生库、可执行程序、脚本、编译过的 Python。
FORBIDDEN_SUFFIXES = frozenset(
    {
        ".so", ".pyd", ".dll", ".dylib", ".exe", ".msi", ".app", ".bat", ".cmd", ".com", ".sh",
        ".command", ".ps1", ".vbs", ".scr", ".jar", ".pyc", ".pyo", ".bin",
    }
)
#: 可执行文件开头的几个字节：ELF、Mach-O（含通用二进制）、Windows PE。
_EXECUTABLE_MAGIC = (
    b"\x7fELF",
    b"\xfe\xed\xfa\xce",
    b"\xfe\xed\xfa\xcf",
    b"\xce\xfa\xed\xfe",
    b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",
    b"MZ",
)

#: 静态扫描拦下的模块（按顶层包名），以及拦下的原因。
FORBIDDEN_MODULES: dict[str, str] = {
    **dict.fromkeys(
        (
            "socket", "ssl", "http", "urllib", "urllib3", "httpx", "requests", "aiohttp", "ftplib",
            "smtplib", "poplib", "imaplib", "telnetlib", "nntplib", "xmlrpc", "socketserver",
            "webbrowser", "asyncio",
        ),
        "网络访问",
    ),
    **dict.fromkeys(("subprocess", "multiprocessing", "pty"), "启动其他程序"),
    **dict.fromkeys(("ctypes", "cffi"), "加载原生代码"),
    **dict.fromkeys(("importlib", "pickle", "marshal", "runpy", "code", "codeop", "zipimport"), "动态加载代码"),
}
#: 拦下的内置函数调用（动态执行代码）。
FORBIDDEN_BUILTINS = frozenset({"exec", "eval", "compile", "__import__"})
#: 拦下的 ``os`` 函数（启动其他程序）；``exec*``、``spawn*`` 按前缀。
FORBIDDEN_OS_FUNCTIONS = frozenset({"system", "popen", "startfile", "fork", "forkpty", "posix_spawn", "posix_spawnp"})
_FORBIDDEN_OS_PREFIXES = ("exec", "spawn")

#: 安装前请用户确认的那句话（「扩展」页的确认弹窗、``adapter install`` 的提示用同一句）。
CONFIRM_NOTICE = "扩展由第三方作者编写，以你的用户权限运行，可以读写你的文件；只安装你信任的来源。"

REGISTRY_FILE = "installed.json"
REGISTRY_SCHEMA = 1
ORIGIN_LOCAL = "local"
ORIGIN_REGISTRY = "registry"

_CHUNK = 256 * 1024
_SAFE_VERSION = re.compile(r"[^0-9A-Za-z._-]+")
_LOCK = threading.RLock()


class AdapterInstallError(RuntimeError):
    """检查没通过或安装失败；消息是给用户看的一句话（多条问题用「；」连接）。"""

    def __init__(self, message: str, problems: Iterable[str] = ()) -> None:
        super().__init__(message)
        self.problems = list(problems) or [message]


class AdapterInstallCancelled(AdapterInstallError):
    pass


# ── 读包 ──────────────────────────────────────────────────────────────────────


@dataclass
class PackageReport:
    """``inspect_package`` 的结果：能不能装、装哪些文件、要提醒用户什么。"""

    root: Path
    manifest: AdapterManifest | None = None
    #: 会装进应用数据目录的文件（相对 ``root``，``/`` 分隔，已排序；不含样例）。
    files: list[str] = field(default_factory=list)
    #: ``samples/`` 下的样例（相对 ``root``）。
    samples: list[str] = field(default_factory=list)
    #: 全部文件（含样例）的总字节数。
    size: int = 0
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.manifest is not None and not self.problems


def _relative_parts(name: str) -> tuple[str, ...] | None:
    """zip 成员名或相对路径 → 各级名字；指向外面、绝对路径、带盘符的返回 None。"""
    text = str(name).replace("\\", "/")
    path = PurePosixPath(text)
    if path.is_absolute() or text.startswith("/"):
        return None
    parts = tuple(part for part in path.parts if part not in ("", "."))
    if any(part == ".." or ":" in part for part in parts):
        return None
    return parts


def _ignored(parts: tuple[str, ...]) -> bool:
    """作者工作目录里常见、但不属于扩展包的东西：隐藏文件、缓存、macOS 压缩残留、打包产物。"""
    if not parts:
        return True
    if parts[0] in EXCLUDED_TOP_DIRS and len(parts) > 1:
        return True
    return any(
        part.startswith(".") or part in ("__pycache__", "__MACOSX") or part.endswith((".pyc", ".pyo"))
        for part in parts
    )


def _looks_executable(head: bytes) -> bool:
    return any(head.startswith(magic) for magic in _EXECUTABLE_MAGIC)


def _file_problem(parts: tuple[str, ...], head: bytes) -> str | None:
    """这一个文件能不能进扩展包；不能时返回原因。"""
    relative = "/".join(parts)
    suffix = PurePosixPath(parts[-1]).suffix.lower()
    if suffix in FORBIDDEN_SUFFIXES or _looks_executable(head):
        return f"{relative}：不允许包含原生库或可执行文件"
    if parts[0] == SAMPLES_DIR and len(parts) > 1:
        return None
    if suffix in ALLOWED_SUFFIXES or (not suffix and parts[-1].upper() in ALLOWED_PLAIN_NAMES):
        return None
    return f"{relative}：扩展包只能包含 .py、.json、.md、.txt 文件（样例放在 samples/ 下）"


def _scan_imports(relative: str, source: str) -> list[str]:
    """静态扫描一份 Python 源码，列出明显越界的写法（不是沙箱，只拦明显的）。"""
    try:
        tree = ast.parse(source, filename=relative)
    except SyntaxError as exc:
        return [f"{relative} 第 {exc.lineno or 0} 行：无法解析（{exc.msg}）"]
    problems: list[str] = []

    def flag(node: ast.AST, what: str, reason: str) -> None:
        problems.append(f"{relative} 第 {getattr(node, 'lineno', 0)} 行：{what}（{reason}）")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in FORBIDDEN_MODULES:
                    flag(node, f"导入了 {alias.name}", FORBIDDEN_MODULES[top])
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            module = node.module or ""
            top = module.split(".")[0]
            if top in FORBIDDEN_MODULES:
                flag(node, f"导入了 {module}", FORBIDDEN_MODULES[top])
            elif top == "os":
                for alias in node.names:
                    if _forbidden_os(alias.name):
                        flag(node, f"导入了 os.{alias.name}", "启动其他程序")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_BUILTINS:
            flag(node, f"调用了 {node.func.id}()", "动态执行代码")
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
            and _forbidden_os(node.attr)
        ):
            flag(node, f"使用了 os.{node.attr}", "启动其他程序")
    return problems


def _forbidden_os(name: str) -> bool:
    return name in FORBIDDEN_OS_FUNCTIONS or name.startswith(_FORBIDDEN_OS_PREFIXES)


def _defines_functions(source: str) -> set[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    } | {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    } | {
        alias.asname or alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }


def bundled_ids() -> set[str]:
    """随应用提供的翻译类型的 id：装进来的扩展不能占用。"""
    found, _problems = discover_adapters(BUNDLED_ADAPTERS_DIR)
    return {manifest.id for _directory, manifest in found}


def inspect_package(
    directory: str | Path,
    *,
    reserved_ids: Iterable[str] | None = None,
) -> PackageReport:
    """检查一个已解开的扩展包目录，把所有问题一次列全（``adapter test`` 一次看到全部）。

    ``reserved_ids`` 是不能占用的 id（默认：随应用提供的类型）。
    """
    root = Path(directory)
    report = PackageReport(root=root)
    if not root.is_dir():
        report.problems.append(f"找不到文件夹：{root}")
        return report

    raw_manifest: Any = None
    for current, subdirectories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        relative_dir = current_path.relative_to(root).parts
        kept_dirs = []
        for name in sorted(subdirectories):
            parts = (*relative_dir, name)
            if _ignored(parts):
                continue
            if (current_path / name).is_symlink():
                report.problems.append(f"{'/'.join(parts)}：不允许包含符号链接")
                continue
            kept_dirs.append(name)
        subdirectories[:] = kept_dirs
        for name in sorted(filenames):
            parts = (*relative_dir, name)
            if _ignored(parts):
                continue
            path = current_path / name
            relative = "/".join(parts)
            if path.is_symlink():
                report.problems.append(f"{relative}：不允许包含符号链接")
                continue
            try:
                size = path.stat().st_size
                with path.open("rb") as handle:
                    head = handle.read(4)
            except OSError as exc:
                report.problems.append(f"{relative}：无法读取（{exc.strerror or exc}）")
                continue
            problem = _file_problem(parts, head)
            if problem:
                report.problems.append(problem)
                continue
            report.size += size
            if parts[0] == SAMPLES_DIR and len(parts) > 1:
                report.samples.append(relative)
            else:
                report.files.append(relative)

    if len(report.files) + len(report.samples) > MAX_PACKAGE_FILES:
        report.problems.append(f"文件过多（{len(report.files) + len(report.samples)} 个，上限 {MAX_PACKAGE_FILES} 个）")
    if report.size > MAX_PACKAGE_BYTES:
        report.problems.append(
            f"扩展包过大（{report.size / 1024 / 1024:.1f} MB，上限 {MAX_PACKAGE_BYTES // 1024 // 1024} MB）"
        )

    if MANIFEST_FILE not in report.files:
        report.problems.insert(0, f"没有 {MANIFEST_FILE}：所选内容不是 Translator 扩展")
        return report
    if ADAPTER_MODULE_FILE not in report.files:
        report.problems.append(f"没有 {ADAPTER_MODULE_FILE}")

    try:
        raw_manifest = json.loads((root / MANIFEST_FILE).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        report.problems.insert(0, f"{MANIFEST_FILE} 无法读取：{exc}")
        return report
    if isinstance(raw_manifest, dict) and raw_manifest.get("requires"):
        report.problems.append("扩展声明了需要额外安装的第三方库（requires），当前版本不支持")
        raw_manifest = {key: value for key, value in raw_manifest.items() if key != "requires"}
    try:
        report.manifest = parse_manifest(raw_manifest, source=MANIFEST_FILE)
    except AdapterManifestError as exc:
        report.problems.insert(0, str(exc))

    for relative in report.files:
        if not relative.endswith(".py"):
            continue
        try:
            source = (root / relative).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            report.problems.append(f"{relative}：无法按 UTF-8 读取（{exc}）")
            continue
        report.problems.extend(_scan_imports(relative, source))
        if relative == ADAPTER_MODULE_FILE:
            defined = _defines_functions(source)
            for name in ("extract", "write"):
                if name not in defined:
                    report.problems.append(f"{ADAPTER_MODULE_FILE} 缺少函数 {name}()")

    manifest = report.manifest
    if manifest is not None:
        reserved = set(bundled_ids() if reserved_ids is None else reserved_ids)
        if manifest.id in reserved:
            report.problems.append(f"id「{manifest.id}」与应用自带的翻译类型相同，请换一个 id")
        if not manifest.license:
            report.warnings.append("清单未注明许可证（license）")
        if not report.samples:
            report.warnings.append("没有样例文件（samples/）")
    return report


def tree_sha256(root: Path, files: Iterable[str]) -> str:
    """一组文件的内容指纹：按相对路径排序，路径和各自的 SHA-256 一起算。"""
    digest = hashlib.sha256()
    for relative in sorted(files):
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(file_sha256(root / relative).encode("ascii") + b"\n")
    return digest.hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ── 来源：文件夹或 zip ────────────────────────────────────────────────────────


@dataclass
class _Source:
    """准备好的来源：``root`` 是包根目录；zip 解在临时目录里，用完由 ``cleanup`` 删掉。"""

    path: Path
    kind: str
    root: Path
    sha256: str
    temporary: Path | None = None

    def cleanup(self) -> None:
        if self.temporary is not None:
            shutil.rmtree(self.temporary, ignore_errors=True)


@contextmanager
def unpacked(source: str | Path) -> Iterator[Path]:
    """文件夹或 zip 的包根目录（``adapter test`` 用）；zip 解在临时目录里，用完删掉。"""
    prepared = _prepare(source)
    try:
        yield prepared.root
    finally:
        prepared.cleanup()


def _package_root(base: Path) -> Path:
    """包根目录：``adapter.json`` 就在这里，或这里只有一个子目录且它里面有。"""
    if (base / MANIFEST_FILE).is_file():
        return base
    children = [child for child in base.iterdir() if not _ignored((child.name,))]
    if len(children) == 1 and children[0].is_dir() and (children[0] / MANIFEST_FILE).is_file():
        return children[0]
    return base


def _zip_prefix(names: list[tuple[str, ...]]) -> tuple[str, ...]:
    if (MANIFEST_FILE,) in names:
        return ()
    tops = {parts[0] for parts in names if parts}
    if len(tops) == 1:
        top = next(iter(tops))
        if (top, MANIFEST_FILE) in names:
            return (top,)
    return ()


def _extract_zip(path: Path, target: Path) -> None:
    """只解出扩展包会用到的成员；路径穿越、符号链接、超体积在解之前就拒绝。"""
    try:
        if path.stat().st_size > MAX_ZIP_BYTES:
            raise AdapterInstallError(f"zip 文件过大（上限 {MAX_ZIP_BYTES // 1024 // 1024} MB）")
        with zipfile.ZipFile(path) as archive:
            members: list[tuple[zipfile.ZipInfo, tuple[str, ...]]] = []
            for info in archive.infolist():
                parts = _relative_parts(info.filename)
                if parts is None:
                    raise AdapterInstallError(f"zip 中含有指向压缩包外的路径：{info.filename}")
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise AdapterInstallError(f"{info.filename}：不允许包含符号链接")
                if info.is_dir() or _ignored(parts):
                    continue
                members.append((info, parts))
            prefix = _zip_prefix([parts for _info, parts in members])
            selected = [
                (info, parts[len(prefix):])
                for info, parts in members
                if parts[: len(prefix)] == prefix and len(parts) > len(prefix)
            ]
            selected = [(info, parts) for info, parts in selected if not _ignored(parts)]
            total = sum(info.file_size for info, _parts in selected)
            if total > MAX_PACKAGE_BYTES:
                raise AdapterInstallError(
                    f"扩展包过大（解压后 {total / 1024 / 1024:.1f} MB，上限 {MAX_PACKAGE_BYTES // 1024 // 1024} MB）"
                )
            if len(selected) > MAX_PACKAGE_FILES:
                raise AdapterInstallError(f"文件过多（{len(selected)} 个，上限 {MAX_PACKAGE_FILES} 个）")
            root = target.resolve()
            for info, parts in selected:
                destination = (root / Path(*parts)).resolve()
                if root not in destination.parents:
                    raise AdapterInstallError(f"zip 中含有指向压缩包外的路径：{info.filename}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, destination.open("wb") as handle:
                    copied = 0
                    for chunk in iter(lambda: source.read(_CHUNK), b""):
                        copied += len(chunk)
                        if copied > info.file_size:
                            raise AdapterInstallError("zip 文件已损坏")
                        handle.write(chunk)
    except AdapterInstallError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, RuntimeError, ValueError) as exc:
        logger.warning(f"[扩展] 解开 {path.name} 失败：{exc!r}")
        raise AdapterInstallError("无法读取 zip 文件，文件可能已损坏或已加密") from exc


def _prepare(source: str | Path) -> _Source:
    path = Path(str(source or "").strip()).expanduser()
    if not str(source or "").strip():
        raise AdapterInstallError("请选择扩展所在的文件夹或 zip 文件")
    if path.is_dir():
        root = _package_root(path)
        report = inspect_package(root, reserved_ids=())
        return _Source(path=path, kind="folder", root=root, sha256=tree_sha256(root, report.files))
    if path.is_file():
        if path.suffix.lower() != ".zip":
            raise AdapterInstallError("请选择扩展所在的文件夹或 .zip 文件")
        temporary = Path(tempfile.mkdtemp(prefix="translator-extension-"))
        try:
            _extract_zip(path, temporary)
            return _Source(path=path, kind="zip", root=temporary, sha256=file_sha256(path), temporary=temporary)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
    raise AdapterInstallError(f"找不到文件或文件夹：{path}")


# ── 登记 ──────────────────────────────────────────────────────────────────────


def install_root() -> Path:
    """装进来的扩展都在这里：``<应用数据>/plugins/adapters``。"""
    return get_app_data_dir() / "plugins" / "adapters"


def _registry_path() -> Path:
    return install_root() / REGISTRY_FILE


def _write_registry(records: dict[str, dict[str, Any]]) -> None:
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    data = {"schema": REGISTRY_SCHEMA, "adapters": {key: records[key] for key in sorted(records)}}
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _rebuild_registry() -> dict[str, dict[str, Any]]:
    """登记文件读不动时，按磁盘上装好的目录重建：每个 id 取最近装的那一份。"""
    records: dict[str, dict[str, Any]] = {}
    root = install_root()
    if not root.is_dir():
        return records
    for id_dir in sorted(path for path in root.iterdir() if path.is_dir() and not path.name.startswith(".")):
        candidates = sorted(
            (path for path in id_dir.iterdir() if path.is_dir() and not path.name.startswith(".")),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for candidate in candidates:
            try:
                manifest = parse_manifest(
                    json.loads((candidate / MANIFEST_FILE).read_text(encoding="utf-8-sig")),
                    source=f"{id_dir.name}/{MANIFEST_FILE}",
                )
            except (OSError, ValueError, AdapterManifestError):
                continue
            if manifest.id != id_dir.name:
                continue
            records[manifest.id] = _record(
                manifest,
                directory=candidate,
                source="",
                origin=ORIGIN_LOCAL,
                sha256="",
                size=sum(path.stat().st_size for path in candidate.rglob("*") if path.is_file()),
                installed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(candidate.stat().st_mtime)),
            )
            break
    return records


def read_registry() -> dict[str, dict[str, Any]]:
    """已登记的扩展（id → 登记项）。

    登记文件损坏时不报错停住：先把它改名留作备份，再按磁盘上的目录重建一份可用的。
    """
    path = _registry_path()
    with _LOCK:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            logger.warning(f"[扩展] 无法读取 {path}：{exc}")
            return {}
        try:
            data = json.loads(text)
            if not isinstance(data, dict) or not isinstance(data.get("adapters"), dict):
                raise ValueError("unexpected structure")
            records = {
                str(key): dict(value)
                for key, value in data["adapters"].items()
                if isinstance(value, dict) and value.get("dir")
            }
        except (ValueError, TypeError) as exc:
            backup = path.with_name(f"{path.name}.broken-{time.strftime('%Y%m%d-%H%M%S')}")
            try:
                path.replace(backup)
            except OSError:
                backup = path
            records = _rebuild_registry()
            logger.warning(
                f"[扩展] 登记文件无法读取（{exc}），已备份为 {backup.name}，并按已安装的目录重建了 {len(records)} 项"
            )
            try:
                _write_registry(records)
            except OSError as write_error:
                logger.warning(f"[扩展] 重建的登记文件无法写入：{write_error}")
        return records


def _record(
    manifest: AdapterManifest,
    *,
    directory: Path,
    source: str,
    origin: str,
    sha256: str,
    size: int,
    installed_at: str | None = None,
) -> dict[str, Any]:
    return {
        "id": manifest.id,
        "version": manifest.version,
        "label": dict(manifest.label),
        "dir": directory.relative_to(install_root()).as_posix(),
        "source": source,
        "origin": origin,
        "sha256": sha256,
        "size": size,
        "installed_at": installed_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


@dataclass
class InstalledAdapter:
    """一个登记过的扩展；``problem`` 非空时它装坏了（目录丢了、清单读不动、与本应用不兼容）。"""

    record: dict[str, Any]
    directory: Path
    manifest: AdapterManifest | None = None
    problem: str = ""

    @property
    def id(self) -> str:
        return str(self.record.get("id") or "")

    def to_dict(self) -> dict[str, Any]:
        data = dict(self.record)
        data["ok"] = self.manifest is not None and not self.problem
        data["problem"] = self.problem
        return data


def installed_adapters() -> list[InstalledAdapter]:
    """按 id 排序的已装扩展，每一项都核对过目录和清单；坏的也列出来（带原因），由调用方决定怎么显示。"""
    root = install_root()
    result: list[InstalledAdapter] = []
    for adapter_id, record in sorted(read_registry().items()):
        record = {**record, "id": adapter_id}
        relative = _relative_parts(str(record.get("dir") or ""))
        directory = root.joinpath(*relative) if relative else root / adapter_id
        item = InstalledAdapter(record=record, directory=directory)
        if not relative or relative[0] != adapter_id:
            item.problem = "登记信息不完整"
        elif not (directory / MANIFEST_FILE).is_file() or not (directory / ADAPTER_MODULE_FILE).is_file():
            item.problem = "扩展文件已丢失，请重新安装"
        else:
            try:
                manifest = parse_manifest(
                    json.loads((directory / MANIFEST_FILE).read_text(encoding="utf-8-sig")),
                    source=f"{adapter_id}/{MANIFEST_FILE}",
                )
            except (OSError, ValueError) as exc:
                item.problem = str(exc) if isinstance(exc, AdapterManifestError) else f"清单无法读取：{exc}"
            else:
                if manifest.id != adapter_id:
                    item.problem = "登记信息与清单不一致，请重新安装"
                else:
                    item.manifest = manifest
        result.append(item)
    return result


# ── 预览、安装、卸载 ──────────────────────────────────────────────────────────


def version_key(value: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", str(value or ""))
    return tuple(int(part) for part in parts[:4]) or (0,)


def _summary(report: PackageReport, source: _Source, installed: dict[str, Any] | None) -> dict[str, Any]:
    manifest = report.manifest
    assert manifest is not None
    installed_version = str((installed or {}).get("version") or "")
    if not installed:
        action = "install"
    elif version_key(manifest.version) > version_key(installed_version):
        action = "upgrade"
    elif version_key(manifest.version) < version_key(installed_version):
        action = "downgrade"
    else:
        action = "reinstall"
    warnings = list(report.warnings)
    if action == "upgrade":
        warnings.insert(0, f"将把已安装的 {installed_version} 更新为 {manifest.version}")
    elif action == "downgrade":
        warnings.insert(0, f"将以较早的版本 {manifest.version} 替换已安装的 {installed_version}")
    elif action == "reinstall":
        warnings.insert(0, f"已安装同一版本（{installed_version}），将重新安装")
    return {
        "id": manifest.id,
        "key": manifest.key,
        "version": manifest.version,
        "label": dict(manifest.label),
        "file_noun": dict(manifest.file_noun),
        "extensions": list(manifest.extensions),
        "license": manifest.license,
        "repo": manifest.repo,
        "source": str(source.path),
        "kind": source.kind,
        "files": list(report.files),
        "file_count": len(report.files),
        "sample_count": len(report.samples),
        "size": report.size,
        "sha256": source.sha256,
        "warnings": warnings,
        "action": action,
        "installed_version": installed_version,
    }


def _checked(source: _Source, reserved_ids: Iterable[str] | None) -> PackageReport:
    report = inspect_package(source.root, reserved_ids=reserved_ids)
    if not report.ok:
        problems = report.problems or ["无法读取扩展清单"]
        raise AdapterInstallError("；".join(problems[:6]) + ("；……" if len(problems) > 6 else ""), problems)
    return report


def preview(source: str | Path, *, reserved_ids: Iterable[str] | None = None) -> dict[str, Any]:
    """安装前的检查与摘要（确认弹窗用）；不改动任何东西。检查不过时抛 ``AdapterInstallError``。"""
    prepared = _prepare(source)
    try:
        report = _checked(prepared, reserved_ids)
        assert report.manifest is not None
        return _summary(report, prepared, read_registry().get(report.manifest.id))
    finally:
        prepared.cleanup()


def _discard(path: Path) -> None:
    """删掉我们自己的一份拷贝：先改名再删，删不掉的留给下次。"""
    if not path.exists():
        return
    trash = path.with_name(f".trash-{uuid.uuid4().hex[:8]}")
    try:
        path.replace(trash)
    except OSError:
        trash = path
    shutil.rmtree(trash, ignore_errors=True)


def _sweep(id_dir: Path, keep: Path | None) -> None:
    """删掉这个 id 下除了 ``keep`` 以外的版本目录和上次没删掉的残留。"""
    if not id_dir.is_dir():
        return
    for path in list(id_dir.iterdir()):
        if keep is not None and path == keep:
            continue
        if path.is_dir():
            _discard(path)
        else:
            path.unlink(missing_ok=True)


def install(
    source: str | Path,
    *,
    confirmed: bool,
    expected_sha256: str = "",
    reserved_ids: Iterable[str] | None = None,
    origin: str = ORIGIN_LOCAL,
    source_label: str = "",
    cancel: threading.Event | None = None,
    before_replace: Callable[[AdapterManifest], None] | None = None,
) -> dict[str, Any]:
    """检查通过并经用户确认后安装；返回登记项（另带 ``label`` 等摘要字段）。

    ``confirmed`` 为 False 时直接拒绝：安装必须经过确认弹窗或命令行的 ``--yes``。
    ``expected_sha256`` 是确认时看到的那份内容的指纹，安装时内容变了就拒绝。
    ``before_replace`` 在换上新目录之前调用（调用方借此拒绝正被任务使用的类型）。
    """
    if not confirmed:
        raise AdapterInstallError("安装扩展前需要确认来源可信")
    prepared = _prepare(source)
    try:
        report = _checked(prepared, reserved_ids)
        manifest = report.manifest
        assert manifest is not None
        if expected_sha256 and expected_sha256 != prepared.sha256:
            raise AdapterInstallError("所选文件在确认后发生了变化，请重新添加")
        if cancel is not None and cancel.is_set():
            raise AdapterInstallCancelled("安装已取消。")
        with _LOCK:
            id_dir = install_root() / manifest.id
            staging = id_dir / f".staging-{uuid.uuid4().hex[:8]}"
            try:
                for relative in report.files:
                    destination = staging.joinpath(*relative.split("/"))
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(prepared.root.joinpath(*relative.split("/")), destination)
                if tree_sha256(staging, report.files) != tree_sha256(prepared.root, report.files):
                    raise AdapterInstallError("复制扩展文件时出错，请重试")
                if before_replace is not None:
                    before_replace(manifest)
                final_dir = id_dir / f"{_SAFE_VERSION.sub('_', manifest.version)[:40]}-{uuid.uuid4().hex[:8]}"
                staging.replace(final_dir)
            except OSError as exc:
                logger.warning(f"[扩展] 安装 {manifest.id} 时写入失败：{exc!r}")
                raise AdapterInstallError("无法写入应用数据目录，请检查磁盘空间和权限") from exc
            finally:
                shutil.rmtree(staging, ignore_errors=True)
            records = read_registry()
            records[manifest.id] = _record(
                manifest,
                directory=final_dir,
                source=source_label or str(prepared.path),
                origin=origin,
                sha256=prepared.sha256,
                size=report.size,
            )
            try:
                _write_registry(records)
            except OSError as exc:
                _discard(final_dir)
                raise AdapterInstallError("无法写入应用数据目录，请检查磁盘空间和权限") from exc
            _sweep(id_dir, keep=final_dir)
        logger.info(f"[扩展] 已安装 {manifest.id} {manifest.version}（来源：{source_label or prepared.path}）")
        return {**records[manifest.id], "key": manifest.key, "extensions": list(manifest.extensions)}
    finally:
        prepared.cleanup()


def uninstall(adapter_id: str) -> dict[str, Any] | None:
    """删掉登记和我们自己的拷贝；没装过时返回 None。该类型的设置（``settings.extensions``）保留。"""
    adapter_id = str(adapter_id or "").strip().lower()
    with _LOCK:
        records = read_registry()
        record = records.pop(adapter_id, None)
        id_dir = install_root() / adapter_id
        if record is None and not id_dir.is_dir():
            return None
        if record is not None:
            _write_registry(records)
        if _relative_parts(adapter_id) == (adapter_id,):
            _discard(id_dir)
    logger.info(f"[扩展] 已卸载 {adapter_id}")
    return record


__all__ = [
    "ALLOWED_SUFFIXES",
    "FORBIDDEN_MODULES",
    "MAX_PACKAGE_BYTES",
    "ORIGIN_LOCAL",
    "ORIGIN_REGISTRY",
    "SAMPLES_DIR",
    "AdapterInstallCancelled",
    "AdapterInstallError",
    "InstalledAdapter",
    "PackageReport",
    "bundled_ids",
    "file_sha256",
    "inspect_package",
    "install",
    "install_root",
    "installed_adapters",
    "preview",
    "read_registry",
    "tree_sha256",
    "uninstall",
    "version_key",
]
