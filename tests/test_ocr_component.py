"""「文字识别组件」一键安装：conda-forge 的程序和库 + tessdata_fast 语言数据。"""
from __future__ import annotations

import hashlib
import io
import os
import re
import subprocess
import sys
import tarfile
import threading
import zipfile
from pathlib import Path

import httpx
import pytest
import zstandard
from fastapi.testclient import TestClient

from core import component_download, ocr_engine
from core import ocr_component_install as installer
from core.component_download import ComponentDownloadError, RemoteFile, extract_conda, stream_conda_files
from core.ocr_component_install import CondaPackage, OcrManifest, TesseractStream

posix_only = pytest.mark.skipif(os.name == "nt", reason="fake tesseract is a shell script")


@pytest.fixture
def hermetic(monkeypatch, tmp_path):
    monkeypatch.setattr(ocr_engine, "_known_locations", lambda: [])
    monkeypatch.setattr(ocr_engine.shutil, "which", lambda name: None)
    monkeypatch.setenv("TRANSLATOR_APP_DATA_DIR", str(tmp_path / "app-data"))
    return tmp_path


def _tar_zst(files: dict[str, bytes | tuple[str, str]]) -> bytes:
    """files: 名字 -> 内容，或 ("link", 目标) 表示符号链接。"""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            if isinstance(data, tuple):
                info.type = tarfile.SYMTYPE
                info.linkname = data[1]
                archive.addfile(info)
                continue
            info.size = len(data)
            info.mode = 0o755 if name.startswith("bin/") else 0o644
            archive.addfile(info, io.BytesIO(data))
    return zstandard.ZstdCompressor().compress(raw.getvalue())


def _conda(files: dict[str, bytes | tuple[str, str]], name: str = "pkg-x-1.0-0.tar.zst") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("metadata.json", b'{"conda_pkg_format_version": 2}')
        archive.writestr(name, _tar_zst(files))
        archive.writestr("info-x-1.0-0.tar.zst", _tar_zst({"info/index.json": b"{}"}))
    return buffer.getvalue()


def _pkg_range(conda: bytes) -> tuple[int, int]:
    """.conda 里 pkg 成员数据的起点和长度（和 manifest 生成脚本的算法一样）。"""
    with zipfile.ZipFile(io.BytesIO(conda)) as archive:
        info = next(item for item in archive.infolist() if item.filename.startswith("pkg-"))
    header = conda[info.header_offset:info.header_offset + 30]
    name_length = int.from_bytes(header[26:28], "little")
    extra_length = int.from_bytes(header[28:30], "little")
    return info.header_offset + 30 + name_length + extra_length, info.compress_size


def _range_client(routes: dict[str, bytes | int], *, ignore_range: tuple[str, ...] = ()) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        value = routes.get(url, 404)
        if isinstance(value, int):
            return httpx.Response(value)
        header = request.headers.get("range")
        status = 200
        if header and url not in ignore_range:
            start, end = (int(part) for part in re.match(r"bytes=(\d+)-(\d+)", header).groups())
            value, status = value[start:end + 1], 206
        # 分块发，像真的网络一样：解压取齐就停时，后面的块不会被读。
        return httpx.Response(status, content=iter([value[i:i + 8192] for i in range(0, len(value), 8192)]))

    return httpx.Client(transport=httpx.MockTransport(handler))


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ── .conda extraction ─────────────────────────────────────────────────────


def test_extract_conda_takes_only_the_listed_files(tmp_path):
    package = tmp_path / "x.conda"
    package.write_bytes(_conda({
        "include/x.h": b"header",
        "lib/libx.1.2.dylib": b"library",
        "lib/libx.1.dylib": ("link", "libx.1.2.dylib"),
        "share/doc": b"docs",
    }))
    extract_conda(package, tmp_path / "out", {"lib/libx.1.2.dylib": "lib/libx.1.dylib"})
    assert (tmp_path / "out" / "lib" / "libx.1.dylib").read_bytes() == b"library"
    assert sorted(p.name for p in (tmp_path / "out").rglob("*") if p.is_file()) == ["libx.1.dylib"]


