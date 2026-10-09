"""Optional CAD plugin lifecycle and ODA connector discovery.

The first-party adapter contains no ODA binaries. ODA remains a separately
licensed, user-provided dependency selected through the connector endpoint.
"""
from __future__ import annotations

import contextlib
import ctypes
import ctypes.wintypes
import hashlib
import json
import os
import platform
import re
import shutil
import threading
import uuid
from pathlib import Path
from typing import Any, Iterator

from app_meta import APP_VERSION
from config import APP_DATA_DIR

PLUGIN_ID = "cad-support"
PLUGIN_VERSION = "0.1.0"
OFFICIAL_DOWNLOAD_URL = "https://www.opendesign.com/guestfiles/oda_file_converter"
_REQUIRED_CONVERTER_NAMES = ("ODAFileConverter", "ODAFileConverter.exe")
_MANIFEST_KEYS = {
    "id",
    "version",
    "adapter",
    "sha256",
    "min_os",
    "min_core_version",
    "max_core_version",
    "compatible_core",
}
_SUPPORTED_MAC_ARCHES = {"arm64", "x86_64", "amd64"}
_SUPPORTED_WINDOWS_ARCHES = {"amd64", "x86_64"}
_PKG_LOCK = threading.RLock()
_LOCK_STATE = threading.local()


def _app_data_dir() -> Path:
    value = str(os.environ.get("TRANSLATOR_APP_DATA_DIR") or "").strip()
    return Path(value).expanduser() if value else Path(APP_DATA_DIR)


def plugin_root() -> Path:
    return _app_data_dir() / "plugins" / PLUGIN_ID


def _oda_config_path(root: Path | None = None) -> Path:
    return (root or plugin_root()) / "oda.json"


def _lock_path() -> Path:
    return plugin_root().parent / f".{PLUGIN_ID}.lock"


@contextlib.contextmanager
def _plugin_lock() -> Iterator[None]:
    """Serialize lifecycle changes in this process and across processes."""
    with _PKG_LOCK:
        depth = int(getattr(_LOCK_STATE, "depth", 0))
        if depth:
            _LOCK_STATE.depth = depth + 1
            try:
                yield
            finally:
                _LOCK_STATE.depth = depth
            return
        _LOCK_STATE.depth = 1
        lock_path = _lock_path()
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = lock_path.open("a+b")
        except Exception:
            _LOCK_STATE.depth = 0
            raise
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                except OSError:
                    pass
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            handle.close()
            _LOCK_STATE.depth = 0


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _platform_info() -> dict[str, str]:
    system = platform.system().lower()
    arch = platform.machine().lower()
    version = platform.release().lower()
    if system == "darwin":
        version = str(platform.mac_ver()[0] or version)
    return {"system": system, "arch": arch, "version": version}


def _version_tuple(value: str) -> tuple[int, ...]:
    values = re.findall(r"\d+", str(value or ""))
    return tuple(int(item) for item in values[:4]) or (0,)


def _platform_supported(info: dict[str, str]) -> bool:
    if info.get("system") == "darwin":
        return info.get("arch") in _SUPPORTED_MAC_ARCHES and _version_tuple(info.get("version", "")) >= (13,)
    if info.get("system") == "windows":
        return info.get("arch") in _SUPPORTED_WINDOWS_ARCHES and _version_tuple(info.get("version", "")) >= (10,)
    return False


def _platform_search_paths(info: dict[str, str]) -> list[Path]:
    """Return narrow, known vendor install locations; never scan a home tree."""
    if info.get("system") == "darwin":
        application_dirs = [Path("/Applications"), Path.home() / "Applications", Path.home() / "Downloads"]
        candidates: list[Path] = []
        for applications in application_dirs:
            candidates.extend(path / "Contents" / "MacOS" / "ODAFileConverter" for path in sorted(applications.glob("ODA*.app")))
            candidates.extend(path / "Contents" / "MacOS" / "ODAFileConverter" for path in sorted(applications.glob("ODAFileConverter*.app")))
        return candidates
    if info.get("system") == "windows":
        roots: list[Path] = []
        for variable in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
            value = str(os.environ.get(variable) or "").strip()
            if value and Path(value) not in roots:
                roots.append(Path(value))
        paths: list[Path] = []
        for root in roots:
            # ODA's installer names vary by release, but remain one level below
            # Program Files. This deliberately avoids recursive user scanning.
            for directory in sorted(root.glob("ODA*")):
                if directory.is_dir():
                    paths.extend((directory / "ODAFileConverter.exe", directory / "bin" / "ODAFileConverter.exe"))
            paths.append(root / "ODAFileConverter.exe")
        return paths
    return []


