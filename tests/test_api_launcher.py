from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from api.app import create_app
from api.launcher import _parent_process_is_alive, _shutdown_requester

REPO_ROOT = Path(__file__).resolve().parents[1]


class ApiLauncherTests(unittest.TestCase):
    def test_current_process_is_alive(self) -> None:
        self.assertTrue(_parent_process_is_alive(os.getpid()))

    def test_missing_process_is_not_alive(self) -> None:
        with patch("api.launcher.psutil.pid_exists", return_value=False):
            self.assertFalse(_parent_process_is_alive(987_654))

    def test_probe_error_means_process_is_alive(self) -> None:
        with patch("api.launcher.psutil.pid_exists", side_effect=OSError):
            self.assertTrue(_parent_process_is_alive(987_654))

    def test_probe_never_sends_a_signal(self) -> None:
        # A liveness probe must not reach for os.kill: on Windows any signal
        # other than CTRL_C/CTRL_BREAK terminates the target process.
        with patch("api.launcher.os.kill", side_effect=AssertionError("os.kill called")):
            self.assertTrue(_parent_process_is_alive(os.getpid()))


class ShutdownRequestTests(unittest.TestCase):
    """Windows has no SIGTERM, so the shell asks over HTTP instead (issue #1)."""

    def test_requester_stops_the_runners_before_the_server(self) -> None:
        server = SimpleNamespace(should_exit=False)
        calls: list[bool] = []

        _shutdown_requester(server, lambda: calls.append(server.should_exit))()

        # The runners are told first, while the server still serves: that is
        # what lets open SSE streams end so uvicorn can drain.
        self.assertEqual(calls, [False])
        self.assertTrue(server.should_exit)

    def test_requester_still_stops_the_server_when_the_runners_raise(self) -> None:
        server = SimpleNamespace(should_exit=False)

        def explode() -> None:
            raise RuntimeError("runner refused")

        _shutdown_requester(server, explode)()

        self.assertTrue(server.should_exit)

    def test_endpoint_runs_the_launcher_hook(self) -> None:
        app = create_app(task_manager=MagicMock(), auth_token="sidecar-token")
        hook = MagicMock()
        app.state.request_shutdown = hook
        client = TestClient(app)

        response = client.post(
            "/api/system/shutdown",
            headers={"X-Translator-Token": "sidecar-token"},
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "shutting_down")
        hook.assert_called_once_with()

    def test_endpoint_requires_the_token(self) -> None:
        app = create_app(task_manager=MagicMock(), auth_token="sidecar-token")
        hook = MagicMock()
        app.state.request_shutdown = hook

        response = TestClient(app).post("/api/system/shutdown")

        self.assertEqual(response.status_code, 401)
        hook.assert_not_called()

    def test_endpoint_without_a_launcher_says_so_instead_of_pretending(self) -> None:
        # The shell treats anything but 2xx as "not shutting down" and falls
        # back to terminating the process right away.
        app = create_app(task_manager=MagicMock())

        response = TestClient(app).post("/api/system/shutdown")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["reason"], "shutdown_unavailable")

    def test_a_real_sidecar_exits_on_its_own_after_the_request(self) -> None:
        # End to end, the way the Windows shell drives it: start the launcher,
        # read the handshake, ask it to shut down, and expect the process to
        # finish its orderly shutdown and exit 0 well inside the shell's 25s
        # budget — without any signal.
        with tempfile.TemporaryDirectory() as app_data:
            env = {
                **os.environ,
                "TRANSLATOR_APP_DATA_DIR": app_data,
                "PYTHONUNBUFFERED": "1",
            }
            env.pop("TRANSLATOR_SIDECAR_PARENT_PID", None)
            process = subprocess.Popen(
                [sys.executable, "-m", "api.launcher"],
                cwd=REPO_ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            try:
                handshake = process.stdout.readline()
                fields = dict(part.split("=", 1) for part in handshake.split())
                request = urllib.request.Request(
                    f"http://127.0.0.1:{fields['PORT']}/api/system/shutdown",
                    method="POST",
                    headers={"X-Translator-Token": fields["TOKEN"]},
                )
                with urllib.request.urlopen(request, timeout=10) as response:
                    self.assertEqual(response.status, 202)

                self.assertEqual(process.wait(timeout=20), 0)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                process.stdout.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