def test_extract_conda_fails_when_a_file_is_missing(tmp_path):
    package = tmp_path / "x.conda"
    package.write_bytes(_conda({"lib/a.so": b"a"}))
    with pytest.raises(ComponentDownloadError, match="不完整"):
        extract_conda(package, tmp_path / "out", {"lib/a.so": "lib/a.so", "lib/b.so": "lib/b.so"})


def test_extract_conda_refuses_a_symlink_where_a_file_is_expected(tmp_path):
    package = tmp_path / "x.conda"
    package.write_bytes(_conda({"lib/a.so": ("link", "/etc/passwd")}))
    with pytest.raises(ComponentDownloadError):
        extract_conda(package, tmp_path / "out", {"lib/a.so": "lib/a.so"})
    assert not (tmp_path / "out" / "lib" / "a.so").exists()


def test_extract_conda_rejects_paths_outside_the_package(tmp_path):
    package = tmp_path / "x.conda"
    package.write_bytes(_conda({"lib/a.so": b"a"}))
    with pytest.raises(ComponentDownloadError):
        extract_conda(package, tmp_path / "out", {"../a.so": "lib/a.so"})


def test_extract_conda_reports_a_damaged_package(tmp_path):
    package = tmp_path / "x.conda"
    package.write_bytes(b"not a zip")
    with pytest.raises(ComponentDownloadError, match="解压失败"):
        extract_conda(package, tmp_path / "out", {"lib/a.so": "lib/a.so"})


# ── reading only the start of the big tesseract package ──────────────────


def _stream_fixture():
    program = b"#!/bin/sh\necho tesseract\n"
    library = b"L" * 5000
    files = {"bin/tesseract": program, "lib/libtesseract.so.5": library}
    filler = {f"share/tessdata/{index:03}.traineddata": os.urandom(8000) for index in range(120)}
    conda = _conda({**files, **filler})
    offset, size = _pkg_range(conda)
    hashes = {name: (len(data), _sha(data)) for name, data in files.items()}
    item = RemoteFile("tesseract.conda", ("https://primary.test/t.conda", "https://mirror.test/t.conda"), 0, "")
    return conda, offset, size, files, hashes, item


def test_stream_reads_the_program_without_the_language_data(tmp_path):
    conda, offset, size, files, hashes, item = _stream_fixture()
    received: list[int] = []
    client = _range_client({"https://primary.test/t.conda": conda})
    stream_conda_files(item, offset, size, tmp_path, {name: name for name in files}, hashes,
                       progress=received.append, client=client)
    assert (tmp_path / "bin" / "tesseract").read_bytes() == files["bin/tesseract"]
    assert os.access(tmp_path / "bin" / "tesseract", os.X_OK) or os.name == "nt"
    assert (tmp_path / "lib" / "libtesseract.so.5").read_bytes() == files["lib/libtesseract.so.5"]
    assert received and received[-1] < size  # 取齐就停，没有读完整包


def test_stream_handles_a_server_that_ignores_range(tmp_path):
    conda, offset, size, files, hashes, item = _stream_fixture()
    client = _range_client({"https://primary.test/t.conda": conda}, ignore_range=("https://primary.test/t.conda",))
    stream_conda_files(item, offset, size, tmp_path, {name: name for name in files}, hashes, client=client)
    assert (tmp_path / "lib" / "libtesseract.so.5").read_bytes() == files["lib/libtesseract.so.5"]


def test_stream_moves_to_the_mirror_when_a_file_does_not_match(tmp_path):
    conda, offset, size, files, hashes, item = _stream_fixture()
    tampered = _conda({"bin/tesseract": b"#!/bin/sh\necho evil!!!!\n", "lib/libtesseract.so.5": b"L" * 5000})
    t_offset, _ = _pkg_range(tampered)
    assert t_offset == offset  # 同名同长度的成员，偏移相同
    client = _range_client({"https://primary.test/t.conda": tampered, "https://mirror.test/t.conda": conda})
    stream_conda_files(item, offset, size, tmp_path, {name: name for name in files}, hashes, client=client)
    assert (tmp_path / "bin" / "tesseract").read_bytes() == files["bin/tesseract"]