def _mach_o(path: Path) -> bool:
    try:
        magic = path.read_bytes()[:4]
    except OSError:
        return False
    return magic in {
        b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf",
        b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
    }


def _windows_version_metadata(path: Path) -> str | None:
    """Read a Windows file version without launching the executable."""
    if os.name == "nt":
        try:
            version = ctypes.windll.version
            size = version.GetFileVersionInfoSizeW(str(path), None)
            if not size:
                return None
            buffer = ctypes.create_string_buffer(size)
            if not version.GetFileVersionInfoW(str(path), 0, size, buffer):
                return None
            pointer = ctypes.c_void_p()
            length = ctypes.wintypes.UINT()
            if not version.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)):
                return None
            # VS_FIXEDFILEINFO has dwFileVersionMS/LS at byte offsets 8/12.
            raw = ctypes.string_at(pointer, length.value)
            if len(raw) >= 16:
                file_version_ms = int.from_bytes(raw[8:12], "little")
                file_version_ls = int.from_bytes(raw[12:16], "little")
                major = file_version_ms >> 16
                minor = file_version_ms & 0xFFFF
                build = file_version_ls >> 16
                revision = file_version_ls & 0xFFFF
                return f"{major}.{minor}.{build}.{revision}"
        except (AttributeError, OSError, ValueError):
            return None
    # Test fixtures and Wine exports often retain the string table in UTF-16.
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    for encoding in ("utf-16le", "utf-8", "latin-1"):
        try:
            text = raw.decode(encoding, errors="ignore")
        except (LookupError, UnicodeError):
            continue
        match = re.search(r"(?:FileVersion|ProductVersion)[^0-9]{0,24}(\d+(?:\.\d+){1,3})", text, re.I)
        if match:
            return match.group(1)
    return None


def _pe_executable(path: Path) -> bool:
    try:
        raw = path.read_bytes()
        if len(raw) < 64 or raw[:2] != b"MZ":
            return False
        offset = int.from_bytes(raw[0x3C:0x40], "little")
        return offset + 4 <= len(raw) and raw[offset:offset + 4] == b"PE\0\0"
    except (OSError, ValueError):
        return False


def _validate_converter(path: Path, info: dict[str, str] | None = None) -> tuple[bool, str | None, str]:
    info = info or _platform_info()
    try:
        if not path.is_file() or path.is_symlink() or path.name not in _REQUIRED_CONVERTER_NAMES:
            return False, None, "ODA 转换器路径不是普通文件。"
    except OSError:
        return False, None, "ODA 转换器路径无法读取。"
    if info.get("system") == "darwin":
        valid = _mach_o(path)
        return valid, None, "ODA 转换器不是 Mach-O 可执行文件。" if not valid else ""
    if info.get("system") == "windows":
        if not _pe_executable(path):
            return False, None, "ODA 转换器不是有效的 Windows PE 文件。"
        version = _windows_version_metadata(path)
        if not version:
            return False, None, "ODA 转换器缺少文件版本元数据。"
        return True, version, ""
    return False, None, "当前平台不支持 ODA 转换器。"


def _find_converter(root: Path | None = None, info: dict[str, str] | None = None) -> Path | None:
    info = info or _platform_info()
    candidates: list[Path] = []
    if root is not None and root.exists():
        # A user-selected package directory is permitted only at its known shape.
        if root.is_file():
            candidates.append(root)
        elif root.name.endswith(".app") and info.get("system") == "darwin":
            candidates.append(root / "Contents" / "MacOS" / "ODAFileConverter")
        elif root.is_dir() and info.get("system") == "windows":
            candidates.extend((root / "ODAFileConverter.exe", root / "bin" / "ODAFileConverter.exe"))
    candidates.extend(_platform_search_paths(info))
    for candidate in candidates:
        valid, _, _ = _validate_converter(candidate, info)
        if valid:
            return candidate.resolve()
    return None


