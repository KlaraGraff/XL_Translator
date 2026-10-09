from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from api import cad_plugin as cad


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("TRANSLATOR_APP_DATA_DIR", str(tmp_path / "app-data"))
    monkeypatch.setattr(cad.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(cad.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(cad.platform, "mac_ver", lambda: ("13.6", ()))
    monkeypatch.setattr(cad.platform, "release", lambda: "22.0")
    return tmp_path


def _mach_o(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xcf\xfa\xed\xfe" + b"\0" * 128)


def test_builtin_marker_and_external_oda_lifecycle(isolated):
    status = cad.install_builtin()
    assert status["plugin"] == "enabled"
    assert status["oda"] == "missing"
    converter = isolated / "ODAFileConverter.app" / "Contents" / "MacOS" / "ODAFileConverter"
    _mach_o(converter)
    status = cad.connect_oda(str(converter))
    assert status["oda"] == "connected"
    assert status["enabled"] is True
    assert status["official_download_url"] == cad.OFFICIAL_DOWNLOAD_URL
    assert converter.exists()
    assert cad.uninstall()["plugin"] == "missing"
    assert converter.exists()


def test_text_fixture_never_counts_as_converter(isolated):
    cad.install_builtin()
    converter = isolated / "ODAFileConverter.app" / "Contents" / "MacOS" / "ODAFileConverter"
    converter.parent.mkdir(parents=True)
    converter.write_text("this is not a binary", encoding="utf-8")
    with pytest.raises(ValueError):
        cad.connect_oda(str(converter))
    assert cad.probe_status()["oda"] == "missing"


def test_package_requires_every_file_checksum_and_rejects_symlink(isolated):
    package = isolated / "package"
    package.mkdir()
    payload = package / "wrapper.py"
    payload.write_text("adapter", encoding="utf-8")
    (package / "plugin.json").write_text(
        json.dumps({"id": cad.PLUGIN_ID, "version": "0.2.0", "adapter": "builtin", "sha256": {}}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="全部文件"):
        cad.install_from_directory(str(package))
    manifest = {
        "id": cad.PLUGIN_ID,
        "version": "0.2.0",
        "adapter": "builtin",
        "sha256": {"wrapper.py": hashlib.sha256(payload.read_bytes()).hexdigest()},
    }
    (package / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    cad.install_from_directory(str(package))
    assert cad.probe_status()["plugin"] == "enabled"


def test_windows_requires_pe_and_version_metadata(monkeypatch, tmp_path):
    monkeypatch.setenv("TRANSLATOR_APP_DATA_DIR", str(tmp_path / "app-data"))
    monkeypatch.setattr(cad.platform, "system", lambda: "Windows")
    monkeypatch.setattr(cad.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(cad.platform, "release", lambda: "10")
    monkeypatch.setattr(cad, "_windows_version_metadata", lambda path: "24.1.0.0")
    cad.install_builtin()
    converter = tmp_path / "ODAFileConverter.exe"
    data = bytearray(256)
    data[:2] = b"MZ"
    data[0x3C:0x40] = (128).to_bytes(4, "little")
    data[128:132] = b"PE\0\0"
    converter.write_bytes(data)
    assert cad.connect_oda(str(converter))["oda"] == "connected"