def test_stream_fails_when_every_source_fails(tmp_path):
    _, offset, size, files, hashes, item = _stream_fixture()
    client = _range_client({"https://primary.test/t.conda": 503, "https://mirror.test/t.conda": 404})
    with pytest.raises(ComponentDownloadError, match="下载失败"):
        stream_conda_files(item, offset, size, tmp_path, {name: name for name in files}, hashes, client=client)


def test_stream_can_be_cancelled(tmp_path):
    conda, offset, size, files, hashes, item = _stream_fixture()
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(component_download.ComponentDownloadCancelled):
        stream_conda_files(item, offset, size, tmp_path, {name: name for name in files}, hashes,
                           cancel=cancel, client=_range_client({"https://primary.test/t.conda": conda}))


# ── manifest ──────────────────────────────────────────────────────────────


def test_manifest_pins_every_file():
    for key, current in installer.MANIFEST.items():
        assert current.subdir in ("osx-arm64", "win-64", "linux-64"), key
        assert current.tesseract.name.startswith("tesseract-5.5.3-")
        assert current.tesseract.data_offset > 0 and current.tesseract.data_size > 100_000_000
        assert current.tesseract.files
        for name, size, sha in current.tesseract.files:
            assert size > 0 and re.fullmatch(r"[0-9a-f]{64}", sha), name
        names = [package.name for package in current.packages]
        assert len(names) == len(set(names))
        for package in current.packages:
            assert package.name.endswith(".conda") and package.size > 0
            assert re.fullmatch(r"[0-9a-f]{64}", package.sha256), package.name
            assert package.files
    for item in installer.TESSDATA:
        assert item.name.endswith(".traineddata") and item.size > 0 and re.fullmatch(r"[0-9a-f]{64}", item.sha256)
        assert all("tessdata_fast" in url and "4.1.0" in url for url in item.urls)


def test_manifest_covers_every_language_the_inplace_route_asks_for():
    from core.pdf_vector.runner import _OCR_LANGS

    wanted = {lang for value in _OCR_LANGS.values() for lang in value.split("+")}
    assert wanted <= set(installer.languages())


def test_windows_keeps_every_dll_next_to_the_program(monkeypatch):
    monkeypatch.setattr(installer.sys, "platform", "win32")
    assert installer._destination("Library/bin/leptonica-1.87.0.dll") == ("Library/bin/leptonica-1.87.0.dll", "bin/leptonica-1.87.0.dll")
    assert installer._destination("msvcp140.dll") == ("msvcp140.dll", "bin/msvcp140.dll")
    monkeypatch.setattr(installer.sys, "platform", "darwin")
    assert installer._destination("lib/libz.1.3.2.dylib>libz.1.dylib") == ("lib/libz.1.3.2.dylib", "lib/libz.1.dylib")
    assert installer._destination("bin/tesseract") == ("bin/tesseract", "bin/tesseract")


def test_download_size_is_reported_for_supported_platforms(monkeypatch):
    monkeypatch.setattr(installer, "platform_key", lambda: "macos-arm64")
    assert 35_000_000 < installer.download_size() < 60_000_000
    monkeypatch.setattr(installer, "platform_key", lambda: "windows-x64")
    assert 35_000_000 < installer.download_size() < 70_000_000
    monkeypatch.setattr(installer, "platform_key", lambda: "")
    assert installer.download_size() == 0


# ── one-click install, end to end with a fake tesseract ──────────────────

_FAKE_TESSERACT = b"""#!/bin/sh
if [ "$1" = "--version" ]; then echo "tesseract 5.5.3"; exit 0; fi
if [ "$1" = "--list-langs" ]; then
  echo "List of available languages in \\"$TESSDATA_PREFIX\\" (2):"
  for f in "$TESSDATA_PREFIX"/*.traineddata; do basename "$f" .traineddata; done
  exit 0
fi
[ -f "$TESSDATA_PREFIX/configs/tsv" ] || exit 3
[ -f "$(dirname "$0")/../lib/libfake.so.1" ] || exit 4
printf 'level\\ttext\\n5\\tPlan\\n5\\tde\\n5\\tmasse\\n'
"""


