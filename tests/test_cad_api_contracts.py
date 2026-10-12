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
    with patch("api.surfaces.cad.probe_status", return_value=status):
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
    with patch("api.routes.cad.probe_status", return_value=status):
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
        patch("api.surfaces.cad.probe_status", return_value={"converter": ""}),
        patch("api.surfaces.cad.build_role_engine", return_value=object()),
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
    # 翻译参数 → CAD → 失败重试次数（默认 2）跟着请求走。
    assert captured["transient_retries"] == settings.request_retry.cad


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
        with patch("api.routes.domains.build_role_engine", return_value=FakeEngine()):
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
    with patch("api.surfaces.cad.probe_status", return_value={"converter": ""}):
        files = manager._scan(
            tmp_path,
            "cad",
            TaskOptions(cad_output_dir=str(output)),
        )
    assert [item.path for item in files] == [source]


def _framed_dxf_text() -> str:
    pairs = (
        (0, "SECTION"), (2, "BLOCKS"),
        (0, "BLOCK"), (8, "0"), (2, "TK-A1"), (70, "0"),
        (0, "TEXT"), (8, "0"), (1, "设计单位"),
        (0, "ENDBLK"),
        (0, "ENDSEC"),
        (0, "SECTION"), (2, "ENTITIES"),
        (0, "INSERT"), (8, "图框"), (2, "TK-A1"),
        (0, "TEXT"), (8, "说明"), (1, "设备基础"),
        (0, "ENDSEC"), (0, "EOF"),
    )
    return "".join(f"{code}\n{value}\n" for code, value in pairs)


def test_cad_inspect_returns_counts_blocks_and_layers_and_honours_locks(tmp_path, monkeypatch):
    monkeypatch.setattr(app_settings, "APP_DATA_DIR", tmp_path / "app-data")
    source = tmp_path / "framed.dxf"
    source.write_text(_framed_dxf_text(), encoding="utf-8")
    status = {"plugin": "enabled", "oda": "connected", "converter": None, "enabled": True}
    with patch("api.routes.cad.probe_status", return_value=status):
        with TestClient(create_app()) as client:
            plain = client.post("/api/cad/inspect", json={"path": str(source), "target_language": "fr"})
            locked = client.post(
                "/api/cad/inspect",
                json={"path": str(source), "target_language": "fr", "locked_blocks": "tk*"},
            )
            scanned = client.post(
                "/api/cad/scan",
                json={"paths": [str(source)], "target_language": "fr", "locked_layers": "说明"},
            )

    assert plain.status_code == 200, plain.text
    body = plain.json()
    assert (body["text_count"], body["converted"]) == (2, False)
    assert body["blocks"] == [{"name": "TK-A1", "texts": 1, "inserts": 1}]
    assert {layer["name"] for layer in body["layers"]} == {"0", "说明"}
    assert locked.json()["text_count"] == 1
    assert scanned.json()["items"][0]["text_count"] == 1


def test_cad_task_options_reach_the_pipeline(tmp_path, monkeypatch):
    monkeypatch.setattr(app_settings, "APP_DATA_DIR", tmp_path / "app-data")
    settings = AppSettings()
    manager = TranslationTaskManager(settings_loader=lambda: settings)
    source = tmp_path / "sample.dxf"
    source.write_text(_dxf_text(), encoding="utf-8")
    with (
        patch("api.surfaces.cad.probe_status", return_value={"converter": ""}),
        patch("api.surfaces.cad.build_role_engine", return_value=object()),
    ):
        runner = manager._build_runner(
            surface="cad",
            files=[SimpleNamespace(path=source, name=source.name, format="dxf")],
            settings=settings,
            source_root=tmp_path,
            options=TaskOptions(
                cad_fit_default_scale=0.8,
                cad_fit_min_scale=0.9,
                cad_display_mode="source_first",
                cad_locked_blocks="tk*, 签名栏",
                cad_locked_layers="",
                cad_translation_font="bogus",
            ),
            source_lang="auto",
            key_overrides={},
        )

    options = runner._options
    assert (options.fit_default_scale, options.fit_min_scale) == (0.8, 0.8)
    assert options.display_mode == "source_first"
    assert options.locked_blocks == ("TK*", "签名栏") and options.locked_layers == ()
    assert options.translation_font == "compatible"
    assert runner._dxf_cache.root == tmp_path / "app-data" / "cache" / "cad-dxf-v1"
