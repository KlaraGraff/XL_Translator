"""The dev server origin is allowed only when a debug build asks for it."""

from __future__ import annotations

from api.app import _allowed_origins

PRODUCTION = ["tauri://localhost", "http://tauri.localhost"]


def test_release_builds_allow_only_the_tauri_webview(monkeypatch):
    monkeypatch.delenv("TRANSLATOR_DEV_ORIGIN", raising=False)
    assert _allowed_origins() == PRODUCTION


def test_debug_builds_add_the_vite_origin(monkeypatch):
    monkeypatch.setenv("TRANSLATOR_DEV_ORIGIN", "http://127.0.0.1:1420")
    assert _allowed_origins() == [*PRODUCTION, "http://127.0.0.1:1420"]


def test_a_non_loopback_origin_is_refused(monkeypatch):
    # The variable is not a general-purpose CORS switch.
    monkeypatch.setenv("TRANSLATOR_DEV_ORIGIN", "https://evil.example")
    assert _allowed_origins() == PRODUCTION


def test_an_empty_value_changes_nothing(monkeypatch):
    monkeypatch.setenv("TRANSLATOR_DEV_ORIGIN", "")
    assert _allowed_origins() == PRODUCTION