def _fake_install_sources(monkeypatch) -> dict[str, bytes]:
    library = b"fake library"
    lib_package = _conda({"lib/libfake.so.1.2": library, "lib/libfake.so.1": ("link", "libfake.so.1.2")})
    tesseract = _conda({"bin/tesseract": _FAKE_TESSERACT, "lib/libtesseract.so.5": b"T" * 100,
                        "share/tessdata/zzz.traineddata": b"Z" * 100})
    offset, size = _pkg_range(tesseract)
    langs = {f"{lang}.traineddata": f"{lang} data".encode() for lang in ("eng", "fra")}
    manifest = OcrManifest(
        subdir="linux-64",
        tesseract=TesseractStream("tesseract-5.5.3-test.conda", offset, size,
                                  ("bin/tesseract", len(_FAKE_TESSERACT), _sha(_FAKE_TESSERACT)),
                                  ("lib/libtesseract.so.5", 100, _sha(b"T" * 100))),
        packages=(CondaPackage("fake-1.2-0.conda", len(lib_package), _sha(lib_package),
                               ("lib/libfake.so.1.2>libfake.so.1",)),),
    )
    tessdata = tuple(RemoteFile(name, (f"https://data.test/{name}",), len(data), _sha(data))
                     for name, data in langs.items())
    monkeypatch.setattr(installer, "MANIFEST", {"linux-x64": manifest})
    monkeypatch.setattr(installer, "platform_key", lambda: "linux-x64")
    monkeypatch.setattr(installer, "TESSDATA", tessdata)
    host = component_download.CONDA_HOSTS[0]
    routes: dict[str, bytes | int] = {
        f"{host}linux-64/fake-1.2-0.conda": lib_package,
        f"{host}linux-64/tesseract-5.5.3-test.conda": tesseract,
        **{f"https://data.test/{name}": data for name, data in langs.items()},
    }
    monkeypatch.setattr(installer, "_http_client", lambda: _range_client(routes))
    return routes


@posix_only
def test_install_lays_out_checks_and_records_the_component(hermetic, monkeypatch):
    _fake_install_sources(monkeypatch)
    phases: list[str] = []
    info = installer.install(progress=lambda phase, done, total: phases.append(phase))
    assert info["installed"] is True and info["tesseract"] == "5.5.3"
    executable = Path(info["path"])
    base = executable.parent.parent
    assert (base / "lib" / "libfake.so.1").read_bytes() == b"fake library"
    assert (base / "tessdata" / "eng.traineddata").is_file()
    assert (base / "tessdata" / "configs" / "tsv").read_text() == "tessedit_create_tsv 1\n"
    assert {"download", "extract", "verify", "done"} <= set(phases)
    assert not (installer.component_root() / ".downloads").exists()
    # 一键安装的版本被选中，并带着它自己的语言数据目录。
    assert ocr_engine.resolve_tesseract() == executable.resolve()
    # 在组件目录里运行、语言数据给相对路径：Windows 上 tesseract 读不了系统代码页以外的路径。
    options = ocr_engine.tesseract_options(executable)
    assert options["cwd"] == str(base) and options["env"]["TESSDATA_PREFIX"] == "tessdata"
    status = ocr_engine.ocr_status()
    assert status["state"] == "connected" and status["source"] == "managed"
    assert status["languages"] == ["eng", "fra"] or sorted(status["languages"]) == ["eng", "fra"]

    # 重装换新目录，旧目录删掉。
    again = installer.install()
    assert again["path"] != info["path"] and not base.exists()

    installer.uninstall()
    assert installer.installed_executable() is None
    assert ocr_engine.resolve_tesseract() is None


@posix_only
def test_install_that_cannot_recognise_text_leaves_nothing_behind(hermetic, monkeypatch):
    _fake_install_sources(monkeypatch)
    monkeypatch.setattr(installer, "_CONFIGS", {})  # 假程序没有 tsv 配置就退出：试识别失败
    with pytest.raises(installer.ComponentInstallError, match="无法在本机运行"):
        installer.install()
    assert installer.installed_executable() is None
    assert not [path for path in installer.component_root().iterdir() if path.name != ".downloads"]


def test_install_refuses_unsupported_platforms(hermetic, monkeypatch):
    monkeypatch.setattr(installer, "platform_key", lambda: "")
    with pytest.raises(installer.ComponentInstallError, match="不支持"):
        installer.install()


