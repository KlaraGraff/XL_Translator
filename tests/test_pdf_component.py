"""「PDF 处理组件」：PyMuPDF 不进安装包，一键安装或连接用户自己的 pdf2docx 后按需加载。"""
from __future__ import annotations

import hashlib
import io
import json
import math
import subprocess
import sys
import threading
import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from core import component_download, pdf_component
from core import pdf_component_install as installer
from core.component_download import ComponentDownloadError, RemoteFile, download_files, extract_wheel

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def hermetic(monkeypatch, tmp_path):
    from core import pdf2docx_plugin

    monkeypatch.setattr(pdf2docx_plugin.shutil, "which", lambda name: None)
    monkeypatch.setenv("TRANSLATOR_APP_DATA_DIR", str(tmp_path / "app-data"))
    pdf_component.reset_for_tests()
    yield tmp_path
    pdf_component.reset_for_tests()


def _wheel(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _remote(name: str, data: bytes, *urls: str) -> RemoteFile:
    return RemoteFile(name, urls or (f"https://primary.test/{name}", f"https://mirror.test/{name}"),
                      len(data), hashlib.sha256(data).hexdigest())


def _client(routes: dict[str, bytes | int]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        value = routes.get(str(request.url), 404)
        if isinstance(value, int):
            return httpx.Response(value)
        return httpx.Response(200, content=value)

    return httpx.Client(transport=httpx.MockTransport(handler))


# ── download / verify / extract ───────────────────────────────────────────


def test_download_falls_back_to_a_mirror_and_checks_the_hash(tmp_path):
    good = b"real wheel bytes"
    item = _remote("a.whl", good)
    client = _client({
        "https://primary.test/a.whl": b"tampered bytes!!",  # same size, wrong content
        "https://mirror.test/a.whl": good,
    })
    seen: list[tuple[int, int]] = []
    [path] = download_files([item], tmp_path, progress=lambda d, t: seen.append((d, t)), client=client)
    assert path.read_bytes() == good
    assert seen[-1] == (len(good), len(good))


def test_download_fails_when_every_source_is_wrong(tmp_path):
    item = _remote("a.whl", b"expected")
    client = _client({"https://primary.test/a.whl": 503, "https://mirror.test/a.whl": b"something else"})
    with pytest.raises(ComponentDownloadError, match="下载失败"):
        download_files([item], tmp_path, client=client)
    assert not (tmp_path / "a.whl").exists()


def test_download_can_be_cancelled(tmp_path):
    cancel = threading.Event()
    cancel.set()
    item = _remote("a.whl", b"data")
    with pytest.raises(component_download.ComponentDownloadCancelled):
        download_files([item], tmp_path, cancel=cancel, client=_client({}))


def test_an_unusable_system_proxy_falls_back_to_a_direct_connection(monkeypatch):
    # Windows「Internet 选项」里填 SOCKS 代理时，Python 读出来是 socks4://，httpx 建客户端就报错。
    for name in ("NO_PROXY", "no_proxy", "ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "socks4://127.0.0.1:7891")
    with pytest.raises(ValueError):
        httpx.Client()
    with component_download.open_client() as client:
        assert client.trust_env is False


def test_download_retries_directly_when_the_system_proxy_is_gone(tmp_path, monkeypatch):
    good = b"real wheel bytes"
    item = _remote("a.whl", good)

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("proxy refused", request=request)

    monkeypatch.setattr(component_download, "open_client", lambda: httpx.Client(transport=httpx.MockTransport(refused)))
    monkeypatch.setattr(component_download, "direct_client", lambda: _client({"https://primary.test/a.whl": good}))
    monkeypatch.setattr(component_download.system_proxy, "proxy_for", lambda url: "http://127.0.0.1:7890")
    [path] = download_files([item], tmp_path)
    assert path.read_bytes() == good

    # 没走代理时连不上就是连不上，不另外直连。
    monkeypatch.setattr(component_download.system_proxy, "proxy_for", lambda url: "")
    (tmp_path / "a.whl").unlink()
    with pytest.raises(ComponentDownloadError, match="下载失败"):
        download_files([item], tmp_path)


def test_rename_waits_for_a_virus_scanner_on_windows(tmp_path, monkeypatch):
    import types

    calls: list[tuple[Path, Path]] = []

    def replace(source, target):
        calls.append((source, target))
        if len(calls) < 3:
            raise PermissionError(13, "The process cannot access the file because it is being used by another process")

    monkeypatch.setattr(component_download, "os", types.SimpleNamespace(name="nt", replace=replace))
    monkeypatch.setattr(component_download.time, "sleep", lambda seconds: None)
    component_download.replace_file(tmp_path / "a.part", tmp_path / "a")
    assert len(calls) == 3

    def always(source, target):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(component_download, "os", types.SimpleNamespace(name="nt", replace=always))
    with pytest.raises(PermissionError):
        component_download.replace_file(tmp_path / "a.part", tmp_path / "a")


def test_extract_rejects_paths_outside_the_target(tmp_path):
    wheel = tmp_path / "bad.whl"
    wheel.write_bytes(_wheel({"../evil.py": b"x"}))
    with pytest.raises(ComponentDownloadError):
        extract_wheel(wheel, tmp_path / "site")
    assert not (tmp_path / "evil.py").exists()


# ── one-click install ─────────────────────────────────────────────────────


def _fake_manifest(monkeypatch) -> dict[str, bytes]:
    wheels = {
        "pymupdf-9.9.9-cp310-abi3-test.whl": _wheel({"pymupdf/__init__.py": b"VersionBind = '9.9.9'\n"}),
        "pdf2docx-0.5.13-py3-none-any.whl": _wheel({"pdf2docx/__init__.py": b""}),
    }
    files = tuple(_remote(name, data) for name, data in wheels.items())
    monkeypatch.setattr(installer, "manifest_files", lambda: files)
    routes = {f"https://primary.test/{name}": data for name, data in wheels.items()}
    monkeypatch.setattr(installer, "_http_client", lambda: _client(routes))
    return wheels


def test_install_unpacks_checks_and_records_the_component(hermetic, monkeypatch):
    _fake_manifest(monkeypatch)
    probes: list[tuple[Path, bool]] = []

    def probe(site_dir, *, convert=False):
        probes.append((site_dir, convert))
        return True, "9.9.9", ""

    monkeypatch.setattr(pdf_component, "probe_in_subprocess", probe)
    phases: list[str] = []
    info = installer.install(progress=lambda phase, done, total: phases.append(phase))

    assert info["installed"] is True and info["pymupdf"] == "9.9.9"
    site_dir = Path(info["site_dir"])
    assert (site_dir / "pymupdf" / "__init__.py").is_file()
    assert probes and probes[0][1] is True, "the install must run one real PDF-to-Word conversion"
    assert phases[0] == "download" and phases[-1] == "done"
    assert not (installer.component_root() / ".downloads").exists()
    assert pdf_component.active_converter() == ("managed", site_dir)

    # 重装进新目录，旧目录删掉。
    second = installer.install()
    assert Path(second["site_dir"]) != site_dir
    assert not site_dir.exists()

    installer.uninstall()
    assert installer.installed_site_dir() is None
    assert pdf_component.active_converter() is None


def test_install_keeps_nothing_when_the_component_cannot_run(hermetic, monkeypatch):
    _fake_manifest(monkeypatch)
    monkeypatch.setattr(pdf_component, "probe_in_subprocess", lambda site_dir, convert=False: (False, "", "boom"))
    with pytest.raises(installer.ComponentInstallError, match="无法在本机运行"):
        installer.install()
    assert installer.installed_site_dir() is None
    leftovers = [path.name for path in installer.component_root().iterdir() if path.name != ".downloads"]
    assert leftovers == []


def test_install_copies_the_vc_runtime_next_to_pymupdf(hermetic, monkeypatch):
    # Windows：PyMuPDF 要的 msvcp140.dll 从 NumPy 的 wheel 里核对后拷到 pymupdf 目录。
    runtime = b"fake msvcp140"
    wheels = {
        "pymupdf-9.9.9-cp310-abi3-test.whl": _wheel({"pymupdf/__init__.py": b"VersionBind = '9.9.9'\n"}),
        "pdf2docx-0.5.13-py3-none-any.whl": _wheel({"pdf2docx/__init__.py": b""}),
        "numpy-test.whl": _wheel({"numpy.libs/msvcp140-abc.dll": runtime}),
    }
    files = tuple(_remote(name, data) for name, data in wheels.items())
    monkeypatch.setattr(installer, "manifest_files", lambda: files)
    monkeypatch.setattr(installer, "_http_client",
                        lambda: _client({f"https://primary.test/{name}": data for name, data in wheels.items()}))
    monkeypatch.setattr(pdf_component, "probe_in_subprocess", lambda site_dir, convert=False: (True, "9.9.9", ""))
    copies = (("numpy.libs/msvcp140-abc.dll", "pymupdf/msvcp140.dll", len(runtime), hashlib.sha256(runtime).hexdigest()),)
    monkeypatch.setitem(installer.RUNTIME_COPIES, installer.platform_key(), copies)
    info = installer.install()
    assert (Path(info["site_dir"]) / "pymupdf" / "msvcp140.dll").read_bytes() == runtime

    monkeypatch.setitem(installer.RUNTIME_COPIES, installer.platform_key(),
                        (("numpy.libs/msvcp140-abc.dll", "pymupdf/msvcp140.dll", len(runtime), "0" * 64),))
    with pytest.raises(installer.ComponentInstallError, match="不完整"):
        installer.install()


def test_windows_runtime_copy_comes_from_the_pinned_numpy_wheel():
    numpy = [item.name for item in installer.MANIFEST["windows-x64"] if item.name.startswith("numpy-")]
    assert numpy == ["numpy-2.2.6-cp311-cp311-win_amd64.whl"], "re-pin RUNTIME_COPIES when NumPy changes"
    [(source, target, size, sha256)] = installer.RUNTIME_COPIES["windows-x64"]
    assert source.startswith("numpy.libs/msvcp140-") and target == "pymupdf/msvcp140.dll"
    assert size > 0 and len(sha256) == 64


def test_install_reports_unsupported_platforms(hermetic, monkeypatch):
    monkeypatch.setattr(installer, "manifest_files", lambda: ())
    with pytest.raises(installer.ComponentInstallError, match="不支持一键安装"):
        installer.install()


def test_manifest_pins_every_platform_we_ship():
    for key in ("macos-arm64", "windows-x64"):
        names = [item.name for item in installer.MANIFEST[key]]
        assert any(name.startswith("pymupdf-") for name in names)
        assert any(name.startswith("pdf2docx-") for name in names)
        for item in installer.MANIFEST[key]:
            assert len(item.sha256) == 64 and item.size > 0
            assert item.urls[0].startswith("https://files.pythonhosted.org/packages/")
            assert len(item.urls) >= 2, "a mirror must back up the official source"


# ── which source is used ──────────────────────────────────────────────────


def _write_fake_pdf2docx(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\nif [ \"$1\" = \"--help\" ]; then echo 'pdf2docx convert' >&2; exit 0; fi\nexit 2\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


@pytest.mark.skipif(sys.platform == "win32", reason="fake executables are POSIX shell scripts")
def test_a_chosen_location_wins_over_the_one_click_install_until_install_again(hermetic, monkeypatch):
    from core import pdf2docx_plugin

    _fake_manifest(monkeypatch)
    monkeypatch.setattr(pdf_component, "probe_in_subprocess", lambda site_dir, convert=False: (True, "9.9.9", ""))
    info = installer.install()
    pdf2docx_plugin.prefer_managed()
    exe = hermetic / "venv" / "bin" / "pdf2docx"
    _write_fake_pdf2docx(exe)

    pdf2docx_plugin.save_pdf2docx_path(str(exe))
    assert pdf_component.active_converter() == ("manual", exe.resolve())
    pdf2docx_plugin.prefer_managed()
    assert pdf_component.active_converter() == ("managed", Path(info["site_dir"]))
    installer.uninstall()
    assert pdf_component.active_converter() == ("manual", exe.resolve())


# ── finding PyMuPDF in a user's own pdf2docx environment ─────────────────


def test_shebang_points_at_the_environment(tmp_path):
    exe = tmp_path / "bin" / "pdf2docx"
    exe.parent.mkdir()
    python = tmp_path / "bin" / "python3"
    python.write_text("", encoding="utf-8")
    exe.write_text(f"#!{python}\nimport sys\n", encoding="utf-8")
    assert pdf_component._shebang_interpreter(exe) == python
    exe.write_text(f"#!/bin/sh\n'''exec' \"{python}\" \"$0\" \"$@\"\n' '''\n", encoding="utf-8")
    assert pdf_component._shebang_interpreter(exe) == python


def test_windows_launcher_shebang(tmp_path):
    exe = tmp_path / "pdf2docx.exe"
    exe.write_bytes(b"MZ\x00launcher" * 50 + b'#!"C:\\Program Files\\Python311\\python.exe"\r\nPK\x03\x04zip')
    assert str(pdf_component._shebang_interpreter(exe)) == "C:\\Program Files\\Python311\\python.exe"


def test_site_packages_next_to_a_venv_executable(tmp_path):
    site = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    (site / "pymupdf").mkdir(parents=True)
    (site / "pymupdf" / "__init__.py").write_text("", encoding="utf-8")
    exe = tmp_path / "venv" / "bin" / "pdf2docx"
    _write_fake_pdf2docx(exe)
    assert pdf_component.locate_site_dir(exe) == site.resolve()


@pytest.mark.parametrize(
    ("tag", "ok"),
    [
        ("cp310-abi3-macosx_11_0_arm64", sys.platform == "darwin"),
        ("cp310-abi3-win_amd64", sys.platform == "win32"),
        ("cp310-abi3-manylinux_2_28_x86_64", sys.platform.startswith("linux")),
        ("cp399-abi3-manylinux_2_28_x86_64", False),  # newer than this Python
        ("cp312-cp312-manylinux_2_28_x86_64", False),  # another Python's binary
    ],
)
def test_wheel_tags_must_match_this_python_and_machine(tag, ok, monkeypatch):
    import platform

    if sys.platform.startswith("linux"):
        monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    assert pdf_component._tag_compatible(tag) is ok


def test_old_pymupdf_is_refused(tmp_path):
    info = tmp_path / "PyMuPDF-1.20.0.dist-info"
    info.mkdir()
    (info / "WHEEL").write_text("Tag: cp39-cp39-win_amd64\n", encoding="utf-8")
    version, problem = pdf_component.check_wheel(tmp_path)
    assert version == "1.20.0" and "过旧" in problem


def test_the_real_pymupdf_loads_through_the_probe_subprocess():
    """launch_sidecar.py --probe-pdf-component：冻结包在 CI 里跑的就是这一条。"""
    pymupdf = pytest.importorskip("pymupdf")
    site_dir = Path(pymupdf.__file__).resolve().parents[1]
    ok, version, detail = pdf_component.probe_in_subprocess(site_dir)
    assert ok, detail
    assert version


def test_the_finder_only_serves_pymupdf(tmp_path):
    (tmp_path / "pymupdf").mkdir()
    (tmp_path / "pymupdf" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "httpx").mkdir()
    (tmp_path / "httpx" / "__init__.py").write_text("", encoding="utf-8")
    finder = pdf_component._ComponentFinder(tmp_path)
    assert finder.find_spec("pymupdf") is not None
    assert finder.find_spec("httpx") is None


# ── without the component ─────────────────────────────────────────────────


def test_lazy_fitz_raises_a_clear_error_when_missing(hermetic, monkeypatch):
    monkeypatch.setattr(pdf_component, "_bundled_module", lambda: None)
    import core.pdf_vector  # noqa: F401 - importing must not need PyMuPDF

    from core.pdf_vector import common

    with pytest.raises(pdf_component.PdfComponentMissing, match="PDF 处理组件"):
        common.fitz.open()
    assert pdf_component.is_available(refresh=True) is False


def _sample_pdf(path: Path) -> None:
    fitz = pytest.importorskip("pymupdf")
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "An ordinary text page with plenty of words to translate here.", fontsize=11)
    page = doc.new_page()
    buffer = io.BytesIO()
    from PIL import Image

    Image.new("RGB", (60, 80), "white").save(buffer, "PNG")
    page.insert_image(page.rect, stream=buffer.getvalue())
    doc.new_page()
    page = doc.new_page()
    for index in range(1200):
        angle = index * 0.1
        page.draw_line((300, 400), (300 + 200 * math.cos(angle), 400 + 200 * math.sin(angle)))
    page.insert_text((72, 72), "Drawing label text with enough characters to count", fontsize=8)
    page = doc.new_page()
    page.insert_image(page.rect, stream=buffer.getvalue())
    page.insert_text((72, 72), "Invisible OCR layer text with enough characters", render_mode=3)
    doc.save(path)


def test_pdfium_classification_matches_pymupdf(tmp_path):
    from core.pdf_routes import _classify_with_pdfium, classify_document

    path = tmp_path / "sample.pdf"
    _sample_pdf(path)
    never = lambda width, height: False  # noqa: E731
    with_fitz = [page.kind for page in classify_document(path, is_oversized=never).pages]
    with_pdfium = [page.kind for page in _classify_with_pdfium(path, is_oversized=never)]
    assert with_fitz == ["text", "scan", "blank", "drawing", "scan"]
    assert with_pdfium == with_fitz


def test_classification_works_without_the_component(hermetic, monkeypatch, tmp_path):
    from core.pdf_routes import classify_document

    path = tmp_path / "sample.pdf"
    _sample_pdf(path)
    monkeypatch.setattr(pdf_component, "is_available", lambda refresh=False: False)
    kind = classify_document(path, is_oversized=lambda width, height: False)
    assert [page.kind for page in kind.pages] == ["text", "scan", "blank", "drawing", "scan"]


def test_the_sidecar_spec_keeps_pymupdf_out():
    spec = (ROOT / "packaging" / "sidecar" / "translator_sidecar.spec").read_text(encoding="utf-8")
    assert '"fitz", "pymupdf"' in spec
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert not any(line.strip().lower().startswith("pymupdf") for line in requirements.splitlines())


def test_bundled_sidecar_check_catches_a_leak(tmp_path):
    from scripts.build_tauri_sidecar import assert_pdf_component_not_bundled

    (tmp_path / "_internal").mkdir()
    assert_pdf_component_not_bundled(tmp_path)
    (tmp_path / "_internal" / "pymupdf").mkdir()
    with pytest.raises(RuntimeError, match="must not be bundled"):
        assert_pdf_component_not_bundled(tmp_path)


# ── plugins page API ──────────────────────────────────────────────────────


def _api_client():
    from api.app import create_app

    return TestClient(create_app())


def test_status_and_one_click_install_through_the_api(hermetic, monkeypatch):
    from api import pdf2docx_plugin as api_plugin

    _fake_manifest(monkeypatch)
    monkeypatch.setattr(pdf_component, "probe_in_subprocess", lambda site_dir, convert=False: (True, "9.9.9", ""))
    monkeypatch.setattr(pdf_component, "_bundled_module", lambda: None)
    client = _api_client()
    headers: dict[str, str] = {}
    status = client.get("/api/plugins/pdf2docx", headers=headers).json()
    assert status["state"] == "missing" and status["one_click_supported"] is True

    started = client.post("/api/plugins/pdf2docx/one-click-install", headers=headers).json()
    assert started["running"] is True
    for _ in range(200):
        progress = client.get("/api/plugins/pdf2docx/install-progress", headers=headers).json()
        if not progress["running"]:
            break
        threading.Event().wait(0.05)
    assert progress["phase"] == "done", progress
    status = client.get("/api/plugins/pdf2docx", headers=headers).json()
    assert status["state"] == "connected" and status["source"] == "managed"

    removed = client.post("/api/plugins/pdf2docx/uninstall", json={"target": "managed"}, headers=headers).json()
    assert removed["state"] == "missing"
    assert api_plugin.install_progress()["running"] is False


def test_route_capabilities_report_the_component(hermetic, monkeypatch):
    from api.routes.pdf_routes import route_capabilities

    monkeypatch.setattr(pdf_component, "_bundled_module", lambda: None)
    caps = route_capabilities()
    assert caps["word_component"] is False
    assert caps["inplace_component"] is False
    assert "PDF 处理组件" in caps["inplace_component_message"]


def test_launch_sidecar_routes_the_probe_flag(tmp_path):
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "launch_sidecar.py"), "--probe-pdf-component", str(tmp_path)],
        capture_output=True, text=True, timeout=120, check=False,
    )
    verdict = json.loads(result.stdout.strip().splitlines()[-1])
    # 空目录里没有 PyMuPDF——测试环境本身装着它时会从环境里加载成功，两种都说明入口接通了。
    assert "ok" in verdict
