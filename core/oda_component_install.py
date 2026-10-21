"""「图纸转换工具」（ODA File Converter）的一键安装。

ODA File Converter 是 Open Design Alliance 免费提供的软件，不是开源软件，不能随本程序分发。
一键安装由用户在插件页确认 ODA 的使用条款后，从 ODA 官网的公开下载地址直接下载到本机，
解到应用数据目录（``plugins/oda-converter/<随机>/``），不经过任何第三方。

官网的文件名不带版本号、内容随 ODA 发新版而变，没法像另外两个组件那样写死 SHA-256，
所以改为核对 ODA 的数字签名（macOS 的 Developer ID、Windows 的 Authenticode），再用装好
的程序把一张小图纸转成 DWG 再转回 DXF，确认能用才启用。

* macOS：挂载 DMG（只读、不弹窗），用 ditto 把 ``ODAFileConverter.app`` 原样拷出来。
* Windows：``msiexec /a`` 做「管理安装」，只把文件解到目标目录，不写注册表、不需要管理员。

本模块在 ``core`` 下，不能从 ``api`` 导入。
"""
from __future__ import annotations

import json
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from core import system_proxy
from core.app_paths import get_app_data_dir
from core.component_download import direct_client, open_client, replace_file

ProgressFn = Callable[[str, int, int], None]

DOWNLOAD_URL = "https://www.opendesign.com/guestfiles/get?filename={}"
# 官网下载页上各平台的安装包（文件名不带版本号，ODA 发新版时原地替换）。
PACKAGES = {
    "macos-arm64": "ODAFileConverter_QT6_macOsX_arm64_15.0dll.dmg",
    "macos-x64": "ODAFileConverter_QT6_macOsX_x64_15.0dll.dmg",
    "windows-x64": "ODAFileConverter_QT6_vc16_amd64dll.msi",
}
# 签名者名称里必须出现的字样（macOS 的 Authority、Windows 证书的 Subject）；macOS 另核对
# ODA 的 Apple 开发者团队编号（2026-10 实测 Developer ID Application: Open Design Alliance (4SVB9F72E4)）。
SIGNER = "Open Design Alliance"
MAC_TEAM_ID = "4SVB9F72E4"
_MAX_BYTES = 800 * 1024 * 1024
_CHUNK = 256 * 1024
_TIMEOUT = httpx.Timeout(connect=15.0, read=60.0, write=60.0, pool=15.0)
_MSI_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
_SAMPLE_TEXT = "PLAN XLT 2026"
_SAMPLE_DXF = (
    "0\nSECTION\n2\nENTITIES\n0\nTEXT\n8\n0\n10\n0.0\n20\n0.0\n30\n0.0\n40\n2.5\n"
    f"1\n{_SAMPLE_TEXT}\n0\nENDSEC\n0\nEOF\n"
)

DOWNLOAD_FAILED = "无法从 ODA 官网下载，请检查网络连接。"
NOT_A_PACKAGE = "ODA 官网返回的不是安装包，请稍后重试，或从官网下载安装。"
SIGNATURE_FAILED = "安装包的数字签名核对未通过，已停止安装。"
UNPACK_FAILED = "安装包无法解开，请从 ODA 官网下载安装。"
SELF_TEST_FAILED = "图纸转换工具已下载，但无法在本机运行。"


class ComponentInstallError(RuntimeError):
    """一键安装没有成功；消息是给用户看的一句话。"""


class _Cancelled(ComponentInstallError):
    pass


def platform_key() -> str:
    machine = platform.machine().lower()
    if sys.platform == "darwin":
        return {"arm64": "macos-arm64", "x86_64": "macos-x64"}.get(machine, "")
    if sys.platform == "win32" and machine in ("amd64", "x86_64"):
        return "windows-x64"
    return ""


def package_name() -> str:
    return PACKAGES.get(platform_key(), "")


def supported() -> bool:
    return bool(package_name())


def component_root() -> Path:
    return get_app_data_dir() / "plugins" / "oda-converter"


def _record_path() -> Path:
    return component_root() / "installed.json"