def test_install_reports_a_failed_download(hermetic, monkeypatch):
    routes = _fake_install_sources(monkeypatch)
    for url in list(routes):
        routes[url] = 503
    with pytest.raises(installer.ComponentInstallError, match="下载失败"):
        installer.install()
    assert installer.installed_executable() is None


# ── which tesseract is used ───────────────────────────────────────────────


def _fake_managed(base: Path) -> Path:
    """在应用数据目录里摆一份「已装好」的组件（不走下载）。"""
    root = installer.component_root()
    target = root / "1-test"
    (target / "tessdata").mkdir(parents=True)
    executable = target / "bin" / "tesseract"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(_FAKE_TESSERACT)
    executable.chmod(0o755)
    (root / "installed.json").write_text('{"version": "1", "dir": "1-test"}', encoding="utf-8")
    return executable


@posix_only
def test_a_chosen_location_wins_over_the_one_click_install(hermetic):
    managed = _fake_managed(hermetic)
    own = hermetic / "own" / "tesseract"
    own.parent.mkdir()
    own.write_bytes(b'#!/bin/sh\n[ "$1" = "--version" ] && echo "tesseract 5.3.0" && exit 0\nexit 0\n')
    own.chmod(0o755)
    assert ocr_engine.resolve_tesseract() == managed.resolve()
    ocr_engine.save_tesseract_path(str(own))
    assert ocr_engine.resolve_tesseract() == own.resolve()
    assert ocr_engine.ocr_status()["source"] == "manual"
    assert ocr_engine.tesseract_options(own) == {}  # 用户自己的照常继承环境


def test_installed_record_cannot_point_outside_the_component_folder(hermetic):
    root = installer.component_root()
    root.mkdir(parents=True)
    (root / "installed.json").write_text('{"dir": "../../elsewhere"}', encoding="utf-8")
    assert installer.installed_executable() is None


@posix_only
def test_inplace_ocr_runs_the_managed_program_with_its_language_data(hermetic, monkeypatch, tmp_path):
    from PIL import Image

    from core.pdf_vector import extract

    managed = _fake_managed(hermetic)
    seen: list[dict] = []
    real_run = subprocess.run

    def spy(command, **kwargs):
        seen.append({"command": command, **kwargs})
        return real_run(command, **kwargs)

    monkeypatch.setattr(extract.subprocess, "run", spy)
    extract._tess(Image.new("L", (40, 20), 255), "eng", str(managed))
    [call] = seen
    assert call["env"]["TESSDATA_PREFIX"] == "tessdata" and call["cwd"] == str(managed.parent.parent)
    # 图片从 stdin 给，不经临时目录的路径（Windows 上中文用户名下的路径 tesseract 读不了）。
    assert call["command"][1] == "stdin" and call["input"].startswith(b"\x89PNG")


# ── plugins page API ──────────────────────────────────────────────────────


@posix_only
def test_a_chosen_tesseract_through_the_api(hermetic, monkeypatch):
    # 一键安装现在装的是识别模型（tests/test_ppocr_component.py）；自己装的 Tesseract 照旧能连接、断开。
    from api.app import create_app

    own = hermetic / "own" / "tesseract"
    own.parent.mkdir()
    own.write_bytes(b'#!/bin/sh\n[ "$1" = "--version" ] && echo "tesseract 5.3.0" && exit 0\nexit 0\n')
    own.chmod(0o755)
    client = TestClient(create_app())
    status = client.post("/api/plugins/ocr/install", json={"path": str(own)}).json()
    assert status["state"] == "connected" and status["source"] == "manual" and status["engine"] == "tesseract"
    removed = client.post("/api/plugins/ocr/uninstall", json={"target": "manual"}).json()
    assert removed["state"] == "missing" and removed["engine"] == ""
    assert own.exists(), "never delete the user's own install"


def test_frozen_smoke_checks_the_decompressor():
    from scripts.frozen_smoke import _CRITICAL_IMPORTS

    assert "zstandard" in _CRITICAL_IMPORTS
    spec = (Path(__file__).resolve().parents[1] / "packaging" / "sidecar" / "translator_sidecar.spec").read_text(encoding="utf-8")
    assert '"zstandard"' in spec
    assert sys.version_info >= (3, 11)
