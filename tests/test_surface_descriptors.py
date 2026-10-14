"""Type descriptors (``GET /api/surfaces``): complete, and worded like the UI.

The descriptor is how a page the front end does not know at build time gets
its name, file noun and switches.  The four built-in pages are still drawn
from the front end's own tables, so their descriptors must say the same thing
those tables say — otherwise a CLI or agent reading the descriptor would name
a page differently from the sidebar.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.surfaces import SURFACE_KEYS, Surface, all_surfaces
from tests.app_data_isolation import IsolatedAppDataTestCase

ROOT = Path(__file__).resolve().parents[1]
UI_SRC = ROOT / "ui" / "src"
LANGUAGES = ("zh", "en", "fr")


def _catalog() -> dict[str, list[dict[str, str]]]:
    """Every en/fr rendering of each Chinese key across the UI catalogs."""
    merged: dict[str, list[dict[str, str]]] = {}
    for path in sorted((UI_SRC / "i18n" / "catalog").glob("*.json")):
        for key, value in json.loads(path.read_text(encoding="utf-8")).items():
            if isinstance(value, dict):
                merged.setdefault(key, []).append(value)
    return merged


def _assert_matches_catalog(text: dict[str, str], what: str) -> None:
    variants = _catalog().get(text["zh"])
    assert variants, f"{what}: 「{text['zh']}」 is not in the UI catalog"
    assert any(
        variant.get("en") == text["en"] and variant.get("fr") == text["fr"] for variant in variants
    ), f"{what}: {text} differs from the UI catalog {variants}"


def _assert_localized(text: object, what: str) -> None:
    assert isinstance(text, dict), what
    for language in LANGUAGES:
        assert str(text.get(language) or "").strip(), f"{what}: no {language} text"


class SurfaceRouteTests(IsolatedAppDataTestCase):
    def test_route_lists_every_registered_surface_in_registration_order(self) -> None:
        with TestClient(create_app()) as client:
            response = client.get("/api/surfaces")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual([item["key"] for item in payload["surfaces"]], list(SURFACE_KEYS))
        self.assertEqual(payload["surfaces"], [surface.describe() for surface in all_surfaces()])


@pytest.mark.parametrize("surface", all_surfaces(), ids=lambda surface: surface.key)
def test_descriptor_is_complete(surface: Surface):
    descriptor = surface.describe()
    assert descriptor["key"] == surface.key
    assert descriptor["builtin"] is True
    _assert_localized(descriptor["label"], f"{surface.key} label")
    assert descriptor["icon"], f"{surface.key}: no icon"
    capabilities = descriptor["capabilities"]
    for name in ("requires_source", "can_pause", "can_resume", "can_end_paused", "bilingual", "translation_only"):
        assert isinstance(capabilities[name], bool), f"{surface.key}: capabilities.{name}"
    assert capabilities["can_pause"] == surface.can_pause
    if surface.requires_source:
        _assert_localized(descriptor["file_noun"], f"{surface.key} file_noun")
        assert descriptor["extensions"], f"{surface.key}: no file extensions"
        assert all(re.fullmatch(r"[a-z0-9]+", ext) for ext in descriptor["extensions"])
    # One name per progress share; a runner that reports an overall
    # percentage (CAD) has neither.
    assert len(descriptor["phases"]) == len(surface.phase_weights)
    for phase in descriptor["phases"]:
        _assert_localized(phase, f"{surface.key} phase")
    # Built-in pages draw their switches from the front end's own tables.
    assert descriptor["options"] == []
    json.dumps(descriptor)


@pytest.mark.parametrize("surface", all_surfaces(), ids=lambda surface: surface.key)
def test_builtin_descriptor_text_matches_the_interface(surface: Surface):
    descriptor = surface.describe()
    _assert_matches_catalog(descriptor["label"], f"{surface.key} label")
    for phase in descriptor["phases"]:
        _assert_matches_catalog(phase, f"{surface.key} phase")
    icons = (UI_SRC / "icons.ts").read_text(encoding="utf-8")
    assert f'"{descriptor["icon"]}",' in icons[: icons.index("] as const")], descriptor["icon"]
    if not surface.requires_source:
        return
    _assert_matches_catalog(descriptor["file_noun"], f"{surface.key} file_noun")
    shell = (UI_SRC / "shell.ts").read_text(encoding="utf-8")
    sidebar = shell[shell.index("const TRANSLATE_ITEMS"):shell.index("const RESOURCE_ITEMS")]
    assert (
        f'{{ id: "{surface.key}", label: t("{descriptor["label"]["zh"]}"), icon: "{descriptor["icon"]}" }}'
        in sidebar
    ), f"{surface.key}: sidebar entry differs from the descriptor"
    workspace = (UI_SRC / "views" / "workspace.ts").read_text(encoding="utf-8")
    nouns = workspace[workspace.index("const SURFACE_FILE_NOUN"):]
    nouns = nouns[: nouns.index("\n")]
    assert f'{surface.key}: t("{descriptor["file_noun"]["zh"]}")' in nouns, (
        f"{surface.key}: workspace file noun differs from the descriptor"
    )
