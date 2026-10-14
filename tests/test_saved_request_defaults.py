"""What a caller without the window gets: saved defaults and confirmable stops.

The command line and MCP send ``apply_saved_defaults``; the engine then fills
every start/scan field they left out with what the page's workspace would
have sent (``buildPayload`` / ``scanPayload`` in ui/src/views/workspace.ts).
These checks pin each built-in page's hook to those window behaviours, so
the two sides cannot drift apart silently, and pin the ``accept`` field on
the three stops the window lets the user step past.
"""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.schemas import ScanRequest
from api.surfaces import all_surfaces, get_surface, unregister_surface
from api.task_contracts import REASON_NEEDS_CONFIRMATION, TaskInputError, TaskOptions
from api.task_manager import TranslationTaskManager
from settings import AppSettings
from tests.app_data_isolation import IsolatedAppDataTestCase
from tests.fixtures.sample_surface import SAMPLE_KEY, register_sample_surface

OPTION_FIELDS = {field.name for field in fields(TaskOptions)}


def _hook(surface: str, settings: AppSettings | None = None, target_lang: str = "fr") -> dict:
    return get_surface(surface).saved_request_defaults(settings or AppSettings(), target_lang=target_lang)


# ── the window behaviours ────────────────────────────────────────────────


def test_word_protects_cover_and_contents_by_default():
    # WORD_TOGGLES protectFrontMatter: not saved, on every time the page opens.
    assert _hook("word") == {"protect_front_matter": True}


@pytest.mark.parametrize("saved", [True, False])
def test_pdf_images_follow_the_saved_switch(saved: bool):
    settings = AppSettings()
    settings.pdf.include_images = saved

    assert _hook("pdf", settings) == {"include_images": saved}
    scan = get_surface("pdf").saved_scan_defaults(settings, source_lang="auto", target_lang="fr")
    assert scan == {"include_images": saved}


def test_cad_sends_its_whole_saved_output_group():
    settings = AppSettings()
    cad = settings.cad_output
    cad.translate_output_filename = True
    cad.include_block_text = False
    cad.keep_work_dxf = True
    cad.copy_related_files = True
    cad.fit_layout = False
    cad.fit_default_scale, cad.fit_min_scale = 0.6, 0.3
    cad.fit_default_scale_zh, cad.fit_min_scale_zh = 0.9, 0.5
    cad.display_mode = "source_first"
    cad.translation_font = "original"
    cad.output_version = "2013"
    cad.locked_blocks = "TITLE*, 图框"
    cad.locked_layers = "DIM"

    values = _hook("cad", settings, target_lang="fr")

    assert values == {
        # Text already in the target language is never sent (buildPayload).
        "untranslated_only": True,
        "cad_translate_output_filename": True,
        "cad_include_block_text": False,
        "cad_keep_work_dxf": True,
        "cad_copy_related_files": True,
        "cad_fit_layout": False,
        "cad_fit_default_scale": 0.6,
        "cad_fit_min_scale": 0.3,
        "cad_display_mode": "source_first",
        "cad_translation_font": "original",
        "cad_output_version": "2013",
        "cad_locked_blocks": "TITLE*, 图框",
        "cad_locked_layers": "DIM",
    }
    # The scale pair follows the target language (cadFitScales).
    chinese = _hook("cad", settings, target_lang="zh")
    assert (chinese["cad_fit_default_scale"], chinese["cad_fit_min_scale"]) == (0.9, 0.5)


def test_cad_custom_output_folder_only_when_switched_on():
    settings = AppSettings()
    settings.cad_output.custom_output_dir = "/tmp/cad-out"
    assert "cad_output_dir" not in _hook("cad", settings)
    settings.cad_output.use_custom_output_dir = True
    assert _hook("cad", settings)["cad_output_dir"] == "/tmp/cad-out"


def test_cad_scan_counts_with_the_language_pair_and_locks():
    settings = AppSettings()
    settings.cad_output.include_block_text = False
    settings.cad_output.locked_layers = "DIM"

    scan = get_surface("cad").saved_scan_defaults(settings, source_lang="auto", target_lang="fr")

    assert scan == {
        "source_language": "auto",
        "target_language": "fr",
        "include_block_text": False,
        "locked_blocks": "",
        "locked_layers": "DIM",
    }


