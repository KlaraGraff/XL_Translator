"""What a format adapter's page gets beyond the protocol: sidebar defaults, the
output location and 补充要求 of its own page, a bundle that leaves its local
paths behind, the switches and target language handed to the adapter, and the
review entries ``write`` reports back.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from api.surfaces.adapter import AdapterSurface
from api.surfaces.base import RunnerRequest
from api.task_contracts import TaskInputError, TaskOptions
from core.document_config import build_document_config_export_payload
from core import tm_manager
from core.adapter_subprocess_host import SubprocessAdapterHost
from core.format_adapter import AdapterManifestError, InProcessAdapterHost, load_manifest, parse_manifest
from core.task_runner_messages import DoneMsg
from settings import AppSettings, ExtensionSettings
from tests.app_data_isolation import IsolatedAppDataTestCase
from tests.test_adapter_runner import FakeEngine, _drain, _item
from tests.test_format_adapter import ECHO_DIR, _manifest
from translator_adapter import OutputFile, ReviewItem

NOTES_DIR = Path(__file__).resolve().parent / "fixtures" / "mark_adapters" / "notes"


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


def _runner_request(settings: AppSettings, source: Path, files: list | None = None) -> RunnerRequest:
    return RunnerRequest(
        files=list(files or []),
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



# ── what the adapter gets and reports ───────────────────────────────────────


def test_target_lang_is_kept_for_the_app():
    with pytest.raises(AdapterManifestError, match="target_lang"):
        parse_manifest(_manifest(options=[{"key": "target_lang", "label": {"zh": "目标", "en": "Target", "fr": "Cible"}}]))


def test_review_items_survive_the_json_round_trip():
    output = OutputFile(path="a", review_items=[ReviewItem("原因", id="u1"), {"reason": "其他", "context": "第 1 页"}])
    again = OutputFile.from_dict(output.to_dict())
    assert again.review_items == [ReviewItem("原因", id="u1"), ReviewItem("其他", context="第 1 页")]
    assert OutputFile.from_dict({"path": "a"}).review_items == []
    assert OutputFile.from_dict({"path": "a", "review_items": [{"reason": " "}]}).review_items == []


@pytest.mark.parametrize("host_class", [InProcessAdapterHost, SubprocessAdapterHost])
def test_scan_and_extract_get_the_switches_when_they_take_them(tmp_path: Path, host_class):
    source = tmp_path / "a.nt"
    source.write_text("Title\nNOTE: speaker\n", encoding="utf-8")
    host = host_class(NOTES_DIR)
    off = {"translate_notes": False, "target_lang": "fr"}
    assert [unit.locked for unit in host.extract(source, off)] == [False, True]
    assert host.scan(source, off).unit_count == 1
    # Without switches (an older caller), notes are taken as usual.
    assert [unit.locked for unit in host.extract(source)] == [False, False]
    # An adapter whose extract takes only the path keeps working when switches are given.
    echo = host_class(ECHO_DIR)
    echo_source = tmp_path / "b.txt"
    echo_source.write_text("one\n", encoding="utf-8")
    assert [unit.text for unit in echo.extract(echo_source, off)] == ["one"]
    assert echo.scan(echo_source, off).unit_count == 1


class AdapterReportTests(IsolatedAppDataTestCase):
    def setUp(self) -> None:
        super().setUp()
        tm_manager.init_db()
        self.source = self.app_data_dir.parent / "docs"
        self.source.mkdir(parents=True)
        manifest = load_manifest(NOTES_DIR)
        self.surface = AdapterSurface(manifest, InProcessAdapterHost(NOTES_DIR, manifest))

    def _run(self, engine: FakeEngine, body: str, *, notes: bool) -> tuple[DoneMsg, str]:
        path = self.source / "a.nt"
        path.write_text(body, encoding="utf-8")
        settings = AppSettings(target_lang="fr")
        settings.extensions["notes"] = ExtensionSettings(smart_context=False, options={"translate_notes": notes})
        with patch("core.engine_dispatcher.build_role_engine", return_value=engine):
            runner = self.surface.build_runner(_runner_request(settings, self.source, [_item(path)]))
            runner.start()
            messages = _drain(runner)
        done = messages[-1]
        assert isinstance(done, DoneMsg), done
        return done, Path(done.file_results[0]["output_path"]).read_text(encoding="utf-8")

    def test_switched_off_notes_are_never_sent_and_write_knows_the_target(self) -> None:
        engine = FakeEngine()
        _done, output = self._run(engine, "Title\nNOTE: speaker\n", notes=False)
        assert engine.sent == ["Title"]
        assert output == "lang=fr\n[fr] Title\nNOTE: speaker\n"

    def test_what_write_asks_to_review_is_listed_with_its_place_and_source(self) -> None:
        done, _output = self._run(FakeEngine(), "Title\nVERTICAL box\n", notes=True)
        items = [(item["location"], item["excerpt"], item["issue"]) for item in done.review["items"]]
        assert items == [
            ("line 2", "VERTICAL box", "竖排文本框中放入了西文"),
            ("第 1 页 · 标题", "", "缩到 60% 仍放不下"),
        ]
        assert done.review["total_count"] == 2
        assert done.file_results[0]["review_count"] == 2