def _configured_converter() -> Path | None:
    try:
        value = json.loads(_oda_config_path().read_text(encoding="utf-8")).get("path", "")
    except (OSError, ValueError, TypeError):
        return None
    path = Path(str(value)).expanduser()
    if not path.is_absolute() or path.is_symlink():
        return None
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return None
    # Never accept an ODA binary copied inside the first-party plugin package.
    try:
        if resolved == plugin_root().resolve() or plugin_root().resolve() in resolved.parents:
            return None
    except OSError:
        return None
    return resolved if resolved.name in _REQUIRED_CONVERTER_NAMES else None


def _converter_candidate(info: dict[str, str]) -> tuple[Path | None, str | None, bool]:
    configured = _configured_converter()
    if configured is not None:
        valid, version, detail = _validate_converter(configured, info)
        if valid:
            return configured, version, True
        return configured, None, False
    discovered = _find_converter(info=info)
    if discovered is not None:
        valid, version, _ = _validate_converter(discovered, info)
        if valid:
            return discovered, version, True
    return None, None, False


def _manifest(root: Path) -> dict[str, Any] | None:
    try:
        value = json.loads((root / "plugin.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) and value.get("id") == PLUGIN_ID else None


def _core_compatible(manifest: dict[str, Any]) -> bool:
    current = _version_tuple(APP_VERSION)
    minimum = manifest.get("min_core_version")
    maximum = manifest.get("max_core_version")
    compatible = manifest.get("compatible_core")
    if minimum and current < _version_tuple(str(minimum)):
        return False
    if maximum and current > _version_tuple(str(maximum)):
        return False
    if isinstance(compatible, dict):
        if compatible.get("min") and current < _version_tuple(str(compatible["min"])):
            return False
        if compatible.get("max") and current > _version_tuple(str(compatible["max"])):
            return False
    elif isinstance(compatible, (list, tuple, set)):
        if APP_VERSION not in {str(item) for item in compatible}:
            return False
    elif compatible and str(compatible) not in {APP_VERSION, "*"}:
        return False
    return True


def _marker_valid(root: Path, manifest: dict[str, Any]) -> bool:
    try:
        marker = json.loads((root / ".enabled").read_text(encoding="utf-8"))
        expected = hashlib.sha256(APP_VERSION.encode("utf-8")).hexdigest()
        return (
            isinstance(marker, dict)
            and marker.get("plugin_id") == PLUGIN_ID
            and marker.get("plugin_version") == manifest.get("version")
            and marker.get("core_version") == APP_VERSION
            and marker.get("core_version_sha256", marker.get("core_version_hash")) == expected
            and marker.get("wrapper_sha256", marker.get("wrapper_hash")) == hashlib.sha256((root / "plugin.json").read_bytes()).hexdigest()
        )
    except (OSError, ValueError, TypeError):
        return False


def probe_status() -> dict[str, Any]:
    with _plugin_lock():
        root = plugin_root()
        manifest = _manifest(root)
        info = _platform_info()
        supported = _platform_supported(info)
        converter, converter_version, converter_valid = _converter_candidate(info)
        oda_invalid = converter is not None and not converter_valid
        plugin_state = "missing"
        detail: list[str] = []
        if manifest is None:
            if (root / "plugin.json").exists():
                plugin_state = "error"
                detail.append("CAD 插件清单缺失或无法读取。")
        elif not _core_compatible(manifest):
            plugin_state = "error"
            detail.append("CAD 插件与当前 Translator 版本不兼容。")
        elif not _marker_valid(root, manifest):
            plugin_state = "error"
            detail.append("CAD 插件启用标记或核心版本校验失败。")
        else:
            plugin_state = "enabled"
        oda_state = "missing"
        if not supported:
            detail.append("当前平台仅支持 macOS 13+（Apple Silicon/Intel）或 Windows 10 x64。")
        elif oda_invalid:
            oda_state = "incompatible"
            detail.append("已配置的 ODA 转换器校验失败。")
        elif converter is not None:
            oda_state = "connected"
        enabled = plugin_state == "enabled" and oda_state == "connected" and supported
        message = "CAD 插件已就绪。" if enabled else "请安装 CAD Support 插件，并选择或下载官方 ODA 转换器。"
        return {
            "plugin_id": PLUGIN_ID,
            "version": str(manifest.get("version") if manifest else PLUGIN_VERSION),
            "installed": plugin_state == "enabled",
            "enabled": enabled,
            "plugin": plugin_state,
            "oda": oda_state,
            "platform": info,
            "platform_supported": supported,
            "root": str(root),
            "converter": str(converter) if converter else None,
            "converter_path": str(converter) if converter else None,
            "converter_found": oda_state == "connected",
            "converter_version": converter_version if oda_state == "connected" else None,
            "converter_arch": info.get("arch") if oda_state == "connected" else None,
            "official_download_url": OFFICIAL_DOWNLOAD_URL,
            "license_action": "user_provided_oda",
            "oda_bundled": False,
            "message": message,
            "detail": detail,
        }


def _validate_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("plugin.json 无法读取。") from exc
    if not isinstance(value, dict) or value.get("id") != PLUGIN_ID:
        raise ValueError("插件 ID 不匹配。")
    if (
        not isinstance(value.get("version"), str)
        or not re.match(r"^\d+\.\d+(?:\.\d+)?(?:[-+][0-9A-Za-z.-]+)?$", value["version"].strip())
    ):
        raise ValueError("plugin.json 缺少有效 version。")
    if value.get("adapter") != "builtin":
        raise ValueError("CAD Support 必须使用 first-party builtin 适配器。")
    unknown = set(value) - _MANIFEST_KEYS
    if unknown:
        raise ValueError("plugin.json 含未知字段：" + ", ".join(sorted(unknown)))
    checksums = value.get("sha256", {})
    if not isinstance(checksums, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in checksums.items()):
        raise ValueError("plugin.json 的 sha256 必须是文件名到摘要的映射。")
    return value


def _package_files(src: Path) -> list[Path]:
    files: list[Path] = []
    for path in src.rglob("*"):
        if path.is_symlink():
            raise ValueError("插件包不得包含符号链接。")
        if path.is_file():
            files.append(path)
    return files


def _validate_package_checksums(src: Path, manifest: dict[str, Any]) -> None:
    checksums = manifest.get("sha256", {})
    files = _package_files(src)
    for path in files:
        relative = path.relative_to(src)
        if path.name in _REQUIRED_CONVERTER_NAMES or "ODAFileConverter.app" in relative.parts:
            raise ValueError("插件包不得包含 ODA 转换器；ODA 必须由用户单独提供。")
    actual_names = {path.relative_to(src).as_posix() for path in files if path.name != "plugin.json"}
    declared_names = set(checksums)
    if actual_names != declared_names:
        missing = sorted(actual_names - declared_names)
        extra = sorted(declared_names - actual_names)
        raise ValueError(f"插件包必须为全部文件提供 sha256（缺少：{missing}，多余：{extra}）。")
    for relative, expected in checksums.items():
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError(f"插件包路径越界：{relative}")
        file_path = (src / relative_path).resolve()
        if src.resolve() not in file_path.parents or not file_path.is_file() or file_path.is_symlink():
            raise ValueError(f"校验文件不存在：{relative}")
        actual = hashlib.sha256(file_path.read_bytes()).hexdigest()
        if actual.lower() != expected.lower():
            raise ValueError(f"插件校验失败：{relative}")


def install_from_directory(source: str) -> dict[str, Any]:
    with _plugin_lock():
        src = Path(source).expanduser().resolve()
        dst = plugin_root().resolve()
        if not src.is_dir():
            raise ValueError("插件目录不存在。")
        if src == dst or dst in src.parents or src in dst.parents:
            raise ValueError("不能从当前安装目录或其父子目录安装插件。")
        manifest = _validate_manifest(src / "plugin.json")
        if not _core_compatible(manifest):
            raise ValueError("插件与当前 Translator 版本不兼容。")
        _validate_package_checksums(src, manifest)
        dst.parent.mkdir(parents=True, exist_ok=True)
        staging = dst.with_name(f"{dst.name}.installing-{uuid.uuid4().hex}")
        backup = dst.with_name(f"{dst.name}.previous-{uuid.uuid4().hex}")
        old_oda: bytes | None = None
        old_oda_exists = False
        try:
            old_oda_path = _oda_config_path(dst)
            if old_oda_path.is_file() and not old_oda_path.is_symlink():
                old_oda = old_oda_path.read_bytes()
                old_oda_exists = True
            shutil.copytree(src, staging, symlinks=False)
            marker = {
                "plugin_id": PLUGIN_ID,
                "plugin_version": manifest["version"],
                "core_version": APP_VERSION,
                "core_version_sha256": hashlib.sha256(APP_VERSION.encode("utf-8")).hexdigest(),
                "core_version_hash": hashlib.sha256(APP_VERSION.encode("utf-8")).hexdigest(),
                "wrapper_sha256": hashlib.sha256((staging / "plugin.json").read_bytes()).hexdigest(),
                "wrapper_hash": hashlib.sha256((staging / "plugin.json").read_bytes()).hexdigest(),
            }
            _atomic_write(staging / ".enabled", json.dumps(marker, ensure_ascii=False, indent=2))
            if old_oda_exists and old_oda is not None:
                _atomic_write(staging / "oda.json", old_oda.decode("utf-8"))
            if dst.exists():
                os.replace(dst, backup)
            os.replace(staging, dst)
            shutil.rmtree(backup, ignore_errors=True)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            if not dst.exists() and backup.exists():
                os.replace(backup, dst)
            raise
        return probe_status()


def install_builtin() -> dict[str, Any]:
    with _plugin_lock():
        root = plugin_root()
        root.parent.mkdir(parents=True, exist_ok=True)
        manifest = {"id": PLUGIN_ID, "version": PLUGIN_VERSION, "adapter": "builtin", "sha256": {}}
        temporary = root.with_name(f"{root.name}.installing-{uuid.uuid4().hex}")
        backup = root.with_name(f"{root.name}.previous-{uuid.uuid4().hex}")
        oda = _oda_config_path(root)
        old_oda = oda.read_bytes() if oda.is_file() and not oda.is_symlink() else None
        try:
            temporary.mkdir(parents=True)
            _atomic_write(temporary / "plugin.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            if old_oda is not None:
                _atomic_write(temporary / "oda.json", old_oda.decode("utf-8"))
            marker = {
                "plugin_id": PLUGIN_ID,
                "plugin_version": PLUGIN_VERSION,
                "core_version": APP_VERSION,
                "core_version_sha256": hashlib.sha256(APP_VERSION.encode("utf-8")).hexdigest(),
                "core_version_hash": hashlib.sha256(APP_VERSION.encode("utf-8")).hexdigest(),
                "wrapper_sha256": hashlib.sha256((temporary / "plugin.json").read_bytes()).hexdigest(),
                "wrapper_hash": hashlib.sha256((temporary / "plugin.json").read_bytes()).hexdigest(),
            }
            _atomic_write(temporary / ".enabled", json.dumps(marker, ensure_ascii=False, indent=2))
            if root.exists():
                os.replace(root, backup)
            os.replace(temporary, root)
            shutil.rmtree(backup, ignore_errors=True)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            if not root.exists() and backup.exists():
                os.replace(backup, root)
            raise
        return probe_status()


def connect_oda(path: str) -> dict[str, Any]:
    with _plugin_lock():
        candidate = Path(path).expanduser()
        if candidate.is_dir():
            candidate = _find_converter(candidate) or candidate
        try:
            candidate = candidate.resolve(strict=True)
        except OSError as exc:
            raise ValueError("未找到可用的 ODAFileConverter 可执行文件。") from exc
        info = _platform_info()
        valid, _, detail = _validate_converter(candidate, info)
        if not valid:
            raise ValueError(detail or "未找到可用的 ODAFileConverter 可执行文件。")
        # A converter supplied inside our package would turn a separately licensed
        # dependency into a bundled component, which is prohibited.
        try:
            if plugin_root().resolve() in candidate.parents:
                raise ValueError("ODA 转换器必须保留在插件目录之外。")
        except OSError:
            pass
        root = plugin_root()
        root.mkdir(parents=True, exist_ok=True)
        _atomic_write(_oda_config_path(root), json.dumps({"path": str(candidate)}, ensure_ascii=False, indent=2))
        return probe_status()


def uninstall() -> dict[str, Any]:
    """Remove first-party plugin files while preserving any external ODA binary."""
    with _plugin_lock():
        root = plugin_root()
        if root.exists():
            shutil.rmtree(root)
        return probe_status()
