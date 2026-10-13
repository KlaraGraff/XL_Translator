from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core import pdf2docx_plugin as plugin

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="fake executables are POSIX shell scripts")

# The real CLI (python-fire) prints help and per-page INFO lines on stderr.
_HELP = (
    "INFO: Showing help with the command 'pdf2docx -- --help'.\n"
    "NAME\n    pdf2docx - Command line interface for ``pdf2docx``.\n"
    "COMMANDS\n     convert\n       Convert pdf file to docx file.\n"
)


def _write_fake_pdf2docx(path: Path, *, writes_output: bool = True, exit_code: int = 0) -> None:
    """Write an executable shell script standing in for the real pdf2docx CLI."""
    path.parent.mkdir(parents=True, exist_ok=True)
    write = '  out="${3#--docx_file=}"; printf "PK-fake-docx" > "$out"\n' if writes_output else ""
    fail = '  echo "something went wrong" >&2\n' if exit_code else ""
    script = (
        "#!/bin/sh\n"
        f"if [ \"$1\" = \"--help\" ]; then cat >&2 <<'HELP'\n{_HELP}HELP\nexit 0; fi\n"
        'if [ "$1" = "convert" ]; then\n'
        '  echo "[INFO] Start to convert $2" >&2\n'
        '  echo "[INFO] (1/1) Page 1"\n'
        f"{write}{fail}"
        f"  exit {exit_code}\n"
        "fi\n"
        "exit 2\n"
    )
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)


