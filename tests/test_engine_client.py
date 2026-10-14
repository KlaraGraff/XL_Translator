"""engine.json and the attach-or-start client used by the command line and MCP.

The real-engine cases start ``python -m api.launcher`` with an isolated
``TRANSLATOR_APP_DATA_DIR``, the way the command line does from source.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from core import engine_client, engine_file
from core.engine_client import EngineClient, EngineUnavailable, iter_sse, parse_handshake

REPO_ROOT = Path(__file__).resolve().parents[1]


def _dead_pid() -> int:
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


class _HealthHandler(BaseHTTPRequestHandler):
    token = "fake-token"

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if self.headers.get("X-Translator-Token") != self.token:
            self.send_response(401)
            self.end_headers()
            return
        body = json.dumps({"ok": True, "service": "translator-sidecar"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        return


class _FakeEngine:
    """A loopback server that only answers ``/health``."""

    def __enter__(self) -> _FakeEngine:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()


class EngineFileTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data_dir = Path(temporary.name)

    def test_written_file_reads_back(self) -> None:
        path = engine_file.write_engine_file(port=43123, token="secret", pid=4242, app_data_dir=self.data_dir)

        self.assertEqual(path, self.data_dir / "run" / "engine.json")
        info = engine_file.read_engine_file(self.data_dir)
        assert info is not None
        self.assertEqual((info.port, info.token, info.pid), (43123, "secret", 4242))
        self.assertTrue(info.version)
        self.assertTrue(info.started_at)
        self.assertEqual(set(json.loads(path.read_text())), {"port", "token", "pid", "version", "started_at"})
        self.assertEqual(list(path.parent.iterdir()), [path], "no temporary file is left behind")

    @unittest.skipIf(sys.platform == "win32", "POSIX permission bits")
    def test_only_the_user_can_read_it(self) -> None:
        path = engine_file.write_engine_file(port=1, token="secret", app_data_dir=self.data_dir)

        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_a_later_engine_replaces_the_entry(self) -> None:
        engine_file.write_engine_file(port=1000, token="first", pid=1, app_data_dir=self.data_dir)
        engine_file.write_engine_file(port=2000, token="second", pid=2, app_data_dir=self.data_dir)

        info = engine_file.read_engine_file(self.data_dir)
        assert info is not None
        self.assertEqual((info.port, info.token, info.pid), (2000, "second", 2))

    def test_an_engine_only_removes_its_own_entry(self) -> None:
        engine_file.write_engine_file(port=2000, token="second", pid=2, app_data_dir=self.data_dir)

        self.assertFalse(engine_file.remove_engine_file(pid=1, app_data_dir=self.data_dir))
        self.assertIsNotNone(engine_file.read_engine_file(self.data_dir))
        self.assertTrue(engine_file.remove_engine_file(pid=2, app_data_dir=self.data_dir))
        self.assertIsNone(engine_file.read_engine_file(self.data_dir))
        self.assertFalse(engine_file.remove_engine_file(pid=2, app_data_dir=self.data_dir))

    def test_missing_or_damaged_files_read_as_absent(self) -> None:
        self.assertIsNone(engine_file.read_engine_file(self.data_dir))
        path = engine_file.engine_file_path(self.data_dir)
        path.parent.mkdir(parents=True)
        for body in ("{", "[]", '{"port": 1}', '{"port": 0, "token": "t", "pid": 1}', '{"port": 1, "token": "", "pid": 1}'):
            path.write_text(body)
            self.assertIsNone(engine_file.read_engine_file(self.data_dir), body)


class HandshakeAndStreamParsingTests(unittest.TestCase):
    """The same cases ``parses_launcher_handshake`` pins in src-tauri/src/main.rs."""

    def test_handshake(self) -> None:
        self.assertEqual(parse_handshake("PORT=43123 TOKEN=one-time-token\n"), (43123, "one-time-token"))
        self.assertEqual(parse_handshake("TOKEN=t PORT=1"), (1, "t"))
        for line in ("PORT=43123\n", "TOKEN=one-time-token\n", "PORT=x TOKEN=t", "PORT=70000 TOKEN=t", "PORT=1 TOKEN=", ""):
            with self.assertRaises(EngineUnavailable, msg=line):
                parse_handshake(line)

    def test_event_stream(self) -> None:
        lines = [
            ": keepalive",
            "",
            "id: 1",
            "event: progress",
            'data: {"overall_percent":12.5}',
            "",
            ":" + " " * 2048,
            "",
            "id: 2",
            "event: done",
            'data: {"output_dir":"/tmp/out"}',
            "",
        ]

        events = list(iter_sse(iter(lines)))

        self.assertEqual([(event.id, event.type) for event in events], [(1, "progress"), (2, "done")])
        self.assertEqual(events[0].data, {"overall_percent": 12.5})
        self.assertFalse(events[0].terminal)
        self.assertTrue(events[1].terminal)


class ConnectTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data_dir = Path(temporary.name)

    def test_attaches_to_the_published_engine(self) -> None:
        with _FakeEngine() as fake:
            engine_file.write_engine_file(port=fake.port, token="fake-token", pid=os.getpid(), app_data_dir=self.data_dir)

            with engine_client.connect(spawn_if_needed=False, app_data_dir=self.data_dir) as client:
                self.assertFalse(client.started_here)
                self.assertEqual(client.base_url, f"http://127.0.0.1:{fake.port}")
                self.assertEqual(client.pid, os.getpid())

    def test_a_stale_entry_is_ignored(self) -> None:
        with _FakeEngine() as fake:
            # Process gone (a crash left the file behind).
            engine_file.write_engine_file(port=fake.port, token="fake-token", pid=_dead_pid(), app_data_dir=self.data_dir)
            self.assertIsNone(engine_client.attach(app_data_dir=self.data_dir))
            # Process alive but the address answers with another token (pid reused).
            engine_file.write_engine_file(port=fake.port, token="old-token", pid=os.getpid(), app_data_dir=self.data_dir)
            self.assertIsNone(engine_client.attach(app_data_dir=self.data_dir))
        # Nothing listening any more.
        engine_file.write_engine_file(port=fake.port, token="fake-token", pid=os.getpid(), app_data_dir=self.data_dir)
        self.assertIsNone(engine_client.attach(app_data_dir=self.data_dir))
        with self.assertRaises(EngineUnavailable):
            engine_client.connect(spawn_if_needed=False, app_data_dir=self.data_dir)

    def test_neither_attach_nor_start_works(self) -> None:
        broken = ([sys.executable, "-c", "import sys; print('boom', file=sys.stderr); sys.exit(3)"], None)
        with patch.object(engine_client, "engine_command", return_value=broken):
            with self.assertRaises(EngineUnavailable) as caught:
                engine_client.connect(app_data_dir=self.data_dir)
        # The engine's own last words come with the error.
        self.assertIn("boom", str(caught.exception))

    def test_a_start_that_never_answers_times_out(self) -> None:
        silent = ([sys.executable, "-c", "import time; time.sleep(30)"], None)
        with patch.object(engine_client, "engine_command", return_value=silent):
            started = time.monotonic()
            with self.assertRaises(EngineUnavailable):
                engine_client.spawn(app_data_dir=self.data_dir, start_timeout=1.0)
        self.assertLess(time.monotonic() - started, 10)


class RealEngineTests(unittest.TestCase):
    """``python -m api.launcher`` started the way the command line starts it."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data_dir = Path(temporary.name)

    def test_started_engine_publishes_itself_and_withdraws_on_close(self) -> None:
        client = engine_client.connect(app_data_dir=self.data_dir)
        try:
            self.assertTrue(client.started_here)
            self.assertTrue(client.health())
            info = engine_file.read_engine_file(self.data_dir)
            assert info is not None
            self.assertEqual(info.pid, client.pid)
            self.assertEqual(client.base_url, f"http://127.0.0.1:{info.port}")
            if sys.platform != "win32":
                self.assertEqual(stat.S_IMODE(engine_file.engine_file_path(self.data_dir).stat().st_mode), 0o600)
            # A second command now attaches to it instead of starting another.
            with engine_client.connect(spawn_if_needed=False, app_data_dir=self.data_dir) as second:
                self.assertFalse(second.started_here)
                self.assertEqual(second.pid, client.pid)
                self.assertEqual([item["key"] for item in second.surfaces()][:4], ["excel", "word", "pdf", "cad"])
        finally:
            process = client._process
            client.close()
        assert process is not None
        self.assertEqual(process.returncode, 0)
        self.assertIsNone(engine_file.read_engine_file(self.data_dir))

    def test_started_engine_ends_with_its_parent(self) -> None:
        parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        self.addCleanup(lambda: parent.poll() is None and parent.kill())
        client = engine_client.spawn(parent_pid=parent.pid, app_data_dir=self.data_dir)
        process = client._process
        assert process is not None
        try:
            self.assertTrue(client.health())
            parent.kill()
            parent.wait()
            # The watchdog polls once a second, then unwinds like a normal quit.
            self.assertEqual(process.wait(timeout=30), 0)
            self.assertIsNone(engine_file.read_engine_file(self.data_dir))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            client.close()


class ClientErrorTests(unittest.TestCase):
    def test_refusals_carry_the_engine_sentence_and_reason(self) -> None:
        from fastapi.testclient import TestClient

        from api.app import create_app

        app = create_app(auth_token="t")
        with TestClient(app) as http:
            client = EngineClient("http://testserver", "t")
            client._http = http
            http.headers["X-Translator-Token"] = "t"
            with self.assertRaises(engine_client.EngineError) as missing:
                client.task("no-such-task")
            self.assertEqual(missing.exception.status, 404)
            self.assertIn("找不到这个任务", missing.exception.detail)
            with self.assertRaises(engine_client.EngineError) as invalid:
                client.start({"surface": "excel"})
            self.assertEqual(invalid.exception.status, 422)
            self.assertTrue(invalid.exception.detail)


if __name__ == "__main__":
    unittest.main(verbosity=2)
