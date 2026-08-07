"""Regressions for shadowed API routes, the auth compare and the shutdown hook.

Starlette matches routes in declaration order.  A literal path declared after
its parameterised sibling is unreachable: ``/api/tm/entries/bulk/pin`` sat
behind ``/api/tm/entries/{entry_id}/pin`` and answered 422 because the router
tried to read "bulk" as an int, which broke the library's bulk-pin button
outright.  The three ``/api/models/connectivity/<literal>`` routes had the same
shape.  These tests pin the declaration order by asserting the status codes.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import settings as settings_module
from api.app import create_app
from core import diagnostics, tm_manager


class _ManagerSpy:
    """Minimal task-manager stand-in that records the shutdown call."""

    def __init__(self) -> None:
        self.shutdown_calls = 0

    def shutdown(self) -> None:
        self.shutdown_calls += 1


class RouteOrderingTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        app_data = self.root / "app-data"
        for patcher in (
            patch.multiple(
                settings_module,
                APP_DATA_DIR=app_data,
                SETTINGS_PATH=app_data / "settings.json",
                KEYS_PATH=app_data / "keys.json",
            ),
            patch.object(tm_manager, "DB_PATH", app_data / "tm.db"),
            patch.object(diagnostics, "DIAGNOSTIC_RECORDS_DIR", self.root / "diagnostics"),
            patch.object(diagnostics, "LOG_PATH", app_data / "app.log"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(create_app())

    def test_tm_bulk_routes_are_not_shadowed_by_the_entry_id_route(self) -> None:
        pinned = self.client.post(
            "/api/tm/entries/bulk/pin", json={"ids": [1, 2], "pinned": True}
        )
        self.assertEqual(pinned.status_code, 200)
        self.assertEqual(pinned.json(), {"count": 2})

        deleted = self.client.post("/api/tm/entries/bulk/delete", json={"ids": [1, 2]})
        self.assertEqual(deleted.status_code, 200)
        self.assertIn("deleted", deleted.json())

    def test_single_entry_pin_still_resolves_after_the_reorder(self) -> None:
        single = self.client.post("/api/tm/entries/7/pin", json={"pinned": True})
        self.assertEqual(single.status_code, 200)
        self.assertEqual(single.json(), {"changed": True})

    def test_non_numeric_entry_id_is_still_rejected(self) -> None:
        """Moving the literals must not turn the int path parameter into a str."""
        response = self.client.post("/api/tm/entries/not-a-number/pin", json={"pinned": True})
        self.assertEqual(response.status_code, 422)

    def test_literal_connectivity_routes_reach_their_own_handlers(self) -> None:
        """The literal handlers skip the role resolution that ``/{role}`` runs.

        That difference is what makes the routing observable: with role
        resolution broken, only a request that really lands on the literal
        handler can still answer 200.
        """
        checks = {
            "text": "check_connectivity",
            "image": "check_image_generation_connectivity",
            "pdf-review": "check_pdf_review_connectivity",
        }
        for path_segment, function_name in checks.items():
            with self.subTest(role=path_segment):
                with (
                    patch(
                        f"api.app.{function_name}",
                        return_value={"ok": True, "which": path_segment},
                    ) as checker,
                    patch(
                        "api.app.resolve_effective_model_config",
                        side_effect=AssertionError("role resolution belongs to /{role}"),
                    ),
                ):
                    response = self.client.post(f"/api/models/connectivity/{path_segment}")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["which"], path_segment)
                self.assertEqual(checker.call_count, 1)

    def test_parameterised_connectivity_route_still_serves_other_roles(self) -> None:
        with patch("api.app.check_connectivity", return_value={"ok": True}):
            response = self.client.post("/api/models/connectivity/cleaner")
        self.assertEqual(response.status_code, 200)
        unknown = self.client.post("/api/models/connectivity/nonsense")
        self.assertEqual(unknown.status_code, 404)


class LoopbackTokenTests(unittest.TestCase):
    """The token compare must not leak how much of a guess was right."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        app_data = Path(temporary.name) / "app-data"
        for patcher in (
            patch.multiple(
                settings_module,
                APP_DATA_DIR=app_data,
                SETTINGS_PATH=app_data / "settings.json",
                KEYS_PATH=app_data / "keys.json",
            ),
            patch.object(tm_manager, "DB_PATH", app_data / "tm.db"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.token = "s3cret-loopback-token"
        self.client = TestClient(create_app(auth_token=self.token))

    def test_correct_token_is_accepted(self) -> None:
        response = self.client.get("/api/languages", headers={"X-Translator-Token": self.token})
        self.assertEqual(response.status_code, 200)

    def test_missing_and_wrong_tokens_are_rejected(self) -> None:
        self.assertEqual(self.client.get("/api/languages").status_code, 401)
        for guess in (
            "",
            "wrong",
            # A shared prefix must be no more revealing than a random string.
            self.token[:-1],
            self.token + "x",
        ):
            with self.subTest(guess=guess):
                response = self.client.get(
                    "/api/languages", headers={"X-Translator-Token": guess}
                )
                self.assertEqual(response.status_code, 401)

    def test_non_ascii_token_header_is_rejected_without_raising(self) -> None:
        """``compare_digest`` refuses non-ASCII ``str``; the bytes form must not."""
        response = self.client.get(
            # Raw bytes: a header value only ever arrives as bytes, and the
            # comparison must handle one that is not ASCII.
            "/api/languages",
            headers={"X-Translator-Token": "令牌".encode("utf-8")},
        )
        self.assertEqual(response.status_code, 401)


class ShutdownHookTests(unittest.TestCase):
    def test_app_shutdown_winds_the_task_manager_down(self) -> None:
        """Nothing used to call ``shutdown``; the lifespan hook now does."""
        manager = _ManagerSpy()
        app = create_app(task_manager=manager)
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/languages").status_code, 200)
            self.assertEqual(manager.shutdown_calls, 0)
        self.assertEqual(manager.shutdown_calls, 1)


if __name__ == "__main__":
    unittest.main()
