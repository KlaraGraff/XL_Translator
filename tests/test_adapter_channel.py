"""What a format adapter's page gets beyond the protocol: sidebar defaults, the
output location and 补充要求 of its own page, and a bundle that leaves its local
paths behind.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from api.surfaces.adapter import AdapterSurface
from api.surfaces.base import RunnerRequest
from api.task_contracts import TaskInputError, TaskOptions
from core.document_config import build_document_config_export_payload
from core.format_adapter import AdapterManifestError, InProcessAdapterHost, parse_manifest
from settings import AppSettings, ExtensionSettings
from tests.test_format_adapter import ECHO_DIR, _manifest


def _surface(origin: str = "bundled", **overrides) -> AdapterSurface:
    manifest = parse_manifest(_manifest(**overrides))
    return AdapterSurface(manifest, InProcessAdapterHost(ECHO_DIR, manifest), origin=origin)


# ── sidebar ─────────────────────────────────────────────────────────────────


def test_a_bundled_type_may_show_by_default_right_after_another_type():
    surface = _surface(visible_by_default=True, sidebar_after="word")
    descriptor = surface.describe()
    assert surface.visible_by_default is True
    assert descriptor["visible_by_default"] is True and descriptor["sidebar_after"] == "word"

    plain = _surface().describe()
    assert plain["visible_by_default"] is False and plain["sidebar_after"] == ""


def test_an_installed_extension_never_takes_the_bundled_defaults():
    surface = _surface("installed", visible_by_default=True, sidebar_after="word")
    descriptor = surface.describe()
    assert descriptor["visible_by_default"] is False and descriptor["sidebar_after"] == ""


def test_sidebar_after_must_name_a_type():
    assert parse_manifest(_manifest(sidebar_after=" Word ")).sidebar_after == "word"
    assert parse_manifest(_manifest(sidebar_after="ext:text")).sidebar_after == "ext:text"
    with pytest.raises(AdapterManifestError, match="sidebar_after"):
        parse_manifest(_manifest(sidebar_after="not a key!"))


def test_shows_follows_the_page_default_until_switched():
    table = AppSettings().translation_types
    assert table.shows("ext:pptx", visible_by_default=True) is True
    assert table.shows("ext:pptx") is False
    assert table.shows("word", visible_by_default=True) is True
    table = AppSettings.model_validate({"translation_types": {"visible": {"ext:pptx": False}}}).translation_types
    assert table.shows("ext:pptx", visible_by_default=True) is False


# ── output location ─────────────────────────────────────────────────────────


def _runner_request(settings: AppSettings, source: Path) -> RunnerRequest:
    return RunnerRequest(
        files=[],
        settings=settings,
        source_root=source,
        options=TaskOptions(options={}),
        source_lang="en",
        key_overrides={},
    )


def test_the_page_writes_to_its_own_output_location(tmp_path: Path):
    surface = _surface()
    source = tmp_path / "docs"
    source.mkdir()
    settings = AppSettings(target_lang="fr")
    runner = surface.build_runner(_runner_request(settings, source))
    assert runner._output_dir.parent == source

    custom = tmp_path / "out"
    settings.extensions["echo"] = ExtensionSettings(use_custom_output_dir=True, custom_output_dir=str(custom))
    runner = surface.build_runner(_runner_request(settings, source))
    assert runner._output_dir.parent == custom
    assert runner._output_dir.name.startswith("docs_翻译输出_")

    # Switched off: the saved folder is kept for later but not used.
    settings.extensions["echo"].use_custom_output_dir = False
    assert surface.build_runner(_runner_request(settings, source))._output_dir.parent == source


def test_an_unusable_output_location_stops_the_task_before_it_starts(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("api.surfaces.adapter.require_translation_model", lambda settings: None)
    blocker = tmp_path / "a-file"
    blocker.write_text("x", encoding="utf-8")
    settings = AppSettings(target_lang="fr")
    settings.extensions["echo"] = ExtensionSettings(use_custom_output_dir=True, custom_output_dir=str(blocker))
    with pytest.raises(TaskInputError, match="是文件而非文件夹"):
        _surface().validate_preflight(files=[object()], settings=settings, options=TaskOptions())


def test_old_settings_files_read_without_the_new_fields():
    loaded = AppSettings.model_validate({"extensions": {"echo": {"target_lang": "fr", "domain_preset": "合同"}}})
    assert loaded.extensions["echo"].use_custom_output_dir is False
    assert loaded.extensions["echo"].custom_output_dir == ""


def test_the_parameter_bundle_leaves_the_output_location_behind():
    settings = AppSettings()
    settings.extensions["echo"] = ExtensionSettings(
        domain_preset="合同", use_custom_output_dir=True, custom_output_dir="/Users/someone/译文"
    )
    exported = build_document_config_export_payload(settings)["document"]["extensions"]["echo"]
    assert exported["domain_preset"] == "合同"
    assert "custom_output_dir" not in exported and "use_custom_output_dir" not in exported

