"""「图纸转换工具」一键安装：从 ODA 官网下载、核对签名、解开、试转换。

真的 hdiutil / codesign / msiexec / PowerShell 只在对应系统上有，这里换成假的命令，
核对每一步的判断和失败时的处理；真下载、真解包由 scripts/oda_install_smoke.py 在
macOS、Windows 的 CI 机器上跑（PR checks 手动触发）。
"""
from __future__ import annotations

import json
import plistlib
import shutil
import subprocess
import threading
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from api import cad_plugin as cad
from core import oda_component_install as oda

MACH_O = b"\xcf\xfa\xed\xfe" + b"\0" * 128
DMG = b"\0" * 4096 + b"koly" + b"\0" * 508
MSI = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 4096


@pytest.fixture
def mac(monkeypatch, tmp_path):
    monkeypatch.setenv("TRANSLATOR_APP_DATA_DIR", str(tmp_path / "app-data"))
    monkeypatch.setattr(cad, "_platform_search_paths", lambda info: [])
    monkeypatch.setattr(cad, "_run_discovery_command", lambda args, **kwargs: [])
    monkeypatch.setattr(cad.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(cad.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(cad.platform, "mac_ver", lambda: ("14.5", ()))
    monkeypatch.setattr(cad.platform, "release", lambda: "23.0")
    monkeypatch.setattr(oda, "platform_key", lambda: "macos-arm64")
    monkeypatch.setattr(oda, "_self_test", lambda executable, work: None)
    return tmp_path


def _client(body: bytes, status: int = 200, seen: list[str] | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(str(request.url))
        return httpx.Response(status, content=body, headers={"content-length": str(len(body))})

    return httpx.Client(transport=httpx.MockTransport(handler))


class FakeMac:
    """假的 hdiutil / codesign / ditto：DMG 里放一个 ODAFileConverter.app。"""

    def __init__(self, authority: str = "Developer ID Application: Open Design Alliance (4SVB9F72E4)",
                 verify_code: int = 0, app_name: str | None = "ODAFileConverter.app", team: str = "4SVB9F72E4") -> None:
        self.authority = authority
        self.team = team
        self.verify_code = verify_code
        self.app_name = app_name
        self.calls: list[list[str]] = []
        self.detached = False

    def __call__(self, args, *, timeout=300, input_text=None, env=None):
        self.calls.append(list(args))
        tool = args[0]
        if tool == "hdiutil" and args[1] == "attach":
            mount = Path(args[args.index("-mountpoint") + 1])
            assert input_text == "Y\n"
            (mount / "Readme.txt").write_text("ODA", encoding="utf-8")
            if self.app_name is None:
                return subprocess.CompletedProcess(args, 0, "", "")
            app = mount / self.app_name
            (app / "Contents" / "MacOS").mkdir(parents=True)
            (app / "Contents" / "MacOS" / "ODAFileConverter").write_bytes(MACH_O)
            with (app / "Contents" / "Info.plist").open("wb") as handle:
                plistlib.dump({"CFBundleShortVersionString": "26.9.0"}, handle)
            return subprocess.CompletedProcess(args, 0, "", "")
        if tool == "hdiutil" and args[1] == "detach":
            self.detached = True
            return subprocess.CompletedProcess(args, 0, "", "")
        if tool == "codesign" and args[1] == "--verify":
            return subprocess.CompletedProcess(args, self.verify_code, "", "invalid signature" if self.verify_code else "")
        if tool == "codesign":
            return subprocess.CompletedProcess(args, 0, "", f"Executable=x\nAuthority={self.authority}\nAuthority=Developer ID Certification Authority\nAuthority=Apple Root CA\nTeamIdentifier={self.team}\n")
        if tool == "ditto":
            shutil.copytree(args[1], args[2])
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"unexpected command {args}")


def _install(monkeypatch, body: bytes = DMG, runner=None, status: int = 200, seen=None):
    monkeypatch.setattr(oda, "_http_client", lambda: _client(body, status, seen))
    monkeypatch.setattr(oda, "_run", runner or FakeMac())
    return oda.install()


def test_platform_packages_come_from_the_official_site():
    assert set(oda.PACKAGES) == {"macos-arm64", "macos-x64", "windows-x64"}
    assert oda.DOWNLOAD_URL.startswith("https://www.opendesign.com/guestfiles/get?filename=")
    assert oda.PACKAGES["windows-x64"].endswith(".msi")
    assert all(name.endswith(".dmg") for key, name in oda.PACKAGES.items() if key.startswith("macos"))


def test_mac_install_end_to_end(mac, monkeypatch):
    seen: list[str] = []
    fake = FakeMac()
    info = _install(monkeypatch, runner=fake, seen=seen)
    assert seen == [oda.DOWNLOAD_URL.format(oda.PACKAGES["macos-arm64"])]
    assert info["installed"] is True and info["version"] == "26.9.0"
    executable = oda.installed_executable()
    assert executable is not None and executable.name == "ODAFileConverter"
    assert executable.parent.parent.parent.name == "ODAFileConverter.app"
    assert fake.detached
    # 原样拷进来的 .app 拷完再核一次签名。
    assert sum(1 for call in fake.calls if call[:2] == ["codesign", "--verify"]) == 2
    root = oda.component_root()
    assert sorted(path.name for path in root.iterdir() if path.name.startswith(".")) == []

    status = cad.probe_status()
    assert status["oda"] == "connected" and status["source"] == "managed"
    assert status["converter"] == str(executable.resolve())
    assert status["managed_installed"] is True and status["one_click_supported"] is True
    # 装好的那一份不写进「连接的位置」，卸载后不会留下指向它的旧路径。
    assert not cad._oda_config_path().exists()
    assert status["oda_bundled"] is False and status["license_action"] == "user_provided_oda"


def test_reinstall_replaces_the_old_copy(mac, monkeypatch):
    _install(monkeypatch)
    first = oda.installed_executable()
    _install(monkeypatch)
    second = oda.installed_executable()
    assert first != second and not first.exists() and second.exists()
    assert len([path for path in oda.component_root().iterdir() if path.is_dir()]) == 1


@pytest.mark.parametrize("fake, message", [
    (FakeMac(verify_code=1), oda.SIGNATURE_FAILED),
    (FakeMac(authority="Developer ID Application: Someone Else (ZZZ)"), oda.SIGNATURE_FAILED),
    (FakeMac(authority="Apple Development: Open Design Alliance (ABC)"), oda.SIGNATURE_FAILED),
    (FakeMac(team="ZZZZZZZZZZ"), oda.SIGNATURE_FAILED),
    (FakeMac(app_name=None), oda.UNPACK_FAILED),
])
def test_mac_rejects_wrong_signatures_and_contents(mac, monkeypatch, fake, message):
    with pytest.raises(oda.ComponentInstallError, match=message):
        _install(monkeypatch, runner=fake)
    assert fake.detached
    assert oda.installed_executable() is None
    assert [path.name for path in oda.component_root().iterdir()] == []


def test_web_page_instead_of_installer_is_refused(mac, monkeypatch):
    with pytest.raises(oda.ComponentInstallError, match=oda.NOT_A_PACKAGE):
        _install(monkeypatch, body=b"<html>maintenance</html>")
    assert oda.installed_executable() is None


def test_download_failure_message(mac, monkeypatch):
    with pytest.raises(oda.ComponentInstallError, match=oda.DOWNLOAD_FAILED):
        _install(monkeypatch, status=503)


def test_cancel_during_download(mac, monkeypatch):
    cancel = threading.Event()
    cancel.set()
    monkeypatch.setattr(oda, "_http_client", lambda: _client(DMG))
    monkeypatch.setattr(oda, "_run", FakeMac())
    with pytest.raises(oda.ComponentInstallError, match="安装已取消"):
        oda.install(cancel=cancel)
    assert oda.installed_executable() is None


def test_failed_self_test_installs_nothing(mac, monkeypatch):
    def broken(executable, work):
        raise oda.ComponentInstallError(oda.SELF_TEST_FAILED)

    monkeypatch.setattr(oda, "_self_test", broken)
    with pytest.raises(oda.ComponentInstallError, match=oda.SELF_TEST_FAILED):
        _install(monkeypatch)
    assert oda.installed_executable() is None
    assert [path.name for path in oda.component_root().iterdir()] == []


def test_unsupported_platform(monkeypatch, tmp_path):
    monkeypatch.setenv("TRANSLATOR_APP_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(oda, "platform_key", lambda: "")
    assert oda.supported() is False
    with pytest.raises(oda.ComponentInstallError):
        oda.install()


@pytest.mark.parametrize("value", ["../outside/ODAFileConverter", "/abs/ODAFileConverter", "C:/x/ODAFileConverter.exe", ""])
def test_record_cannot_point_outside(mac, value):
    root = oda.component_root()
    root.mkdir(parents=True)
    (root / "installed.json").write_text(json.dumps({"executable": value}), encoding="utf-8")
    assert oda.installed_executable() is None


# ── Windows ──────────────────────────────────────────────────────────────


class FakeWindows:
    def __init__(self, status: str = "Valid",
                 subject: str = "CN=OPEN DESIGN ALLIANCE, O=OPEN DESIGN ALLIANCE, L=Scottsdale, S=Arizona, C=US",
                 msi_code: int = 0) -> None:
        self.status = status
        self.subject = subject
        self.msi_code = msi_code
        self.calls: list[list[str]] = []

    def __call__(self, args, *, timeout=300, input_text=None, env=None):
        self.calls.append(list(args))
        powershell = args[0] == "powershell" or args[0].lower().endswith("powershell.exe")
        if powershell and "Get-AuthenticodeSignature" in args[-1]:
            assert env and env["ODA_PACKAGE"].endswith(".msi")
            assert not any(key.upper() == "PSMODULEPATH" for key in env)
            return subprocess.CompletedProcess(args, 0, f"{self.status}\r\n{self.subject}\r\n", "")
        if powershell:
            return subprocess.CompletedProcess(args, 0, "26.9.0.0\r\n", "")
        if args[0].endswith("msiexec.exe"):
            assert args[1] == "/a" and "/qn" in args
            target = Path(next(arg for arg in args if arg.startswith("TARGETDIR="))[len("TARGETDIR="):])
            folder = target / "ODA" / "ODAFileConverter 26.9.0"
            folder.mkdir(parents=True)
            (folder / "ODAFileConverter.exe").write_bytes(b"MZ")
            (folder / "TD_Db.dll").write_bytes(b"MZ")
            return subprocess.CompletedProcess(args, self.msi_code, "", "")
        raise AssertionError(f"unexpected command {args}")


def test_windows_unpack_checks_signature_then_extracts(monkeypatch, tmp_path):
    monkeypatch.setenv("PSModulePath", r"C:\Program Files\PowerShell\7\Modules")
    fake = FakeWindows()
    monkeypatch.setattr(oda, "_run", fake)
    package = tmp_path / "ODAFileConverter.msi"
    package.write_bytes(MSI)
    executable = oda._windows_unpack(package, tmp_path / "app")
    assert executable.name == "ODAFileConverter.exe" and (executable.parent / "TD_Db.dll").exists()
    assert "Get-AuthenticodeSignature" in fake.calls[0][-1]  # 签名先于解包
    assert oda._windows_version(executable) == "26.9.0.0"


@pytest.mark.parametrize("fake, message", [
    (FakeWindows(status="NotSigned"), oda.SIGNATURE_FAILED),
    (FakeWindows(subject="CN=Someone Else"), oda.SIGNATURE_FAILED),
    (FakeWindows(subject="CN=Open Design Alliance, O=Someone Else, C=US"), oda.SIGNATURE_FAILED),
    (FakeWindows(msi_code=1603), oda.UNPACK_FAILED),
])
def test_windows_rejects_bad_packages(monkeypatch, tmp_path, fake, message):
    monkeypatch.setattr(oda, "_run", fake)
    package = tmp_path / "ODAFileConverter.msi"
    package.write_bytes(MSI)
    with pytest.raises(oda.ComponentInstallError, match=message):
        oda._windows_unpack(package, tmp_path / "app")
    if message == oda.SIGNATURE_FAILED:
        assert not any(call[0].endswith("msiexec.exe") for call in fake.calls)


def test_package_magic():
    assert oda._MSI_MAGIC == MSI[:8]


# ── 试转换 ───────────────────────────────────────────────────────────────


def test_self_test_round_trips_a_drawing(monkeypatch, tmp_path):
    from core import cad_translation

    class Converter:
        def __init__(self, executable, timeout=600):
            self.executable = executable

        def dxf_to_dwg(self, source, destination):
            assert oda._SAMPLE_TEXT in Path(source).read_text(encoding="ascii")
            Path(destination).write_bytes(b"AC1032" + b"\0" * 64)
            return Path(destination)

        def dwg_to_dxf(self, source, destination):
            Path(destination).write_text(f"0\nTEXT\n1\n{oda._SAMPLE_TEXT}\n0\nEOF\n", encoding="utf-8")
            return Path(destination)

    monkeypatch.setattr(cad_translation, "SubprocessCadConverter", Converter)
    oda._self_test(tmp_path / "ODAFileConverter", tmp_path / "work")

    class Lossy(Converter):
        def dwg_to_dxf(self, source, destination):
            Path(destination).write_text("0\nEOF\n", encoding="utf-8")
            return Path(destination)

    monkeypatch.setattr(cad_translation, "SubprocessCadConverter", Lossy)
    with pytest.raises(oda.ComponentInstallError, match=oda.SELF_TEST_FAILED):
        oda._self_test(tmp_path / "ODAFileConverter", tmp_path / "work2")

    class Crashing(Converter):
        def dxf_to_dwg(self, source, destination):
            raise cad_translation.CadPipelineError("图纸转换工具未能启动")

    monkeypatch.setattr(cad_translation, "SubprocessCadConverter", Crashing)
    with pytest.raises(oda.ComponentInstallError, match=oda.SELF_TEST_FAILED):
        oda._self_test(tmp_path / "ODAFileConverter", tmp_path / "work3")


# ── 优先级与插件页 API ───────────────────────────────────────────────────


def test_connected_location_beats_managed_and_managed_beats_discovery(mac, monkeypatch):
    _install(monkeypatch)
    managed = oda.installed_executable().resolve()
    own = mac / "own" / "ODAFileConverter.app" / "Contents" / "MacOS" / "ODAFileConverter"
    own.parent.mkdir(parents=True)
    own.write_bytes(MACH_O)
    found = mac / "found" / "ODAFileConverter.app" / "Contents" / "MacOS" / "ODAFileConverter"
    found.parent.mkdir(parents=True)
    found.write_bytes(MACH_O)
    monkeypatch.setattr(cad, "_platform_search_paths", lambda info: [found])

    assert cad.probe_status()["converter"] == str(managed)
    status = cad.connect_oda(str(own.parent.parent.parent))
    assert status["converter"] == str(own.resolve()) and status["source"] == "external"
    # 卸载一键安装的那份不碰用户自己装的。
    status = cad.remove_managed()
    assert status["converter"] == str(own.resolve()) and own.exists()
    assert oda.installed_executable() is None


def test_one_click_install_through_the_api(mac, monkeypatch):
    from api.app import create_app

    monkeypatch.setattr(oda, "_http_client", lambda: _client(DMG))
    monkeypatch.setattr(oda, "_run", FakeMac())
    own = mac / "own" / "ODAFileConverter.app"
    (own / "Contents" / "MacOS").mkdir(parents=True)
    (own / "Contents" / "MacOS" / "ODAFileConverter").write_bytes(MACH_O)
    client = TestClient(create_app())
    status = client.post("/api/plugins/cad/oda", json={"path": str(own)}).json()
    assert status["source"] == "external" and status["one_click_supported"] is True

    started = client.post("/api/plugins/cad/one-click-install").json()
    assert started["running"] is True
    for _ in range(200):
        progress = client.get("/api/plugins/cad/install-progress").json()
        if not progress["running"]:
            break
        threading.Event().wait(0.05)
    assert progress["phase"] == "done", progress
    status = client.get("/api/plugins/cad").json()
    # 一键装好就用装好的：之前连接的位置不再优先。
    assert status["oda"] == "connected" and status["source"] == "managed"
    assert status["converter_version"]

    removed = client.post("/api/plugins/cad/remove-converter").json()
    assert removed["managed_installed"] is False
    assert (own / "Contents" / "MacOS" / "ODAFileConverter").exists()


def test_api_refuses_one_click_where_unsupported(monkeypatch, tmp_path):
    from api.app import create_app

    monkeypatch.setenv("TRANSLATOR_APP_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cad, "_platform_search_paths", lambda info: [])
    monkeypatch.setattr(cad, "_run_discovery_command", lambda args, **kwargs: [])
    monkeypatch.setattr(cad.platform, "system", lambda: "Linux")
    client = TestClient(create_app())
    assert client.get("/api/plugins/cad").json()["one_click_supported"] is False
    response = client.post("/api/plugins/cad/one-click-install")
    assert response.status_code == 409
