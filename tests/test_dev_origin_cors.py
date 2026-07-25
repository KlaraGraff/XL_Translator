"""The dev server origin is allowed only when a debug build asks for it."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from api.app import _allowed_origins

PRODUCTION = ["tauri://localhost", "http://tauri.localhost"]


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
