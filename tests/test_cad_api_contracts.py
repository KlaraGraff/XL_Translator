from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

import settings as app_settings
from api.app import create_app
from api.cad_plugin import OFFICIAL_DOWNLOAD_URL
from api.task_manager import TaskOptions, TranslationTaskManager
from settings import AppSettings


def _dxf_text() -> str:
    return "0\nSECTION\n2\nENTITIES\n0\nTEXT\n1\nDoor\n0\nENDSEC\n0\nEOF\n"


def test_official_cad_download_uses_chinese_vendor_page():
    with TestClient(create_app()) as client:
        response = client.post("/api/plugins/cad/official-download", json={})

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["url"] == OFFICIAL_DOWNLOAD_URL
    assert payload["authorization_url"] == OFFICIAL_DOWNLOAD_URL
    assert payload["url"] == "https://www.opendesign.com/cn/guestfiles/oda_file_converter"


def test_cad_scan_sources_exposes_capability_at_response_root(tmp_path, monkeypatch):
    monkeypatch.setattr(app_settings, "APP_DATA_DIR", tmp_path / "app-data")
    source = tmp_path / "sample.dxf"
    source.write_text(_dxf_text(), encoding="utf-8")
    status = {
        "plugin": "enabled",
        "oda": "connected",
        "converter": None,
        "enabled": True,
    }
    with patch("api.app.probe_status", return_value=status):
        with TestClient(create_app()) as client:
            response = client.post(
                "/api/sources/scan",
                json={"surface": "cad", "path": str(source)},
            )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["capability"] == status
    assert payload["risk"]["capability"] == status
    assert payload["items"][0]["format"] == "dxf"


def test_cad_scan_route_accepts_a_shell_quoted_path(tmp_path, monkeypatch):
    monkeypatch.setattr(app_settings, "APP_DATA_DIR", tmp_path / "app-data")
    source = tmp_path / "quoted drawing.dxf"
    source.write_text(_dxf_text(), encoding="utf-8")
    status = {"plugin": "enabled", "oda": "connected", "converter": None, "enabled": True}
    with patch("api.app.probe_status", return_value=status):
        with TestClient(create_app()) as client:
            response = client.post(
                "/api/cad/scan",
                json={"paths": [f'"{source}"'], "target_language": "zh"},
            )

    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["path"] == str(source)


def test_cad_auto_source_language_reaches_translation_engine(tmp_path, monkeypatch):
    monkeypatch.setattr(app_settings, "APP_DATA_DIR", tmp_path / "app-data")
    settings = AppSettings()
    manager = TranslationTaskManager(settings_loader=lambda: settings)
    source = tmp_path / "sample.dxf"
    source.write_text(_dxf_text(), encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_translate(*args, **kwargs):
        captured.update(kwargs)
        return {"Door": "门"}

    with (
        patch("api.task_manager.probe_status", return_value={"converter": ""}),
        patch("api.task_manager.build_role_engine", return_value=object()),
        patch("core.engine_dispatcher.get_system_prompt", return_value="CAD prompt"),
        patch("core.engine_dispatcher.translate_texts", side_effect=fake_translate),
    ):
        runner = manager._build_runner(
            surface="cad",
            files=[SimpleNamespace(path=source, name=source.name, format="dxf")],
            settings=settings,
            source_root=tmp_path,
            options=TaskOptions(),
            source_lang="auto",
            key_overrides={},
        )
        runner._translator(["Door"], {})

    assert captured["source_lang"] == "auto"


def test_domain_prompt_optimize_accepts_saved_custom_prompt(tmp_path, monkeypatch):
    app_data = tmp_path / "app-data"
    monkeypatch.setattr(app_settings, "APP_DATA_DIR", app_data)
    monkeypatch.setattr(app_settings, "SETTINGS_PATH", app_data / "settings.json")
    monkeypatch.setattr(app_settings, "KEYS_PATH", app_data / "keys.json")
    monkeypatch.setattr(app_settings, "BACKUPS_DIR", app_data / "backups")
    monkeypatch.setattr(app_settings, "RECOVERY_PATH", app_data / "recovery.json")

    class FakeEngine:
        def chat(self, system, user):
            assert "当前提示词名称：我的 CAD 提示词" in user
            return "整理后的 CAD 提示词"

    with TestClient(create_app()) as client:
        saved = client.put(
            "/api/domains/cad",
            json={
                "preset": "我的 CAD 提示词",
                "custom_prompts": {"我的 CAD 提示词": {"_base": "原始 CAD 提示词"}},
            },
        )
        assert saved.status_code == 200, saved.text
        with patch("api.app.build_role_engine", return_value=FakeEngine()):
            response = client.post(
                "/api/domains/cad/optimize",
                json={
                    "preset": "我的 CAD 提示词",
                    "target_lang": "zh",
                    "prompt": "原始 CAD 提示词",
                },
            )

    assert response.status_code == 200, response.text
    assert response.json()["prompt"] == "整理后的 CAD 提示词"


def test_cad_scan_excludes_custom_output_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(app_settings, "APP_DATA_DIR", tmp_path / "app-data")
    source = tmp_path / "source.dxf"
    source.write_text(_dxf_text(), encoding="utf-8")
    output = tmp_path / "translated"
    output.mkdir()
    generated = output / "source_中文.dxf"
    generated.write_text(_dxf_text(), encoding="utf-8")
    manager = TranslationTaskManager(settings_loader=AppSettings)
    with patch("api.task_manager.probe_status", return_value={"converter": ""}):
        files = manager._scan(
            tmp_path,
            "cad",
            TaskOptions(cad_output_dir=str(output)),
        )
    assert [item.path for item in files] == [source]