def _write_unrelated_executable(path: Path) -> None:
    """Runs fine but is not pdf2docx: its help does not list a convert command."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\necho 'usage: something else'\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)


def _write_broken_executable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def hermetic(monkeypatch, tmp_path):
    # Keep the machine's real pdf2docx installation and PATH out of these tests.
    monkeypatch.setattr(plugin.shutil, "which", lambda name: None)
    monkeypatch.setenv("TRANSLATOR_APP_DATA_DIR", str(tmp_path / "app-data"))
    return tmp_path


# ── probe ─────────────────────────────────────────────────────────────────


def test_probe_accepts_the_cli_whose_help_lists_convert(hermetic):
    exe = hermetic / "pdf2docx"
    _write_fake_pdf2docx(exe)
    assert plugin.probe_pdf2docx(exe) is True


def test_probe_rejects_a_program_without_the_convert_command(hermetic):
    exe = hermetic / "pdf2docx"
    _write_unrelated_executable(exe)
    assert plugin.probe_pdf2docx(exe) is False


def test_probe_rejects_a_failing_program(hermetic):
    exe = hermetic / "pdf2docx"
    _write_broken_executable(exe)
    assert plugin.probe_pdf2docx(exe) is False


def test_probe_never_raises_on_missing_file(hermetic):
    assert plugin.probe_pdf2docx(hermetic / "nope") is False


# ── resolve: config path > PATH ───────────────────────────────────────────


def test_resolve_prefers_configured_path_over_path_env(hermetic, monkeypatch):
    on_path = hermetic / "path-bin" / "pdf2docx"
    _write_fake_pdf2docx(on_path)
    monkeypatch.setattr(plugin.shutil, "which", lambda name: str(on_path))

    configured = hermetic / "configured" / "pdf2docx"
    _write_fake_pdf2docx(configured)
    config_path = hermetic / "plugin.json"
    config_path.write_text(f'{{"path": "{configured}"}}', encoding="utf-8")

    assert plugin.resolve_pdf2docx(config_path) == configured.resolve()


def test_resolve_falls_back_to_path_env_when_unconfigured(hermetic, monkeypatch):
    on_path = hermetic / "path-bin" / "pdf2docx"
    _write_fake_pdf2docx(on_path)
    monkeypatch.setattr(plugin.shutil, "which", lambda name: str(on_path))

    assert plugin.resolve_pdf2docx(hermetic / "missing-config.json") == on_path.resolve()


def test_resolve_ignores_an_unrelated_program_on_path(hermetic, monkeypatch):
    on_path = hermetic / "path-bin" / "pdf2docx"
    _write_unrelated_executable(on_path)
    monkeypatch.setattr(plugin.shutil, "which", lambda name: str(on_path))

    assert plugin.resolve_pdf2docx(hermetic / "missing-config.json") is None


def test_resolve_accepts_a_virtualenv_folder_in_config(hermetic):
    venv = hermetic / "venv"
    _write_fake_pdf2docx(venv / "bin" / "pdf2docx")
    config_path = hermetic / "plugin.json"
    config_path.write_text(f'{{"path": "{venv}"}}', encoding="utf-8")

    assert plugin.resolve_pdf2docx(config_path) == (venv / "bin" / "pdf2docx").resolve()


def test_resolve_returns_none_when_nothing_found(hermetic):
    assert plugin.resolve_pdf2docx(hermetic / "missing-config.json") is None


def test_resolve_follows_a_symlinked_install(hermetic, monkeypatch):
    # pipx installs ~/.local/bin/pdf2docx as a link into its own virtualenv.
    real = hermetic / "pipx" / "venvs" / "pdf2docx" / "bin" / "pdf2docx"
    _write_fake_pdf2docx(real)
    link = hermetic / "local-bin" / "pdf2docx"
    link.parent.mkdir()
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("symlinks not available")
    monkeypatch.setattr(plugin.shutil, "which", lambda name: str(link))
    assert plugin.resolve_pdf2docx(hermetic / "missing-config.json") == real.resolve()


def test_corrupt_config_is_treated_as_unconfigured(hermetic):
    config_path = hermetic / "plugin.json"
    config_path.write_text("[not, an, object", encoding="utf-8")
    assert plugin.resolve_pdf2docx(config_path) is None
    assert plugin.pdf2docx_status(config_path)["state"] == "missing"


# ── save / load / disconnect ──────────────────────────────────────────────


def test_save_rejects_missing_path(hermetic):
    with pytest.raises(ValueError):
        plugin.save_pdf2docx_path(str(hermetic / "does-not-exist"), hermetic / "plugin.json")


def test_save_rejects_unrelated_program(hermetic):
    exe = hermetic / "pdf2docx"
    _write_unrelated_executable(exe)
    with pytest.raises(ValueError):
        plugin.save_pdf2docx_path(str(exe), hermetic / "plugin.json")


def test_save_accepts_folder_and_persists(hermetic):
    venv = hermetic / "venv"
    _write_fake_pdf2docx(venv / "bin" / "pdf2docx")
    config_path = hermetic / "plugin.json"
    status = plugin.save_pdf2docx_path(str(venv), config_path)
    assert status["state"] == "connected"
    assert status["path"] == str((venv / "bin" / "pdf2docx").resolve())
    assert plugin.resolve_pdf2docx(config_path) == (venv / "bin" / "pdf2docx").resolve()


def test_status_reports_invalid_for_a_saved_path_that_stopped_working(hermetic):
    exe = hermetic / "pdf2docx"
    _write_fake_pdf2docx(exe)
    config_path = hermetic / "plugin.json"
    plugin.save_pdf2docx_path(str(exe), config_path)

    exe.unlink()  # The user's install disappeared after being saved.
    status = plugin.pdf2docx_status(config_path)
    assert status["state"] == "invalid"
    assert status["path"] == str(exe)


def test_disconnect_also_ignores_pdf2docx_on_path_until_reconnected(hermetic, monkeypatch):
    on_path = hermetic / "path-bin" / "pdf2docx"
    _write_fake_pdf2docx(on_path)
    monkeypatch.setattr(plugin.shutil, "which", lambda name: str(on_path))
    config_path = hermetic / "plugin.json"
    assert plugin.pdf2docx_status(config_path)["state"] == "connected"

    plugin.clear_pdf2docx_path(config_path)
    assert plugin.resolve_pdf2docx(config_path) is None
    assert plugin.pdf2docx_status(config_path)["state"] == "missing"
    assert on_path.is_file()  # Disconnecting never deletes the user's install.

    status = plugin.save_pdf2docx_path(str(on_path), config_path)
    assert status["state"] == "connected"


def test_status_shape_when_missing(hermetic):
    status = plugin.pdf2docx_status(hermetic / "missing-config.json")
    assert status == {"state": "missing", "path": "", "message": status["message"]}
    assert isinstance(status["message"], str) and status["message"]


def test_default_config_path_is_outside_the_cad_and_ocr_plugin_folders(hermetic):
    from api.cad_plugin import _oda_config_path
    from core.ocr_engine import default_ocr_config_path

    path = plugin.default_pdf2docx_config_path()
    assert path.parent.name == "pdf2docx-support"
    assert path.name == "plugin.json"
    assert path.parent not in (_oda_config_path().parent, default_ocr_config_path().parent)
    assert path.parent.parent == default_ocr_config_path().parent.parent


# ── convert_with_plugin ───────────────────────────────────────────────────


def test_convert_writes_the_docx_and_ignores_info_noise(hermetic):
    exe = hermetic / "pdf2docx"
    _write_fake_pdf2docx(exe)
    out = hermetic / "out" / "doc.docx"
    plugin.convert_with_plugin(exe, hermetic / "in.pdf", out, timeout=20)
    assert out.read_bytes() == b"PK-fake-docx"


def test_convert_raises_when_no_output_is_written(hermetic):
    exe = hermetic / "pdf2docx"
    _write_fake_pdf2docx(exe, writes_output=False)
    out = hermetic / "out" / "doc.docx"
    out.parent.mkdir()
    out.write_bytes(b"stale")  # A leftover from an earlier run must not count as success.
    with pytest.raises(plugin.Pdf2DocxPluginError, match="did not write"):
        plugin.convert_with_plugin(exe, hermetic / "in.pdf", out, timeout=20)
    assert not out.exists()


def test_convert_raises_on_non_zero_exit(hermetic):
    exe = hermetic / "pdf2docx"
    _write_fake_pdf2docx(exe, exit_code=3)
    out = hermetic / "doc.docx"
    with pytest.raises(plugin.Pdf2DocxPluginError, match="code 3") as info:
        plugin.convert_with_plugin(exe, hermetic / "in.pdf", out, timeout=20)
    assert "something went wrong" in str(info.value)
    assert "[INFO]" not in str(info.value)
    assert not out.exists()


def test_convert_raises_on_timeout(hermetic):
    exe = hermetic / "pdf2docx"
    exe.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
    exe.chmod(0o755)
    with pytest.raises(plugin.Pdf2DocxPluginError, match="timed out"):
        plugin.convert_with_plugin(exe, hermetic / "in.pdf", hermetic / "doc.docx", timeout=0.5)


# ── pdf_word_route integration ────────────────────────────────────────────


def test_word_route_available_follows_the_plugin(hermetic, monkeypatch):
    from core import pdf_word_route

    assert pdf_word_route.word_route_available() is False
    exe = hermetic / "pdf2docx"
    _write_fake_pdf2docx(exe)
    plugin.save_pdf2docx_path(str(exe))
    assert pdf_word_route.word_route_available() is True
    plugin.clear_pdf2docx_path()
    assert pdf_word_route.word_route_available() is False


def test_convert_pdf_to_docx_raises_without_plugin(hermetic):
    from core import pdf_word_route

    with pytest.raises(plugin.Pdf2DocxPluginError):
        pdf_word_route.convert_pdf_to_docx(hermetic / "in.pdf", hermetic / "out.docx")


def test_convert_pdf_to_docx_checks_should_stop_before_starting(hermetic):
    from core import pdf_word_route

    exe = hermetic / "pdf2docx"
    _write_fake_pdf2docx(exe)
    plugin.save_pdf2docx_path(str(exe))
    with pytest.raises(pdf_word_route.WordRouteStopped):
        pdf_word_route.convert_pdf_to_docx(hermetic / "in.pdf", hermetic / "out.docx", should_stop=lambda: True)
    assert not (hermetic / "out.docx").exists()


# ── routes ───────────────────────────────────────────────────────────────


@pytest.fixture
def app_client(hermetic):
    from api.app import create_app

    with TestClient(create_app()) as client:
        yield client


def test_status_route_reports_missing_when_undetected(app_client):
    response = app_client.get("/api/plugins/pdf2docx")
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "missing"


def test_install_route_connects_a_valid_path(app_client, hermetic):
    venv = hermetic / "venv"
    _write_fake_pdf2docx(venv / "bin" / "pdf2docx")
    response = app_client.post("/api/plugins/pdf2docx/install", json={"path": str(venv)})
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "connected"


def test_install_route_rejects_invalid_and_empty_paths(app_client, hermetic):
    assert app_client.post("/api/plugins/pdf2docx/install", json={"path": str(hermetic / "nope")}).status_code == 422
    assert app_client.post("/api/plugins/pdf2docx/install", json={"path": ""}).status_code == 422


def test_probe_and_uninstall_routes(app_client, hermetic):
    exe = hermetic / "pdf2docx"
    _write_fake_pdf2docx(exe)
    app_client.post("/api/plugins/pdf2docx/install", json={"path": str(exe)})

    response = app_client.post("/api/plugins/pdf2docx/probe", json={})
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "connected"

    response = app_client.post("/api/plugins/pdf2docx/uninstall", json={})
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "missing"
    assert exe.is_file()


def test_official_download_route_returns_project_url(app_client):
    from api.pdf2docx_plugin import OFFICIAL_PDF2DOCX_URL

    response = app_client.post("/api/plugins/pdf2docx/official-download", json={})
    assert response.status_code == 200, response.text
    assert response.json() == {"authorization_url": OFFICIAL_PDF2DOCX_URL, "url": OFFICIAL_PDF2DOCX_URL}
    assert OFFICIAL_PDF2DOCX_URL == "https://github.com/ArtifexSoftware/pdf2docx"