def test_excel_sends_nothing_beyond_the_request_defaults():
    assert _hook("excel") == {}


@pytest.mark.parametrize("surface", all_surfaces(), ids=lambda surface: surface.key)
def test_every_default_names_a_real_field(surface):
    # A misspelt key would be dropped by the route without a sound.
    assert set(surface.saved_request_defaults(AppSettings(), target_lang="fr")) <= OPTION_FIELDS
    if surface.requires_source:
        scan = surface.saved_scan_defaults(AppSettings(), source_lang="auto", target_lang="fr")
        assert set(scan) <= set(ScanRequest.model_fields)


def test_a_run_time_page_gets_its_descriptor_switch_defaults():
    register_sample_surface()
    try:
        assert _hook(SAMPLE_KEY) == {"options": {"uppercase": False}}
    finally:
        unregister_surface(SAMPLE_KEY)


# ── the routes ───────────────────────────────────────────────────────────


class _Manager:
    """Records what the routes hand the task manager."""

    def __init__(self, saved: dict, scan_saved: dict | None = None) -> None:
        self.saved = saved
        self.scan_saved = scan_saved or {}
        self.options: TaskOptions | None = None
        self.calls: list[tuple] = []

    def saved_request_defaults(self, surface, *, target_lang=None):
        self.calls.append(("request", surface, target_lang))
        return dict(self.saved)

    def saved_scan_defaults(self, surface, *, source_lang=None, target_lang=None):
        self.calls.append(("scan", surface, source_lang, target_lang))
        return dict(self.scan_saved)

    def preflight_task(self, *, surface, source_path, selected_paths, options):
        self.options = options
        return {"requires_confirmation": False, "risk": {}, "candidate_snapshot": {}}


