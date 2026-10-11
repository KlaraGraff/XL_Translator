"""The backend surface registry and the front end must name the same pages.

A new translation page touches both sides (docs/ADDING_A_TRANSLATION_PAGE.md).
These checks fail with the missing key named, so a half-registered page is
caught by `pytest` instead of by a user clicking 暂停 on a page the backend
does not know can pause.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from api.surfaces import DOCUMENT_SURFACE_KEYS, SURFACE_KEYS, Surface, all_surfaces

ROOT = Path(__file__).resolve().parents[1]
UI_SRC = ROOT / "ui" / "src"


def _string_union(source: str, declaration: str) -> set[str]:
    """String literals of a TS union that starts at ``declaration``."""
    start = source.index(declaration)
    end = source.index(";", start)
    return set(re.findall(r'"([a-z_]+)"', source[start:end]))


def test_workspace_pages_match_the_file_based_surfaces():
    source = (UI_SRC / "views" / "workspace.ts").read_text(encoding="utf-8")
    assert _string_union(source, "export type Surface =") == set(DOCUMENT_SURFACE_KEYS)


def test_api_client_task_surface_covers_every_registered_surface():
    source = (UI_SRC / "api-client.ts").read_text(encoding="utf-8")
    ui_keys = _string_union(source, "surface:")
    missing = set(SURFACE_KEYS) - ui_keys
    assert not missing, f"ui/src/api-client.ts task surface lacks {sorted(missing)}"


def test_task_center_pause_list_matches_backend_capabilities():
    source = (UI_SRC / "views" / "tasks.ts").read_text(encoding="utf-8")
    match = re.search(r"PAUSABLE_SURFACES = new Set<TaskSurface>\(\[([^\]]*)\]\)", source)
    assert match, "PAUSABLE_SURFACES not found in ui/src/views/tasks.ts"
    ui_pausable = set(re.findall(r'"([a-z_]+)"', match.group(1)))
    backend_pausable = {surface.key for surface in all_surfaces() if surface.can_pause}
    assert ui_pausable == backend_pausable


@pytest.mark.parametrize("surface", all_surfaces(), ids=lambda surface: surface.key)
def test_surface_declaration_is_complete(surface: Surface):
    for name in ("key", "label", "page_key"):
        assert str(getattr(surface, name, "") or "").strip(), f"{surface.key}: {name} is empty"
    assert surface.key == surface.key.strip().lower()

    cls = type(surface)
    # TM cleaning starts from a language pair through its own task-manager
    # path; every file-based page goes through these hooks.
    if surface.requires_source:
        for hook in ("scan", "validate_preflight", "build_runner"):
            assert getattr(cls, hook) is not getattr(Surface, hook), f"{surface.key}: {hook} not overridden"
        assert (
            cls.scan_sources is not Surface.scan_sources
            or cls.scan_response is not Surface.scan_response
        ), f"{surface.key}: neither scan_sources nor scan_response overridden"

    if surface.can_resume or surface.can_end_paused or surface.pause_mode != "immediate":
        assert surface.can_pause, f"{surface.key}: pause options declared without can_pause"

    if surface.phase_weights:
        assert all(weight >= 0 for weight in surface.phase_weights)
        assert sum(surface.phase_weights) > 0


def test_every_task_option_comes_from_a_same_named_request_field():
    from dataclasses import MISSING, fields

    from api.schemas import TaskStartRequest
    from api.task_contracts import TaskOptions

    request_fields = TaskStartRequest.model_fields
    for option in fields(TaskOptions):
        assert option.name in request_fields, f"TaskStartRequest lacks {option.name}"
        assert option.default is not MISSING, f"TaskOptions.{option.name} needs a default"


@pytest.mark.parametrize("surface", all_surfaces(), ids=lambda surface: surface.key)
def test_surface_page_key_occupies_at_least_one_model_role(surface: Surface):
    # An unknown page key maps to no roles: the task would start without
    # reserving or freezing any model connection.
    from core.model_api_identity import task_model_roles_for_page
    from settings import AppSettings

    assert task_model_roles_for_page(AppSettings(), surface.page_key), (
        f"{surface.key}: add page_key {surface.page_key!r} to "
        "core/model_api_identity.task_model_roles_for_page"
    )
