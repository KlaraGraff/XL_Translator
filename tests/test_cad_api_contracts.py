from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

import settings as app_settings
from api.app import create_app
from api.task_manager import TaskOptions, TranslationTaskManager
from settings import AppSettings


def _dxf_text() -> str:
    return "0\nSECTION\n2\nENTITIES\n0\nTEXT\n1\nDoor\n0\nENDSEC\n0\nEOF\n"


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
