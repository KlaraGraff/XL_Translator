"""中文、日文线条字识别（PP-OCR 模型 + ONNX Runtime）：一键安装与识别子进程。

真的模型和 ONNX Runtime 不在测试环境里（它们只在组件里）：子进程换成一个按同样协议
回话的假识别程序，安装换成假的下载源。真下载、真识别由 scripts/component_install_smoke.py
在 macOS / Windows 上跑。
"""
from __future__ import annotations

import hashlib
import io
import json
import sys
import textwrap
import threading
import zipfile
from pathlib import Path

import httpx
import pytest
from PIL import Image

from core import pdf_component, ppocr_component
from core import pdf_component_install
from core import ppocr_component_install as installer
from core.component_download import RemoteFile

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def hermetic(monkeypatch, tmp_path):
    monkeypatch.setenv("TRANSLATOR_APP_DATA_DIR", str(tmp_path / "app-data"))
    numpy_site = tmp_path / "pdf-component" / "site-packages"
    numpy_site.mkdir(parents=True)
    monkeypatch.setattr(ppocr_component, "numpy_site_dir", lambda: numpy_site)
    ppocr_component.shutdown_reader()
    yield tmp_path
    ppocr_component.shutdown_reader()


# 和 core.ppocr_component.worker_main 同一套协议的假识别程序。models 目录里放个
# behaviour 文件决定它怎么表现：fail_start（启动失败）、crash（读到请求就退出）、
# error（这一页出错）。
_FAKE_WORKER = textwrap.dedent('''
    import json, os, sys
    from pathlib import Path
    site, models, numpy_site = sys.argv[-3:]
    mode = (Path(models) / "behaviour").read_text().strip() if (Path(models) / "behaviour").exists() else ""
    def send(message):
        sys.stdout.write(json.dumps(message) + "\\n"); sys.stdout.flush()
    if mode == "fail_start":
        print("loader noise", file=sys.stderr); send({"ok": False, "error": "ImportError: no onnxruntime"}); sys.exit(1)
    print("native library banner")  # 不是 JSON 的行要跳过
    send({"ok": True, "onnxruntime": "9.9.9-test"})
    for line in sys.stdin.buffer:
        request = json.loads(line.decode("utf-8"))
        if mode == "crash":
            sys.exit(3)
        if mode == "error":
            send({"error": "ValueError: bad page"}); continue
        from PIL import Image
        with Image.open(request["image"]) as image:
            size = image.size
        for done in range(3):
            send({"progress": [done, 2]})
        rows = [{"source_text": "Plan de masse 2026", "pid": os.getpid(), "size": list(size),
                 "image": request["image"]}]
        Path(request["out"]).write_text(json.dumps(rows), encoding="utf-8")
        send({"done": len(rows)})
''')


@pytest.fixture
def fake_worker(hermetic, monkeypatch):
    script = hermetic / "fake_worker.py"
    script.write_text(_FAKE_WORKER, encoding="utf-8")
    calls: list[list[str]] = []

    def command(*args: str) -> list[str]:
        calls.append(list(args))
        return [sys.executable, str(script), *args]

    monkeypatch.setattr(ppocr_component, "_sidecar_command", command)
    return calls


def _install_fake_pack(behaviour: str = "") -> tuple[Path, Path]:
    base = installer.component_root() / f"{installer.MANIFEST_VERSION}-test"
    site_dir, models = base / "site-packages", base / "models"
    (site_dir / "onnxruntime").mkdir(parents=True)
    (site_dir / "onnxruntime" / "__init__.py").write_text("", encoding="utf-8")
    models.mkdir()
    for name in (installer.DET_MODEL, installer.REC_MODEL):
        (models / name).write_bytes(b"model")
    if behaviour:
        (models / "behaviour").write_text(behaviour, encoding="utf-8")
    installer._write_record({"version": installer.MANIFEST_VERSION, "dir": base.name})
    return site_dir, models


def _page() -> Image.Image:
    return Image.new("L", (640, 480), 255)


# ── 识别子进程 ────────────────────────────────────────────────────────────