def _preflight(manager: _Manager, body: dict) -> dict:
    client = TestClient(create_app(task_manager=manager))
    response = client.post("/api/tasks/preflight", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_unset_fields_take_the_saved_value_and_sent_ones_win():
    manager = _Manager({"protect_front_matter": True, "translate_headers_footers": True})

    body = _preflight(
        manager,
        {
            "surface": "word",
            "source_path": "/tmp/a.docx",
            "target_lang": "en",
            "translate_headers_footers": False,
            "apply_saved_defaults": True,
        },
    )

    assert manager.options is not None
    assert manager.options.protect_front_matter is True
    assert manager.options.translate_headers_footers is False
    assert manager.calls == [("request", "word", "en")]
    # The caller can report the options the task will actually run with.
    assert body["options"]["protect_front_matter"] is True
    assert body["options"]["translate_headers_footers"] is False


def test_the_window_keeps_getting_exactly_what_it_sends():
    manager = _Manager({"protect_front_matter": True})

    body = _preflight(manager, {"surface": "word", "source_path": "/tmp/a.docx"})

    assert manager.options is not None
    assert manager.options.protect_front_matter is False
    assert manager.calls == []
    assert "options" not in body


def test_run_time_switches_merge_key_by_key():
    register_sample_surface()
    try:
        manager = _Manager({"options": {"uppercase": False, "other": True}})
        _preflight(
            manager,
            {
                "surface": SAMPLE_KEY,
                "source_path": "/tmp/notes",
                "options": {"uppercase": True},
                "apply_saved_defaults": True,
            },
        )
    finally:
        unregister_surface(SAMPLE_KEY)
    assert manager.options is not None
    assert manager.options.options == {"uppercase": True, "other": True}


def test_scan_fills_unset_fields_too(tmp_path: Path):
    manager = _Manager({}, scan_saved={"include_images": True})
    captured: dict = {}

    def scan_sources(self, root, request):
        captured["include_images"] = request.include_images
        return SimpleNamespace(items=[], skipped=[], summary={}, risk={})

    client = TestClient(create_app(task_manager=manager))
    with patch.object(type(get_surface("pdf")), "scan_sources", scan_sources):
        response = client.post(
            "/api/sources/scan",
            json={"surface": "pdf", "path": str(tmp_path), "apply_saved_defaults": True},
        )
        assert response.status_code == 200, response.text
        assert captured["include_images"] is True
        explicit = client.post(
            "/api/sources/scan",
            json={"surface": "pdf", "path": str(tmp_path), "include_images": False, "apply_saved_defaults": True},
        )
        assert explicit.status_code == 200, explicit.text
        assert captured["include_images"] is False


class TaskManagerDefaultsTests(IsolatedAppDataTestCase):
    def test_the_target_language_is_the_one_the_task_would_use(self) -> None:
        settings = AppSettings()
        settings.cad_output.fit_default_scale = 0.6
        settings.cad_output.fit_default_scale_zh = 0.9
        settings.target_lang = "zh"
        manager = TranslationTaskManager(settings_loader=lambda: settings)

        # CAD reads the shared target language when the request names none.
        self.assertEqual(manager.saved_request_defaults("cad")["cad_fit_default_scale"], 0.9)
        self.assertEqual(manager.saved_request_defaults("cad", target_lang="fr")["cad_fit_default_scale"], 0.6)
        scan = manager.saved_scan_defaults("cad")
        self.assertEqual(scan["target_language"], "zh")


# ── confirmable stops ────────────────────────────────────────────────────


def _xls(path: Path) -> SimpleNamespace:
    return SimpleNamespace(path=path, format="xls")


def test_xls_without_excel_can_be_confirmed(tmp_path: Path):
    settings = AppSettings()
    with (
        patch("api.surfaces.excel.require_translation_model"),
        patch("api.surfaces.excel.get_local_excel_availability", return_value=(False, "未安装")),
        patch("api.surfaces.excel.libreoffice_xls_conversion_available", return_value=False),
    ):
        with pytest.raises(TaskInputError) as stopped:
            get_surface("excel").validate_preflight(
                files=[_xls(tmp_path / "a.xls")], settings=settings, options=TaskOptions()
            )
        get_surface("excel").validate_preflight(
            files=[_xls(tmp_path / "a.xls")], settings=settings, options=TaskOptions(allow_xls_fallback=True)
        )
    assert stopped.value.reason == REASON_NEEDS_CONFIRMATION
    assert stopped.value.accept == {"allow_xls_fallback": True}
    assert ".xls" in str(stopped.value)


def test_doc_without_word_can_be_confirmed(tmp_path: Path):
    settings = AppSettings()
    with (
        patch("api.surfaces.word.require_translation_model"),
        patch("api.surfaces.word.get_local_word_automation_availability", return_value=(False, "未安装")),
    ):
        with pytest.raises(TaskInputError) as stopped:
            get_surface("word").validate_preflight(
                files=[SimpleNamespace(path=tmp_path / "a.doc", format="doc")],
                settings=settings,
                options=TaskOptions(),
            )
    assert stopped.value.reason == REASON_NEEDS_CONFIRMATION
    assert stopped.value.accept == {"allow_doc_fallback": True}


def test_the_error_body_carries_accept():
    manager = MagicMock()
    manager.preflight_task.side_effect = TaskInputError(
        "需要确认。", reason="pdf_review_model_unavailable", accept={"allow_known_review_failure": True}
    )
    client = TestClient(create_app(task_manager=manager))

    response = client.post("/api/tasks/preflight", json={"surface": "pdf", "source_path": "/tmp/a.pdf"})

    assert response.status_code == 422
    assert response.json() == {
        "detail": "需要确认。",
        # The window keys its dialog on this reason; it stays as it was.
        "reason": "pdf_review_model_unavailable",
        "accept": {"allow_known_review_failure": True},
    }


def test_plain_input_errors_carry_no_accept():
    manager = MagicMock()
    manager.preflight_task.side_effect = TaskInputError("未找到可翻译的文件。")
    client = TestClient(create_app(task_manager=manager))

    response = client.post("/api/tasks/preflight", json={"surface": "excel", "source_path": "/tmp/x"})

    assert response.status_code == 422
    assert "accept" not in response.json()
