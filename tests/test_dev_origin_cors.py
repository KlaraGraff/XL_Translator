"""The dev server origin is allowed only when a debug build asks for it."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch

from api.app import _allowed_origins

PRODUCTION = ["tauri://localhost", "http://tauri.localhost"]
REPO_ROOT = Path(__file__).resolve().parents[1]
TAURI_CONFIG = REPO_ROOT / "src-tauri" / "tauri.conf.json"
UI_PACKAGE = REPO_ROOT / "ui" / "package.json"


def dev_url_origin() -> str:
    """The origin the dev webview will report, taken from tauri.conf.json's devUrl."""
    dev_url = json.loads(TAURI_CONFIG.read_text(encoding="utf-8"))["build"]["devUrl"]
    parts = urlsplit(dev_url)
    return f"{parts.scheme}://{parts.netloc}"


class DevOriginCorsTests(unittest.TestCase):
    def test_release_builds_allow_only_the_tauri_webview(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TRANSLATOR_DEV_ORIGIN", None)
            self.assertEqual(_allowed_origins(), PRODUCTION)

    def test_debug_builds_add_the_vite_origin(self) -> None:
        with patch.dict(os.environ, {"TRANSLATOR_DEV_ORIGIN": "http://127.0.0.1:1420"}):
            self.assertEqual(
                _allowed_origins(), [*PRODUCTION, "http://127.0.0.1:1420"]
            )

    def test_a_non_loopback_origin_is_refused(self) -> None:
        # The variable is not a general-purpose CORS switch.
        with patch.dict(os.environ, {"TRANSLATOR_DEV_ORIGIN": "https://evil.example"}):
            self.assertEqual(_allowed_origins(), PRODUCTION)

    def test_an_empty_value_changes_nothing(self) -> None:
        with patch.dict(os.environ, {"TRANSLATOR_DEV_ORIGIN": ""}):
            self.assertEqual(_allowed_origins(), PRODUCTION)

    def test_the_configured_dev_url_is_an_origin_this_allowlist_accepts(self) -> None:
        # The Rust shell derives TRANSLATOR_DEV_ORIGIN from tauri.conf.json's devUrl,
        # so a devUrl this list refuses (a non-loopback host, a stray path) means every
        # request the dev webview makes is rejected — silently, and only in dev.
        with patch.dict(os.environ, {"TRANSLATOR_DEV_ORIGIN": dev_url_origin()}):
            self.assertIn(dev_url_origin(), _allowed_origins())

    def test_the_dev_url_points_at_the_host_and_port_vite_binds(self) -> None:
        # The webview's origin is whatever devUrl says, character for character:
        # http://localhost:1420 and http://127.0.0.1:1420 are two different origins to
        # a browser even though they reach the same socket. If devUrl and the vite
        # command drift apart, the app either fails to connect or gets its requests
        # refused by CORS, and neither failure names the config that caused it.
        dev_script = json.loads(UI_PACKAGE.read_text(encoding="utf-8"))["scripts"]["dev"]
        argv = dev_script.split()
        host = argv[argv.index("--host") + 1]
        port = argv[argv.index("--port") + 1]
        self.assertEqual(urlsplit(dev_url_origin()).netloc, f"{host}:{port}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