def test_pages_are_read_by_one_worker_that_stays_open(fake_worker):
    site_dir, models = _install_fake_pack()
    reader = ppocr_component.make_reader()
    assert reader is not None
    steps: list[tuple[int, int]] = []
    first = reader(_page(), lambda done, total: steps.append((done, total)))
    second = reader(_page())
    assert first[0]["source_text"] == "Plan de masse 2026" and first[0]["size"] == [640, 480]
    assert steps == [(0, 2), (1, 2), (2, 2)]
    # 一份图纸的各页共用一个子进程，不每页重新加载模型
    assert first[0]["pid"] == second[0]["pid"] and len(fake_worker) == 1
    assert fake_worker[0][0] == ppocr_component.WORKER_FLAG
    assert fake_worker[0][1:3] == [str(site_dir), str(models)]
    # 页面图片放在临时目录里，读完就删
    assert not Path(first[0]["image"]).exists()


def test_a_worker_that_exits_fails_the_page_and_the_next_page_starts_a_new_one(fake_worker):
    _site_dir, models = _install_fake_pack("crash")
    reader = ppocr_component.make_reader()
    with pytest.raises(ppocr_component.OcrReaderError, match="意外退出"):
        reader(_page())
    (models / "behaviour").unlink()
    assert reader(_page())[0]["source_text"] == "Plan de masse 2026"
    assert len(fake_worker) == 2


def test_an_error_on_one_page_keeps_the_worker(fake_worker):
    _site_dir, models = _install_fake_pack("error")
    reader = ppocr_component.make_reader()
    with pytest.raises(ppocr_component.OcrReaderError, match="识别这一页时出错"):
        reader(_page())
    (models / "behaviour").unlink()   # 已开着的子进程照旧按「出错」回话
    with pytest.raises(ppocr_component.OcrReaderError):
        reader(_page())
    assert len(fake_worker) == 1


def test_a_worker_that_cannot_start_says_so(fake_worker):
    _install_fake_pack("fail_start")
    reader = ppocr_component.make_reader()
    with pytest.raises(ppocr_component.OcrReaderError, match="无法运行"):
        reader(_page())


def test_a_failing_progress_callback_does_not_leave_answers_for_the_next_page(fake_worker):
    _install_fake_pack()
    reader = ppocr_component.make_reader()

    def stop(done, total):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        reader(_page(), stop)
    rows = reader(_page())
    assert rows[0]["source_text"] == "Plan de masse 2026" and len(fake_worker) == 2


def test_an_idle_worker_is_closed(fake_worker, monkeypatch):
    monkeypatch.setattr(ppocr_component, "IDLE_SECONDS", 0.05)
    _install_fake_pack()
    reader = ppocr_component.make_reader()
    reader(_page())
    worker = ppocr_component._READER._worker
    assert worker is not None
    for _ in range(100):
        if ppocr_component._READER._worker is None and not worker.alive():
            break
        threading.Event().wait(0.05)
    assert ppocr_component._READER._worker is None and not worker.alive()
    reader(_page())
    assert len(fake_worker) == 2


def test_probe_reads_one_line(fake_worker):
    site_dir, models = _install_fake_pack()
    ok, version, detail = ppocr_component.probe_in_subprocess(site_dir, models, Path("numpy-site"))
    assert (ok, version, detail) == (True, "9.9.9-test", "")
    (models / "behaviour").write_text("fail_start", encoding="utf-8")
    ok, _version, detail = ppocr_component.probe_in_subprocess(site_dir, models, Path("numpy-site"))
    assert not ok and "无法运行" in detail


def test_no_reader_without_the_pack_or_the_pdf_component(hermetic, monkeypatch):
    assert ppocr_component.make_reader() is None
    _install_fake_pack()
    assert ppocr_component.make_reader() is not None
    monkeypatch.setattr(ppocr_component, "numpy_site_dir", lambda: None)
    assert ppocr_component.make_reader() is None


def test_numpy_comes_from_the_one_click_pdf_component(hermetic, monkeypatch):
    monkeypatch.undo()   # 用真的 numpy_site_dir
    monkeypatch.setenv("TRANSLATOR_APP_DATA_DIR", str(hermetic / "app-data"))
    managed = hermetic / "managed"
    monkeypatch.setattr(pdf_component, "managed_site_dir", lambda: managed)
    assert ppocr_component.numpy_site_dir() == managed


def test_the_worker_entry_is_wired_into_the_sidecar():
    launcher = (ROOT / "scripts" / "launch_sidecar.py").read_text(encoding="utf-8")
    assert '"--ppocr-worker"' in launcher and ppocr_component.WORKER_FLAG == "--ppocr-worker"


