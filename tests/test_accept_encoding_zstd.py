"""Every outgoing client asks for gzip, never zstd (friend's CAD feedback 2026-11-25).

httpx 0.28 adds ``zstd`` to Accept-Encoding as soon as zstandard is installed
(the OCR component download needs it).  DeepSeek then answers in zstd, one
frame per keep-alive flush, and httpx 0.28.1 fails on the second frame with
``DecodingError: cannot use a decompressobj multiple times`` — 0.3 s per
request, 53 of 53 CAD texts untranslated.

The stand-in server below behaves like that: zstd frames flushed one by one
when the client accepts zstd, one gzip body otherwise.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import threading
import time
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar
from unittest.mock import patch

import httpx

from core import component_download, system_proxy, text_transport
from tests.app_data_isolation import IsolatedAppDataTestCase

try:
    import zstandard
except ImportError:  # pragma: no cover - zstandard is in requirements.txt
    zstandard = None

_ANSWER = {
    "id": "x",
    "object": "chat.completion",
    "model": "deepseek-chat",
    "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "译文"}}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}


class _DeepSeekLike(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen_encodings: ClassVar[list[str]] = []

    def log_message(self, *_args) -> None:
        return

    def _chunk(self, data: bytes) -> None:
        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        accept = self.headers.get("Accept-Encoding", "")
        type(self).seen_encodings.append(accept)
        body = json.dumps(_ANSWER).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        if "zstd" in accept and zstandard is not None:
            # Keep-alive blank line first, the answer later: two separate frames.
            self.send_header("Content-Encoding", "zstd")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            compressor = zstandard.ZstdCompressor()
            self._chunk(compressor.compress(b"\n"))
            time.sleep(0.05)
            self._chunk(compressor.compress(body))
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            return
        packed = gzip.compress(body) if "gzip" in accept else body
        if "gzip" in accept:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(packed)))
        self.end_headers()
        self.wfile.write(packed)


@contextmanager
def _server() -> Iterator[str]:
    _DeepSeekLike.seen_encodings = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _DeepSeekLike)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()
        server.server_close()


class AcceptEncodingTest(IsolatedAppDataTestCase):
    def setUp(self) -> None:
        super().setUp()
        # Loopback only: no proxy from the test machine in the way.
        env = {name: "" for name in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")}
        patcher = patch.dict("os.environ", {**env, "no_proxy": "*", "NO_PROXY": "*"})
        patcher.start()
        self.addCleanup(patcher.stop)

    @unittest.skipIf(zstandard is None, "zstandard not installed")
    def test_stand_in_reproduces_the_failure_with_httpx_defaults(self) -> None:
        with _server() as base, httpx.Client(trust_env=False) as client:
            if "zstd" not in client.headers.get("Accept-Encoding", ""):
                self.skipTest("this httpx no longer advertises zstd by itself")
            try:
                client.post(base + "/chat/completions", json={}).json()
            except httpx.DecodingError:
                return
        self.skipTest("this httpx decodes back-to-back zstd frames now")

    def test_clients_ask_for_gzip_only(self) -> None:
        for client in (
            system_proxy.http_client(trust_env=False),
            system_proxy.http_client(trust_env=False, headers={"X-Test": "1"}),
            component_download.direct_client(),
        ):
            with client:
                self.assertEqual(client.headers["Accept-Encoding"], "gzip, deflate")

        async def async_header() -> str:
            async with system_proxy.async_client(trust_env=False) as client:
                return client.headers["Accept-Encoding"]

        self.assertEqual(asyncio.run(async_header()), "gzip, deflate")

    def test_caller_choice_is_kept(self) -> None:
        with system_proxy.http_client(trust_env=False, headers={"Accept-Encoding": "identity"}) as client:
            self.assertEqual(client.headers["Accept-Encoding"], "identity")

    def test_translation_request_gets_its_answer(self) -> None:
        with _server() as base:
            text, route = text_transport.request_text(
                base_url=base, api_key="sk-test", model="deepseek-chat",
                system="翻译", user="原文", api_mode="chat",
            )
        self.assertEqual(text, "译文")
        self.assertEqual(route.mode, "chat")
        self.assertTrue(_DeepSeekLike.seen_encodings)
        for accept in _DeepSeekLike.seen_encodings:
            self.assertNotIn("zstd", accept)
            self.assertIn("gzip", accept)


if __name__ == "__main__":
    unittest.main()