def _read_record() -> dict[str, Any]:
    try:
        data = json.loads(_record_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _safe_relative(value: str) -> Path | None:
    """installed.json 里记的相对路径：只许往下走，不许绝对路径和 ..。"""
    parts = [part for part in value.replace("\\", "/").split("/") if part]
    if not parts or any(part in (".", "..") for part in parts) or Path(value).is_absolute() or ":" in value:
        return None
    return Path(*parts)


def installed_executable() -> Path | None:
    """一键安装装好的转换程序；没装或文件已缺失时返回 None。"""
    record = _read_record()
    relative = _safe_relative(str(record.get("executable") or ""))
    if relative is None:
        return None
    executable = component_root() / relative
    try:
        if executable.is_symlink() or not executable.is_file():
            return None
    except OSError:
        return None
    return executable


def installed_info() -> dict[str, Any]:
    record = _read_record()
    executable = installed_executable()
    return {
        "installed": executable is not None,
        "path": str(executable or ""),
        "version": str(record.get("version") or "") if executable else "",
    }


def _write_record(data: dict[str, Any]) -> None:
    path = _record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _http_client() -> httpx.Client | None:
    """测试替换成假的下载源；平时由下载函数自己建、自己关。"""
    return None


def _run(args: list[str], *, timeout: float = 300, input_text: str | None = None,
         env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """跑系统自带的工具（hdiutil、codesign、msiexec、PowerShell）。测试里整体替换。"""
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False,
                          input=input_text, env=env, creationflags=_NO_WINDOW)


def _download(url: str, path: Path, progress: ProgressFn | None, cancel: threading.Event | None) -> None:
    client = _http_client()
    http = client or open_client()
    partial = path.with_name(path.name + ".part")

    def fetch(session: httpx.Client) -> None:
        with session.stream("GET", url) as response:
            response.raise_for_status()
            total = int(response.headers.get("content-length") or 0)
            if total > _MAX_BYTES:
                raise ComponentInstallError(NOT_A_PACKAGE)
            received = 0
            with partial.open("wb") as handle:
                for chunk in response.iter_bytes(_CHUNK):
                    if cancel is not None and cancel.is_set():
                        raise _Cancelled("安装已取消。")
                    handle.write(chunk)
                    received += len(chunk)
                    if received > _MAX_BYTES:
                        raise ComponentInstallError(NOT_A_PACKAGE)
                    if progress:
                        progress("download", received, max(total, received))

    try:
        try:
            fetch(http)
        except httpx.HTTPError as exc:
            # 代理软件已退出、系统代理设置还在：直连再试一次。
            if client is not None or not system_proxy.should_retry_direct(exc, url):
                raise
            logger.warning(f"[图纸转换工具] 经系统代理连不上，改为直连再试：{exc!r}")
            with direct_client() as session:
                fetch(session)
        replace_file(partial, path)
    except httpx.HTTPError as exc:
        logger.warning(f"[图纸转换工具] 下载失败：{url}: {exc!r}")
        raise ComponentInstallError(DOWNLOAD_FAILED) from exc
    except OSError as exc:
        logger.warning(f"[图纸转换工具] 下载写入失败：{exc!r}")
        raise ComponentInstallError(DOWNLOAD_FAILED) from exc
    finally:
        partial.unlink(missing_ok=True)
        if client is None:
            http.close()


def _looks_like_package(path: Path, kind: str) -> bool:
    """官网有时会返回网页（维护、限流）：按文件头尾认一下是不是安装包。"""
    try:
        with path.open("rb") as handle:
            if kind == "msi":
                return handle.read(8) == _MSI_MAGIC
            handle.seek(-512, os.SEEK_END)
            return handle.read(4) == b"koly"
    except OSError:
        return False


# ---- macOS ----

def _mac_signature_ok(app: Path) -> bool:
    verify = _run(["codesign", "--verify", "--deep", "--strict", str(app)])
    if verify.returncode != 0:
        logger.warning(f"[图纸转换工具] 签名校验未通过：{(verify.stderr or verify.stdout)[-800:]}")
        return False
    detail = _run(["codesign", "-dvv", str(app)])
    lines = (detail.stderr + detail.stdout).splitlines()
    authorities = [line.split("=", 1)[1] for line in lines if line.startswith("Authority=")]
    teams = [line.split("=", 1)[1].strip() for line in lines if line.startswith("TeamIdentifier=")]
    if not authorities or SIGNER not in authorities[0] or not authorities[0].startswith("Developer ID Application") \
            or teams != [MAC_TEAM_ID]:
        logger.warning(f"[图纸转换工具] 签名者不是 {SIGNER}：{authorities} {teams}")
        return False
    return True


def _mac_unpack(package: Path, staging: Path) -> Path:
    """挂载 DMG、核对签名、拷出 .app；返回 staging 里的可执行文件。"""
    mount = Path(tempfile.mkdtemp(prefix="oda-dmg-"))
    # DMG 带许可协议时 hdiutil 会等用户输入 Y：用户已在插件页同意了 ODA 的条款。
    attach = _run(["hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen", "-mountpoint", str(mount),
                   str(package)], input_text="Y\n")
    if attach.returncode != 0:
        logger.warning(f"[图纸转换工具] 挂载 DMG 失败：{(attach.stderr or attach.stdout)[-800:]}")
        shutil.rmtree(mount, ignore_errors=True)
        raise ComponentInstallError(UNPACK_FAILED)
    try:
        apps = sorted(path for path in mount.iterdir()
                      if path.suffix == ".app" and (path / "Contents" / "MacOS" / "ODAFileConverter").is_file())
        if not apps:
            logger.warning(f"[图纸转换工具] DMG 里没有 ODAFileConverter.app：{[p.name for p in mount.iterdir()]}")
            raise ComponentInstallError(UNPACK_FAILED)
        if not _mac_signature_ok(apps[0]):
            raise ComponentInstallError(SIGNATURE_FAILED)
        target = staging / "ODAFileConverter.app"
        copy = _run(["ditto", str(apps[0]), str(target)])
        if copy.returncode != 0:
            logger.warning(f"[图纸转换工具] 拷贝 .app 失败：{(copy.stderr or copy.stdout)[-800:]}")
            raise ComponentInstallError(UNPACK_FAILED)
    finally:
        _run(["hdiutil", "detach", str(mount), "-force"], timeout=60)
        shutil.rmtree(mount, ignore_errors=True)
    if not _mac_signature_ok(target):
        raise ComponentInstallError(SIGNATURE_FAILED)
    return target / "Contents" / "MacOS" / "ODAFileConverter"


def _mac_version(executable: Path) -> str:
    try:
        with (executable.parent.parent / "Info.plist").open("rb") as handle:
            info = plistlib.load(handle)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return ""
    return str(info.get("CFBundleShortVersionString") or info.get("CFBundleVersion") or "")


# ---- Windows ----

_AUTHENTICODE = (
    "$s = Get-AuthenticodeSignature -LiteralPath $env:ODA_PACKAGE; "
    "Write-Output $s.Status; Write-Output $s.SignerCertificate.Subject"
)


def _powershell(script: str, timeout: float, **variables: str) -> subprocess.CompletedProcess[str]:
    """用系统自带的 Windows PowerShell 5.1 跑一小段脚本，参数走环境变量（不拼进命令行）。

    不继承 PSModulePath：从 PowerShell 7 里启动时它指向 7 的模块目录，5.1 会加载不了
    Get-AuthenticodeSignature 所在的 Microsoft.PowerShell.Security。
    """
    root = Path(os.environ.get("SystemRoot") or r"C:\Windows")
    executable = root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    env = {key: value for key, value in os.environ.items() if key.upper() != "PSMODULEPATH"}
    env.update(variables)
    return _run([str(executable) if executable.is_file() else "powershell", "-NoProfile", "-NonInteractive",
                 "-Command", script], env=env, timeout=timeout)


def _windows_signature_ok(package: Path) -> bool:
    result = _powershell(_AUTHENTICODE, 120, ODA_PACKAGE=str(package))
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    # 证书的组织（O=）必须是 ODA；2026-10 实测 Subject 为全大写的
    # "CN=OPEN DESIGN ALLIANCE, O=OPEN DESIGN ALLIANCE, L=Scottsdale, S=Arizona, C=US"。
    organizations = {part.strip()[2:].strip().casefold() for part in (lines[1] if len(lines) > 1 else "").split(",")
                     if part.strip().upper().startswith("O=")}
    if result.returncode != 0 or len(lines) < 2 or lines[0] != "Valid" or SIGNER.casefold() not in organizations:
        logger.warning(f"[图纸转换工具] 安装包签名核对未通过：{lines} {result.stderr[-400:]}")
        return False
    return True


def _windows_unpack(package: Path, staging: Path) -> Path:
    if not _windows_signature_ok(package):
        raise ComponentInstallError(SIGNATURE_FAILED)
    target = staging / "files"
    target.mkdir(parents=True, exist_ok=True)
    msiexec = Path(os.environ.get("SystemRoot") or r"C:\Windows") / "System32" / "msiexec.exe"
    log = staging / "msiexec.log"
    result = _run([str(msiexec), "/a", str(package), "/qn", f"TARGETDIR={target}", "/L*", str(log)], timeout=600)
    if result.returncode not in (0, 3010):
        detail = ""
        try:
            detail = log.read_text(encoding="utf-16", errors="ignore")[-1200:]
        except OSError:
            pass
        logger.warning(f"[图纸转换工具] msiexec /a 失败（{result.returncode}）：{detail}")
        raise ComponentInstallError(UNPACK_FAILED)
    found = sorted(target.rglob("ODAFileConverter.exe"), key=lambda path: len(path.parts))
    if not found:
        logger.warning("[图纸转换工具] 解开的安装包里没有 ODAFileConverter.exe")
        raise ComponentInstallError(UNPACK_FAILED)
    return found[0]


def _windows_version(executable: Path) -> str:
    result = _powershell("(Get-Item -LiteralPath $env:ODA_EXE).VersionInfo.ProductVersion", 60,
                         ODA_EXE=str(executable))
    return result.stdout.strip().splitlines()[0] if result.returncode == 0 and result.stdout.strip() else ""


# ---- 自检与安装 ----

def _self_test(executable: Path, work: Path) -> None:
    """把一张只有一行字的小图纸转成 DWG 再转回 DXF，字还在才算能用。"""
    from core.cad_translation import CadPipelineError, SubprocessCadConverter

    work.mkdir(parents=True, exist_ok=True)
    sample = work / "self-test.dxf"
    sample.write_text(_SAMPLE_DXF, encoding="ascii")
    converter = SubprocessCadConverter(executable, timeout=180)
    try:
        dwg = converter.dxf_to_dwg(sample, work / "self-test.dwg")
        back = converter.dwg_to_dxf(dwg, work / "round-trip.dxf")
        ok = dwg.read_bytes()[:4] == b"AC10" and _SAMPLE_TEXT in back.read_text(encoding="utf-8", errors="ignore")
    except (CadPipelineError, OSError, subprocess.SubprocessError) as exc:
        logger.warning(f"[图纸转换工具] 一键安装后试转换失败：{exc!r}")
        raise ComponentInstallError(SELF_TEST_FAILED) from exc
    if not ok:
        logger.warning("[图纸转换工具] 一键安装后试转换的结果不对")
        raise ComponentInstallError(SELF_TEST_FAILED)


_INSTALL_LOCK = threading.Lock()


def install(*, progress: ProgressFn | None = None, cancel: threading.Event | None = None) -> dict[str, Any]:
    """下载、核对签名、解开、试转换，全部通过才换上新目录。"""
    name = package_name()
    if not name:
        raise ComponentInstallError("当前系统不支持一键安装。")
    if not _INSTALL_LOCK.acquire(blocking=False):
        raise ComponentInstallError("组件正在安装，请稍候。")
    root = component_root()
    staging = root / f".staging-{uuid.uuid4().hex[:8]}"
    try:
        staging.mkdir(parents=True, exist_ok=True)
        package = staging / name
        _download(DOWNLOAD_URL.format(name), package, progress, cancel)
        kind = "msi" if name.endswith(".msi") else "dmg"
        if not _looks_like_package(package, kind):
            raise ComponentInstallError(NOT_A_PACKAGE)
        if cancel is not None and cancel.is_set():
            raise _Cancelled("安装已取消。")
        if progress:
            progress("extract", 0, 1)
        unpacked = staging / "app"
        unpacked.mkdir()
        executable = _windows_unpack(package, unpacked) if kind == "msi" else _mac_unpack(package, unpacked)
        package.unlink(missing_ok=True)
        if progress:
            progress("verify", 0, 1)
        _self_test(executable, staging / "self-test")
        version = _windows_version(executable) if kind == "msi" else _mac_version(executable)
        final_dir = root / uuid.uuid4().hex[:8]
        old_dirs = [path for path in root.iterdir() if path.is_dir() and path != staging]
        unpacked.replace(final_dir)
        _write_record({
            "executable": (final_dir / executable.relative_to(unpacked)).relative_to(root).as_posix(),
            "version": version,
            "package": name,
            "platform": platform_key(),
        })
        for old in old_dirs:
            _discard(old)
        if progress:
            progress("done", 1, 1)
        return installed_info()
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        _INSTALL_LOCK.release()


def _discard(path: Path) -> None:
    trash = path.with_name(f".trash-{uuid.uuid4().hex[:8]}")
    try:
        path.replace(trash)
    except OSError:
        trash = path
    shutil.rmtree(trash, ignore_errors=True)


def uninstall() -> None:
    """删掉一键安装的转换工具（正在转换的进程结束后才删得掉的，下次安装或卸载时再删）。"""
    root = component_root()
    _record_path().unlink(missing_ok=True)
    if not root.is_dir():
        return
    for path in list(root.iterdir()):
        if path.is_dir():
            _discard(path)