def test_the_sidecar_spec_keeps_the_recognition_libraries_out():
    spec = (ROOT / "packaging" / "sidecar" / "translator_sidecar.spec").read_text(encoding="utf-8")
    assert '"cv2", "onnxruntime"' in spec and '"numpy"' in spec


# ── 一键安装 ──────────────────────────────────────────────────────────────


def _wheel(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _remote(name: str, data: bytes) -> RemoteFile:
    return RemoteFile(name, (f"https://primary.test/{name}", f"https://mirror.test/{name}"),
                      len(data), hashlib.sha256(data).hexdigest())


def _fake_sources(monkeypatch, *, runtime: bytes | None = None, model_hash: str | None = None) -> None:
    det, rec = b"detector", b"recogniser"
    monkeypatch.setattr(installer, "MODEL_FILES", (
        ("rapidocr/models/det.onnx", installer.DET_MODEL, len(det), model_hash or hashlib.sha256(det).hexdigest()),
        ("rapidocr/models/rec.onnx", installer.REC_MODEL, len(rec), hashlib.sha256(rec).hexdigest()),
    ))
    blobs = {
        "onnxruntime-test.whl": _wheel({"onnxruntime/__init__.py": b"__version__ = '9.9.9'\n",
                                        "onnxruntime/capi/__init__.py": b""}),
        "rapidocr-test.whl": _wheel({"rapidocr/models/det.onnx": det, "rapidocr/models/rec.onnx": rec,
                                     "rapidocr/main.py": b"# not used",
                                     "rapidocr-3.10.0.dist-info/licenses/LICENSE": b"Apache",
                                     "rapidocr-3.10.0.dist-info/licenses/MODEL_LICENSES.md": b"models"}),
    }
    if runtime is not None:
        from tests.test_ocr_component import _conda

        blobs["vc14_runtime-test.conda"] = _conda({name: runtime + name.encode() for name in installer._WINDOWS_RUNTIME})
    files = tuple(_remote(name, data) for name, data in blobs.items())
    monkeypatch.setattr(installer, "manifest_files", lambda: files)
    routes = {f"https://primary.test/{name}": data for name, data in blobs.items()}

    def handler(request: httpx.Request) -> httpx.Response:
        value = routes.get(str(request.url))
        return httpx.Response(200, content=value) if value is not None else httpx.Response(404)

    monkeypatch.setattr(installer, "_http_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))


def test_install_unpacks_checks_and_records_the_pack(hermetic, monkeypatch):
    _fake_sources(monkeypatch)
    probes: list[tuple[Path, Path, Path]] = []

    def probe(site_dir, models, numpy_site):
        probes.append((site_dir, models, numpy_site))
        return True, "9.9.9", ""

    monkeypatch.setattr(ppocr_component, "probe_in_subprocess", probe)
    phases: list[str] = []
    info = installer.install(progress=lambda phase, done, total: phases.append(phase))
    assert info["installed"] is True and info["onnxruntime"] == "9.9.9" and info["models"] == installer.MODEL_NAME
    site_dir, models = installer.installed_dirs()
    assert (models / installer.DET_MODEL).read_bytes() == b"detector"
    assert (models / installer.REC_MODEL).read_bytes() == b"recogniser"
    assert (models / "LICENSE").is_file() and (models / "MODEL_LICENSES.md").is_file()
    assert not (models.parent / "rapidocr").exists(), "only the models and licenses are taken from the wheel"
    assert probes == [(site_dir, models, ppocr_component.numpy_site_dir())]
    assert phases[0] == "download" and phases[-1] == "done"
    assert not (installer.component_root() / ".downloads").exists()

    again = installer.install()
    assert again["path"] != info["path"] and not Path(info["path"]).exists()
    installer.uninstall()
    assert installer.installed_dirs() is None and ppocr_component.make_reader() is None


def test_windows_gets_the_vc_runtime_next_to_onnxruntime(hermetic, monkeypatch):
    _fake_sources(monkeypatch, runtime=b"runtime:")
    monkeypatch.setattr(ppocr_component, "probe_in_subprocess", lambda *args: (True, "9.9.9", ""))
    installer.install()
    site_dir, _models = installer.installed_dirs()
    for name in installer._WINDOWS_RUNTIME:
        assert (site_dir / "onnxruntime" / "capi" / name).read_bytes() == b"runtime:" + name.encode()


def test_a_model_that_does_not_match_leaves_nothing_behind(hermetic, monkeypatch):
    _fake_sources(monkeypatch, model_hash="0" * 64)
    monkeypatch.setattr(ppocr_component, "probe_in_subprocess", lambda *args: (True, "9.9.9", ""))
    with pytest.raises(installer.ComponentInstallError, match="核对未通过"):
        installer.install()
    assert installer.installed_dirs() is None
    assert not [path for path in installer.component_root().iterdir() if path.name != ".downloads"]


def test_a_pack_that_cannot_read_leaves_nothing_behind(hermetic, monkeypatch):
    _fake_sources(monkeypatch)
    monkeypatch.setattr(ppocr_component, "probe_in_subprocess", lambda *args: (False, "", "boom"))
    with pytest.raises(installer.ComponentInstallError, match="无法在本机运行"):
        installer.install()
    assert installer.installed_dirs() is None


def test_install_needs_the_pdf_component(hermetic, monkeypatch):
    _fake_sources(monkeypatch)
    monkeypatch.setattr(ppocr_component, "numpy_site_dir", lambda: None)
    with pytest.raises(installer.ComponentInstallError, match="PDF 处理组件"):
        installer.install()


def test_install_reports_unsupported_platforms(hermetic, monkeypatch):
    monkeypatch.setattr(installer, "manifest_files", lambda: ())
    with pytest.raises(installer.ComponentInstallError, match="不支持一键安装"):
        installer.install()


def test_installed_record_cannot_point_outside_the_component_folder(hermetic):
    installer._write_record({"version": installer.MANIFEST_VERSION, "dir": "../elsewhere"})
    assert installer.installed_dirs() is None


def test_manifest_pins_every_platform_we_ship():
    for key in ("macos-arm64", "windows-x64"):
        names = [item.name for item in installer.MANIFEST[key]]
        assert names[0].startswith(f"onnxruntime-{installer.ONNXRUNTIME_VERSION}-cp311-")
        assert names[1].startswith("rapidocr-")
        for item in installer.MANIFEST[key]:
            assert len(item.sha256) == 64 and item.size > 0
            assert len(item.urls) >= 2, "a mirror must back up the official source"
    assert installer.MANIFEST["windows-x64"][2].name.startswith("vc14_runtime-")
    # macOS 12 还要能用：ONNX Runtime 1.19 是最后一版 macosx_11_0 的 wheel
    assert "macosx_11_0" in installer.MANIFEST["macos-arm64"][0].name
    for _member, name, size, sha256 in installer.MODEL_FILES:
        assert name in (installer.DET_MODEL, installer.REC_MODEL) and size > 0 and len(sha256) == 64


def test_download_size_is_about_forty_megabytes(monkeypatch):
    for key in ("macos-arm64", "windows-x64"):
        monkeypatch.setattr(installer, "platform_key", lambda key=key: key)
        assert 30 * 2**20 < installer.download_size() < 50 * 2**20


def test_worker_rows_are_plain_json():
    np = pytest.importorskip("numpy")
    text = json.dumps([{"display_box": [np.float32(1.5), np.int64(2)], "score": np.float64(0.25)}],
                      default=ppocr_component._plain)
    assert json.loads(text) == [{"display_box": [1.5, 2], "score": 0.25}]


# ── 插件页 ────────────────────────────────────────────────────────────────


@pytest.mark.skipif(sys.platform == "win32", reason="the old fake tesseract is a shell script")
def test_one_click_install_updates_the_old_tesseract_through_the_api(hermetic, monkeypatch):
    from fastapi.testclient import TestClient

    from api import ocr_plugin
    from api.app import create_app
    from core import ocr_component_install as tesseract_installer
    from core import ocr_engine
    from tests.test_ocr_component import _fake_install_sources

    monkeypatch.setattr(ocr_engine, "_known_locations", lambda: [])
    monkeypatch.setattr(ocr_engine.shutil, "which", lambda name: None)
    # 以前一键安装的 Tesseract：只认西文
    _fake_install_sources(monkeypatch)
    tesseract_installer.install()
    # 这个测试模拟发布包的 Python 3.11 清单；开发机可能是别的 Python 版本。
    _fake_sources(monkeypatch)
    client = TestClient(create_app())
    status = client.get("/api/plugins/ocr").json()
    assert status["engine"] == "tesseract" and status["source"] == "managed"
    assert status["one_click_supported"] is True and status["needs_pdf_component"] is False
    assert status["download_bytes"] == installer.download_size()

    monkeypatch.setattr(ppocr_component, "probe_in_subprocess", lambda *args: (True, "9.9.9", ""))
    started = client.post("/api/plugins/ocr/one-click-install").json()
    assert started["running"] is True
    for _ in range(200):
        progress = client.get("/api/plugins/ocr/install-progress").json()
        if not progress["running"]:
            break
        threading.Event().wait(0.05)
    assert progress["phase"] == "done", progress
    status = client.get("/api/plugins/ocr").json()
    assert status["state"] == "connected" and status["source"] == "managed" and status["engine"] == "ppocr"
    # 换上识别模型后，以前一键安装的 Tesseract 删掉
    assert tesseract_installer.installed_executable() is None

    removed = client.post("/api/plugins/ocr/uninstall", json={"target": "managed"}).json()
    assert removed["state"] == "missing" and removed["managed_installed"] is False
    assert installer.installed_dirs() is None and ocr_plugin.install_progress()["running"] is False


def test_one_click_install_automatically_installs_the_pdf_component_first(hermetic, monkeypatch):
    from fastapi.testclient import TestClient

    from api import ocr_plugin
    from api.app import create_app
    from core import pdf_component_install as pdf_installer

    monkeypatch.setattr(ppocr_component, "numpy_site_dir", lambda: None)
    # 模拟两份发布清单，避免测试依赖当前开发机的 Python 版本。
    monkeypatch.setattr(installer, "manifest_files", lambda: (object(),))
    monkeypatch.setattr(installer, "download_size", lambda: 10)
    monkeypatch.setattr(pdf_installer, "manifest_files", lambda: (object(),))
    monkeypatch.setattr(pdf_installer, "download_size", lambda: 20)
    ready = False
    calls: list[str] = []

    def numpy_site():
        return hermetic / "pdf-site" if ready else None

    monkeypatch.setattr(ppocr_component, "numpy_site_dir", numpy_site)

    def fake_pdf_install(*, progress=None, cancel=None):
        nonlocal ready
        calls.append("pdf")
        if progress:
            progress("download", 20, 20)
            progress("extract", 1, 1)
            progress("verify", 1, 1)
        (hermetic / "pdf-site").mkdir()
        ready = True
        return {"installed": True}

    def fake_ocr_install(*, progress=None, cancel=None):
        calls.append("ocr")
        if progress:
            progress("download", 10, 10)
            progress("extract", 1, 1)
            progress("verify", 1, 1)
        return {"installed": True}

    monkeypatch.setattr(pdf_installer, "install", fake_pdf_install)
    monkeypatch.setattr(installer, "install", fake_ocr_install)
    client = TestClient(create_app())
    assert client.get("/api/plugins/ocr").json()["needs_pdf_component"] is True
    status = client.get("/api/plugins/ocr").json()
    assert status["one_click_supported"] is True
    response = client.post("/api/plugins/ocr/one-click-install")
    assert response.status_code == 200 and response.json()["total"] == 30
    for _ in range(100):
        progress = client.get("/api/plugins/ocr/install-progress").json()
        if not progress["running"]:
            break
        threading.Event().wait(0.01)
    assert progress["phase"] == "done" and calls == ["pdf", "ocr"]
    assert ocr_plugin.install_progress()["running"] is False


def test_ocr_status_exposes_runtime_and_dependency_diagnostics(hermetic, monkeypatch):
    from api import ocr_plugin

    monkeypatch.setattr(installer, "manifest_files", lambda: ())
    monkeypatch.setattr(pdf_component_install, "manifest_files", lambda: ())
    monkeypatch.setattr(ppocr_component, "numpy_site_dir", lambda: None)
    status = ocr_plugin.probe_status()
    assert status["one_click_supported"] is False
    assert status["dependency_one_click_supported"] is False
    assert status["runtime_python"] and status["runtime_platform"] and status["runtime_architecture"]
    assert "当前运行环境" in status["one_click_reason"]
