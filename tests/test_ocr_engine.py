from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core import ocr_engine as ocr


def _write_fake_tesseract(path: Path, *, version: str = "5.3.1", langs: tuple[str, ...] = ("eng", "chi_sim")) -> None:
    """Write an executable shell script standing in for the real tesseract binary."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lang_lines = "\n".join(f'echo "{lang}"' for lang in langs)
    script = (
        "#!/bin/sh\n"
        f'if [ "$1" = "--version" ]; then echo "tesseract {version}"; exit 0; fi\n'
        f'if [ "$1" = "--list-langs" ]; then echo "List of available languages ({len(langs)}):"; {lang_lines}; exit 0; fi\n'
        "exit 1\n"
    )
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)


def _write_broken_executable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def hermetic(monkeypatch, tmp_path):
    # Keep the machine's real tesseract installation and PATH out of these tests.
    monkeypatch.setattr(ocr, "_known_locations", lambda: [])
    monkeypatch.setattr(ocr.shutil, "which", lambda name: None)
    monkeypatch.setenv("TRANSLATOR_APP_DATA_DIR", str(tmp_path / "app-data"))
    return tmp_path


# ── tesseract_version / tesseract_languages ──────────────────────────────


def test_tesseract_version_parses_first_line(hermetic):
    exe = hermetic / "tesseract"
    _write_fake_tesseract(exe, version="5.3.1")
    assert ocr.tesseract_version(exe) == "5.3.1"


def test_tesseract_version_never_raises_on_missing_file(hermetic):
    assert ocr.tesseract_version(hermetic / "nope") is None


def test_tesseract_languages_strips_header_line(hermetic):
    exe = hermetic / "tesseract"
    _write_fake_tesseract(exe, langs=("eng", "fra", "chi_sim"))
    assert ocr.tesseract_languages(exe) == ["eng", "fra", "chi_sim"]


def test_tesseract_languages_never_raises_on_broken_executable(hermetic):
    exe = hermetic / "tesseract"
    _write_broken_executable(exe)
    assert ocr.tesseract_languages(exe) == []


# ── resolve_tesseract: config path > PATH > known locations ──────────────


def test_resolve_prefers_configured_path_over_path_env(hermetic, monkeypatch):
    on_path = hermetic / "path-tesseract" / "tesseract"
    _write_fake_tesseract(on_path)
    monkeypatch.setattr(ocr.shutil, "which", lambda name: str(on_path))

    configured = hermetic / "configured" / "tesseract"
    _write_fake_tesseract(configured)
    config_path = hermetic / "ocr_plugin.json"
    config_path.write_text(f'{{"path": "{configured}"}}', encoding="utf-8")

    resolved = ocr.resolve_tesseract(config_path)
    assert resolved == configured.resolve()


def test_resolve_falls_back_to_path_env_when_unconfigured(hermetic, monkeypatch):
    on_path = hermetic / "path-tesseract" / "tesseract"
    _write_fake_tesseract(on_path)
    monkeypatch.setattr(ocr.shutil, "which", lambda name: str(on_path))

    resolved = ocr.resolve_tesseract(hermetic / "missing-config.json")
    assert resolved == on_path.resolve()


def test_resolve_falls_back_to_known_locations_last(hermetic, monkeypatch):
    known = hermetic / "known" / "tesseract"
    _write_fake_tesseract(known)
    monkeypatch.setattr(ocr, "_known_locations", lambda: [known])

    resolved = ocr.resolve_tesseract(hermetic / "missing-config.json")
    assert resolved == known.resolve()


def test_resolve_accepts_a_folder_in_config(hermetic):
    folder = hermetic / "install-dir"
    _write_fake_tesseract(folder / "tesseract")
    config_path = hermetic / "ocr_plugin.json"
    config_path.write_text(f'{{"path": "{folder}"}}', encoding="utf-8")

    resolved = ocr.resolve_tesseract(config_path)
    assert resolved == (folder / "tesseract").resolve()


def test_resolve_returns_none_when_nothing_found(hermetic):
    assert ocr.resolve_tesseract(hermetic / "missing-config.json") is None


# ── invalid path handling ─────────────────────────────────────────────────


def test_save_tesseract_path_rejects_broken_executable(hermetic):
    exe = hermetic / "tesseract"
    _write_broken_executable(exe)
    with pytest.raises(ValueError):
        ocr.save_tesseract_path(str(exe), hermetic / "ocr_plugin.json")


def test_save_tesseract_path_rejects_missing_path(hermetic):
    with pytest.raises(ValueError):
        ocr.save_tesseract_path(str(hermetic / "does-not-exist"), hermetic / "ocr_plugin.json")


def test_save_tesseract_path_accepts_folder_and_persists(hermetic):
    folder = hermetic / "install-dir"
    _write_fake_tesseract(folder / "tesseract")
    config_path = hermetic / "ocr_plugin.json"
    status = ocr.save_tesseract_path(str(folder), config_path)
    assert status["state"] == "connected"
    assert ocr.resolve_tesseract(config_path) == (folder / "tesseract").resolve()


def test_ocr_status_reports_invalid_for_a_saved_path_that_stopped_working(hermetic):
    exe = hermetic / "tesseract"
    _write_fake_tesseract(exe)
    config_path = hermetic / "ocr_plugin.json"
    ocr.save_tesseract_path(str(exe), config_path)

    exe.unlink()  # The user's install disappeared after being saved.
    status = ocr.ocr_status(config_path)
    assert status["state"] == "invalid"
    assert status["path"] == str(exe)


def test_clear_tesseract_path_forgets_the_saved_path(hermetic):
    exe = hermetic / "tesseract"
    _write_fake_tesseract(exe)
    config_path = hermetic / "ocr_plugin.json"
    ocr.save_tesseract_path(str(exe), config_path)
    ocr.clear_tesseract_path(config_path)
    assert not config_path.exists()
    assert ocr.resolve_tesseract(config_path) is None


# ── status shape ───────────────────────────────────────────────────────────


def test_status_shape_when_missing(hermetic):
    status = ocr.ocr_status(hermetic / "missing-config.json")
    assert status == {
        "state": "missing",
        "source": "",
        "path": "",
        "version": None,
        "languages": [],
        "has_latin": False,
        "message": status["message"],
        "managed_installed": False,
        "one_click_supported": status["one_click_supported"],
        "download_bytes": status["download_bytes"],
    }
    assert isinstance(status["message"], str) and status["message"]


def test_status_shape_when_connected_reports_version_languages_and_latin(hermetic):
    exe = hermetic / "tesseract"
    _write_fake_tesseract(exe, version="5.3.1", langs=("eng", "chi_sim"))
    config_path = hermetic / "ocr_plugin.json"
    ocr.save_tesseract_path(str(exe), config_path)

    status = ocr.ocr_status(config_path)
    assert status["state"] == "connected"
    assert status["version"] == "5.3.1"
    assert status["languages"] == ["eng", "chi_sim"]
    assert status["has_latin"] is True


def test_has_latin_false_without_any_latin_language_pack(hermetic):
    exe = hermetic / "tesseract"
    _write_fake_tesseract(exe, langs=("chi_sim", "jpn"))
    config_path = hermetic / "ocr_plugin.json"
    ocr.save_tesseract_path(str(exe), config_path)

    status = ocr.ocr_status(config_path)
    assert status["has_latin"] is False


def test_default_ocr_config_path_is_outside_the_cad_plugin_folder(hermetic):
    # Installing a CAD plugin package replaces plugins/cad-support wholesale.
    from api.cad_plugin import _oda_config_path

    assert ocr.default_ocr_config_path().parent != _oda_config_path().parent
    assert ocr.default_ocr_config_path().parent.parent == _oda_config_path().parent.parent
    assert ocr.default_ocr_config_path().name == "ocr_plugin.json"


# ── routes ───────────────────────────────────────────────────────────────


@pytest.fixture
def app_client(hermetic, monkeypatch):
    monkeypatch.setattr(ocr, "_known_locations", lambda: [])
    from api.app import create_app

    with TestClient(create_app()) as client:
        yield client


def test_ocr_plugin_status_route_reports_missing_when_undetected(app_client):
    response = app_client.get("/api/plugins/ocr")
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "missing"


def test_ocr_plugin_install_route_connects_a_valid_path(app_client, hermetic):
    folder = hermetic / "install-dir"
    _write_fake_tesseract(folder / "tesseract")
    response = app_client.post("/api/plugins/ocr/install", json={"path": str(folder)})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["state"] == "connected"
    assert payload["version"] == "5.3.1"


def test_ocr_plugin_install_route_rejects_invalid_path(app_client, hermetic):
    response = app_client.post("/api/plugins/ocr/install", json={"path": str(hermetic / "nope")})
    assert response.status_code == 422, response.text


def test_ocr_plugin_install_route_rejects_empty_path(app_client):
    response = app_client.post("/api/plugins/ocr/install", json={"path": ""})
    assert response.status_code == 422, response.text


def test_ocr_plugin_probe_route_reflects_current_state(app_client, hermetic):
    folder = hermetic / "install-dir"
    _write_fake_tesseract(folder / "tesseract")
    app_client.post("/api/plugins/ocr/install", json={"path": str(folder)})

    response = app_client.post("/api/plugins/ocr/probe", json={})
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "connected"


def test_ocr_plugin_uninstall_route_forgets_saved_path(app_client, hermetic):
    folder = hermetic / "install-dir"
    _write_fake_tesseract(folder / "tesseract")
    app_client.post("/api/plugins/ocr/install", json={"path": str(folder)})

    response = app_client.post("/api/plugins/ocr/uninstall", json={})
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "missing"


def test_ocr_plugin_official_download_route_returns_project_url(app_client):
    from api.ocr_plugin import OFFICIAL_OCR_URL

    response = app_client.post("/api/plugins/ocr/official-download", json={})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["url"] == OFFICIAL_OCR_URL
    assert payload["authorization_url"] == OFFICIAL_OCR_URL


def test_resolve_follows_a_symlinked_install(hermetic, monkeypatch):
    # Homebrew installs /opt/homebrew/bin/tesseract as a link to the Cellar binary.
    real = hermetic / "Cellar" / "bin" / "tesseract"
    _write_fake_tesseract(real)
    link_dir = hermetic / "bin"
    link_dir.mkdir()
    link = link_dir / "tesseract"
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("symlinks not available")
    monkeypatch.setattr(ocr, "_known_locations", lambda: [link])
    assert ocr.resolve_tesseract(hermetic / "missing-config.json") == real.resolve()
