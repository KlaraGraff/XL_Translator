"""A system proxy nobody listens on: feedback and update checks retry once directly.

Real sockets on 127.0.0.1: a small relay stand-in, and a proxy address whose
port is closed (what a VPN or proxy app leaves behind after it quits).
"""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from core import feedback, system_proxy, task_logger, update_checker
from tests.app_data_isolation import IsolatedAppDataTestCase
from tests.test_update_checker import _release_payload

_PROXY_VARS = ("http_proxy", "https_proxy", "all_proxy", "no_proxy")


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def _system_proxy(url: str | None) -> Iterator[None]:
    """Point every proxy variable at ``url`` (None: no proxy), NO_PROXY cleared."""
    values = {name: url or "" for name in _PROXY_VARS[:3]}
    values["no_proxy"] = ""
    env = {**values, **{name.upper(): value for name, value in values.items()}}
    with patch.dict("os.environ", env):
        yield


class _Relay:
    """Answers every request with ``status`` and ``body``; counts what arrived."""

    def __init__(self, status: int = 200, body: dict | None = None) -> None:
        relay = self
        self.requests: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def _answer(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                self.rfile.read(length)
                relay.requests.append(f"{self.command} {self.path}")
                payload = json.dumps(body if body is not None else {"id": "FB-20261017-RETRY"}).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = _answer

            def log_message(self, *_args: object) -> None:
                return None

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class FeedbackDirectRetryTests(IsolatedAppDataTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(task_logger._remove_file_handlers)
        self.dead_proxy = f"http://127.0.0.1:{_closed_port()}"

    def _relay(self, status: int = 200, body: dict | None = None) -> _Relay:
        relay = _Relay(status, body)
        self.addCleanup(relay.close)
        return relay

    def _log_lines(self) -> list[str]:
        path = self.app_data_dir / "app.log"
        log = path.read_text(encoding="utf-8") if path.exists() else ""
        return [line for line in log.splitlines() if "[task:feedback]" in line]

    def test_dead_system_proxy_falls_back_to_one_direct_send(self) -> None:
        relay = self._relay()
        with _system_proxy(self.dead_proxy):
            self.assertTrue(system_proxy.proxy_for(relay.url))
            reference = feedback._upload(relay.url, b"zip", {"category": "problem"}, tag="t1")
        self.assertEqual(reference, "FB-20261017-RETRY")
        self.assertEqual(relay.requests, ["POST /v1/feedback"])
        lines = self._log_lines()
        retry = next(line for line in lines if "已改直连重试" in line)
        self.assertIn("发送=t1", retry)
        self.assertIn("ConnectError", retry)
        self.assertTrue(any("反馈直连重试已连上" in line and "HTTP=200" in line for line in lines), lines)

    def test_whole_background_send_succeeds_through_the_fallback(self) -> None:
        relay = self._relay()
        progress: list[int] = []
        with _system_proxy(self.dead_proxy):
            reference = feedback._upload(relay.url, b"x" * 200_000, {}, on_progress=progress.append, tag="t2")
        self.assertEqual(reference, "FB-20261017-RETRY")
        self.assertEqual(progress[-1], 200_000)

    def test_both_attempts_failing_reports_a_network_error(self) -> None:
        with _system_proxy(self.dead_proxy), self.assertRaises(feedback.FeedbackError) as caught:
            feedback._upload(f"http://127.0.0.1:{_closed_port()}", b"zip", {}, tag="t3")
        self.assertEqual(caught.exception.reason, "network")
        self.assertIn("阶段=连接", caught.exception.log_detail)
        self.assertIn("已改直连重试，仍失败", caught.exception.log_detail)

    def test_server_status_through_a_working_proxy_is_not_retried(self) -> None:
        # 这台「代理」能连上，并替服务器回了 500：服务器已经答话，不再直连重发。
        relay = self._relay(500, {"error": "upstream"})
        with _system_proxy(relay.url), self.assertRaises(feedback.FeedbackError) as caught:
            feedback._upload("http://relay.invalid", b"zip", {}, tag="t4")
        self.assertEqual(caught.exception.reason, "server")
        self.assertEqual(relay.requests, ["POST http://relay.invalid/v1/feedback"])
        self.assertFalse(any("直连" in line for line in self._log_lines()))

    def test_relay_errors_after_the_fallback_are_not_retried_again(self) -> None:
        relay = self._relay(429, {"error": "rate_limited"})
        with _system_proxy(self.dead_proxy), self.assertRaises(feedback.FeedbackError) as caught:
            feedback._upload(relay.url, b"zip", {}, tag="t5")
        self.assertEqual(caught.exception.reason, "rate_limited")
        self.assertIn("已改直连重试", caught.exception.log_detail)
        self.assertEqual(len(relay.requests), 1)

    def test_no_proxy_means_no_second_attempt(self) -> None:
        with _system_proxy(None), self.assertRaises(feedback.FeedbackError) as caught:
            feedback._upload(f"http://127.0.0.1:{_closed_port()}", b"zip", {}, tag="t6")
        self.assertEqual(caught.exception.reason, "network")
        self.assertNotIn("直连", caught.exception.log_detail)
        self.assertFalse(any("直连" in line for line in self._log_lines()))


class UpdateCheckDirectRetryTests(IsolatedAppDataTestCase):
    def _check(self, api_url: str) -> update_checker.UpdateCheckResult:
        with patch.object(update_checker, "LATEST_RELEASE_API_URL", api_url):
            return update_checker.check_for_updates(current_version="8.0.0", platform_name="Darwin", machine="arm64")

    def test_dead_system_proxy_falls_back_to_a_direct_check(self) -> None:
        relay = _Relay(200, _release_payload())
        self.addCleanup(relay.close)
        with _system_proxy(f"http://127.0.0.1:{_closed_port()}"):
            result = self._check(f"{relay.url}/releases/latest")
        self.assertTrue(result.has_update, result)
        self.assertEqual(relay.requests, ["GET /releases/latest"])

    def test_server_status_through_a_working_proxy_is_not_retried(self) -> None:
        relay = _Relay(503, {"message": "busy"})
        self.addCleanup(relay.close)
        with _system_proxy(relay.url):
            result = self._check("http://api.invalid/releases/latest")
        self.assertEqual(result.status, "error")
        self.assertEqual(result.diagnostic_code, "release_request_httpstatuserror")
        self.assertEqual(relay.requests, ["GET http://api.invalid/releases/latest"])
