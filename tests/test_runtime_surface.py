"""A page registered at run time goes through the same routes as a built-in one.

``tests/fixtures/sample_surface.py`` registers ``ext:sample``: no compiled
table on either side, only its descriptor.  These checks walk it through
the descriptor list, scan, start, the switch values, the per-type target
language and the task record, and pin what registration refuses so a
run-time page can never take a built-in page's place.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.surfaces import SURFACE_KEYS, all_surfaces, get_surface, register_surface, unregister_surface
from api.surfaces.excel import ExcelSurface
from api.task_contracts import TaskInputError
from core.surface_settings import set_surface_setting
from tests.app_data_isolation import IsolatedAppDataTestCase
from tests.fixtures.sample_surface import SAMPLE_KEY, SampleSurface, register_sample_surface


def _wait(client: TestClient, task_id: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = client.get(f"/api/tasks/{task_id}").json()
        if status.get("terminal"):
            return status
        time.sleep(0.05)
    raise TimeoutError(task_id)


class RuntimeSurfaceRouteTests(IsolatedAppDataTestCase):
    def setUp(self) -> None:
        super().setUp()
        register_sample_surface()
        self.addCleanup(unregister_surface, SAMPLE_KEY)
        self.source = self.app_data_dir.parent / "notes"
        self.source.mkdir(parents=True)
        (self.source / "a.txt").write_text("hello", encoding="utf-8")
        (self.source / "b.txt").write_text("world", encoding="utf-8")
        (self.source / "skip.md").write_text("not mine", encoding="utf-8")

    def test_descriptor_lists_the_page_after_the_built_in_ones(self) -> None:
        with TestClient(create_app()) as client:
            surfaces = client.get("/api/surfaces").json()["surfaces"]
        assert [item["key"] for item in surfaces] == [*SURFACE_KEYS, SAMPLE_KEY]
        sample = surfaces[-1]
        assert sample["builtin"] is False
        assert sample["extensions"] == ["txt"]
        assert sample["label"]["zh"] == "示例文本"
        assert [option["key"] for option in sample["options"]] == ["uppercase"]
        assert len(sample["phases"]) == len(SampleSurface.phase_weights)

    def test_scan_start_and_finish_with_the_switch_values(self) -> None:
        with TestClient(create_app()) as client:
            scanned = client.post("/api/sources/scan", json={"surface": SAMPLE_KEY, "path": str(self.source)})
            assert scanned.status_code == 200, scanned.text
            payload = scanned.json()
            assert [item["name"] for item in payload["items"]] == ["a.txt", "b.txt"]
            assert all(item["file_id"] for item in payload["items"])
            assert payload["previous_output"] is None

            started = client.post(
                "/api/tasks",
                json={
                    "surface": SAMPLE_KEY,
                    "source_path": str(self.source),
                    "target_lang": "fr",
                    "options": {"uppercase": True},
                },
            )
            assert started.status_code == 202, started.text
            status = _wait(client, started.json()["task_id"])

        assert status["state"] == "done", status
        assert status["surface"] == SAMPLE_KEY
        results = status["result"]["file_results"]
        assert [Path(item["output_path"]).name for item in results] == ["a_fr.txt", "b_fr.txt"]
        assert Path(results[0]["output_path"]).read_text(encoding="utf-8") == "HELLO"
        assert [entry["state"] for entry in status["file_progress"]["files"]] == ["generated", "generated"]
        # Only the translation model is reserved for a run-time page.
        assert set(status["model_snapshot"]) == {"translation"}

    def test_the_page_target_language_comes_from_its_own_settings(self) -> None:
        import settings as settings_module

        stored = settings_module.load_settings()
        set_surface_setting(stored, SAMPLE_KEY, "target_lang", "de")
        settings_module.save_settings(stored)
        with TestClient(create_app()) as client:
            started = client.post("/api/tasks", json={"surface": SAMPLE_KEY, "source_path": str(self.source)})
            assert started.status_code == 202, started.text
            status = _wait(client, started.json()["task_id"])
        assert status["task_snapshot"]["target_lang"] == "de"
        assert Path(status["result"]["file_results"][0]["output_path"]).name == "a_de.txt"
        assert Path(status["result"]["file_results"][0]["output_path"]).read_text(encoding="utf-8") == "hello"

    def test_pause_and_resume_follow_the_descriptor(self) -> None:
        register_sample_surface(delay=0.3)
        with TestClient(create_app()) as client:
            started = client.post("/api/tasks", json={"surface": SAMPLE_KEY, "source_path": str(self.source)})
            assert started.status_code == 202, started.text
            task_id = started.json()["task_id"]
            paused = client.post(f"/api/tasks/{task_id}/pause")
            assert paused.status_code == 200, paused.text
            assert paused.json()["state"] == "paused"
            time.sleep(0.8)
            # Paused before the second file: only the first one is written.
            status = client.get(f"/api/tasks/{task_id}").json()
            assert status["state"] == "paused"
            assert [entry["state"] for entry in status["file_progress"]["files"]].count("generated") <= 1
            resumed = client.post(f"/api/tasks/{task_id}/resume")
            assert resumed.status_code == 200, resumed.text
            status = _wait(client, task_id)
        assert status["state"] == "done", status
        assert len(status["result"]["file_results"]) == 2

    def test_an_unregistered_page_is_refused_like_an_unknown_one(self) -> None:
        unregister_surface(SAMPLE_KEY)
        with TestClient(create_app()) as client:
            scanned = client.post("/api/sources/scan", json={"surface": SAMPLE_KEY, "path": str(self.source)})
            started = client.post("/api/tasks", json={"surface": SAMPLE_KEY, "source_path": str(self.source)})
            listed = client.get("/api/surfaces").json()["surfaces"]
        assert scanned.status_code == 422
        assert started.status_code == 422
        assert SAMPLE_KEY not in [item["key"] for item in listed]


def test_registration_never_replaces_or_imitates_a_built_in_page():
    class Imposter(SampleSurface):
        key = "excel"

    class BuiltinFlag(SampleSurface):
        key = "ext:flagged"
        builtin = True

    class NoPrefix(SampleSurface):
        key = "sample"

    for surface in (Imposter(), BuiltinFlag(), NoPrefix(), ExcelSurface()):
        with pytest.raises(ValueError):
            register_surface(surface)
    register_sample_surface()
    try:
        with pytest.raises(ValueError):
            register_surface(SampleSurface())
        # Unregistering a built-in key is a no-op.
        unregister_surface("excel")
        assert get_surface("excel").builtin
    finally:
        unregister_surface(SAMPLE_KEY)
    with pytest.raises(TaskInputError):
        get_surface(SAMPLE_KEY)
    assert tuple(surface.key for surface in all_surfaces()) == SURFACE_KEYS


def test_request_surface_values_are_matched_exactly():
    from pydantic import ValidationError

    from api.schemas import ScanRequest, TaskStartRequest

    assert TaskStartRequest(surface="tm_clean").surface == "tm_clean"
    for surface in ("Excel", "excel ", "ext:missing", ""):
        with pytest.raises(ValidationError):
            TaskStartRequest(surface=surface, source_path="/tmp/x")
    # Scanning is only for file-based pages.
    with pytest.raises(ValidationError):
        ScanRequest(surface="tm_clean", path="/tmp/x")
