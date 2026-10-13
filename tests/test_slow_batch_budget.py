"""Slow model answers must not leave batches untranslated (feedback FB-20261013-UVW95F).

A real local HTTP server plays the model, so the whole path runs: dispatcher,
OpenAI engine, text transport with its protocol negotiation.  Time limits are
shrunk to fractions of a second; the shapes of the failures are the ones the
user's log showed with 120 s / 180 s.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

os.environ.setdefault("TRANSLATOR_APP_DATA_DIR", tempfile.mkdtemp(prefix="xl-translator-slow-"))

from core import engine_dispatcher as dispatcher  # noqa: E402
from core import text_transport as transport  # noqa: E402
from engines.openai_engine import OpenAIEngine  # noqa: E402


class _FakeModel:
    """Answers every batch with ``FR:<text>`` after ``delay(items, seen_before)`` seconds."""

    def __init__(self, delay) -> None:
        self.delay = delay
        self.requests: list[list[str]] = []
        self._seen: set[tuple[str, ...]] = set()
        self._lock = threading.Lock()
        model = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:  # keep test output clean
                pass

            def do_POST(self) -> None:  # noqa: N802 - http.server API
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                items = json.loads(body["messages"][-1]["content"])
                with model._lock:
                    key = tuple(items)
                    seen = key in model._seen
                    model._seen.add(key)
                    model.requests.append(items)
                time.sleep(model.delay(items, seen))
                answer = json.dumps([f"FR:{text}" for text in items], ensure_ascii=False)
                payload = json.dumps({"choices": [{
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": answer},
                }]}).encode()
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except OSError:  # the client already gave up on this answer
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "_FakeModel":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"


class SlowBatchBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        transport.clear_protocol_cache()
        # No proxy for the loopback fake model.
        env = patch.dict(os.environ, {"NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"})
        env.start()
        self.addCleanup(env.stop)
        fast_backoff = patch.object(dispatcher, "_TRANSIENT_RETRY_BASE_DELAY", 0.05)
        fast_backoff.start()
        self.addCleanup(fast_backoff.stop)
        self.addCleanup(transport.clear_protocol_cache)

    def _limits(self, *, total: float, read: float):
        defaults = dict(transport.request_text.__kwdefaults__)
        defaults["total_seconds"] = total
        return (
            patch.object(transport.request_text, "__kwdefaults__", defaults),
            patch.object(transport, "READ_TIMEOUT", read),
        )

    def _translate(self, model: _FakeModel, texts, *, batch_size, concurrency, retries=2):
        engine = OpenAIEngine(api_key="k", model="m", base_url=model.base_url, connection_id="slow")
        stats = dispatcher.TranslationBatchRunStats()
        errors: list[str] = []
        results = dispatcher.translate_texts(
            texts, engine, "fr", "system", batch_size=batch_size, concurrency=concurrency,
            error_callback=errors.append, stats=stats, transient_retries=retries,
        )
        return results, stats, errors

    def test_parallel_batches_do_not_spend_their_time_waiting_for_the_first_one(self) -> None:
        # Every answer takes 0.6 s and each request may take 1 s.  The first
        # batch negotiates the protocol while the others wait; they used to
        # start their own send with 0.4 s left and fail with
        # 「请求总时限已用尽；协议仍未确定。」
        texts = [f"条目{index}" for index in range(4)]
        total, read = self._limits(total=1.0, read=5.0)
        with _FakeModel(lambda items, seen: 0.6) as model, total, read:
            results, stats, errors = self._translate(
                model, texts, batch_size=1, concurrency=4, retries=0,
            )
        self.assertEqual(results, {text: f"FR:{text}" for text in texts})
        self.assertEqual(stats.untranslated_count, 0, errors)
        self.assertEqual(len(model.requests), 4)

    def test_resend_after_a_read_timeout_gets_a_full_time_limit(self) -> None:
        # The first answer is slower than the read timeout; the resend answers
        # in 0.8 s, well inside its own 1.8 s.  It used to inherit what the
        # first attempt left (about 0.55 s), time out, and keep the batch as
        # source text.
        texts = ["一", "二", "三"]
        total, read = self._limits(total=1.8, read=1.2)
        with _FakeModel(lambda items, seen: 0.8 if seen else 2.0) as model, total, read:
            results, stats, errors = self._translate(model, texts, batch_size=10, concurrency=1)
        self.assertEqual(results, {text: f"FR:{text}" for text in texts})
        self.assertEqual(stats.untranslated_count, 0, errors)
        self.assertEqual(stats.transient_retry_count, 1)
        self.assertEqual(len(model.requests), 2)

    def test_batch_that_keeps_timing_out_is_halved_once(self) -> None:
        # Four items never answer in time, two do.  After its one resend the
        # batch is split in halves instead of being kept as source text.
        texts = ["甲", "乙", "丙", "丁"]
        total, read = self._limits(total=2.0, read=0.4)
        with _FakeModel(lambda items, seen: 0.8 if len(items) > 2 else 0.05) as model, total, read:
            results, stats, errors = self._translate(
                model, texts, batch_size=10, concurrency=1, retries=1,
            )
        self.assertEqual(results, {text: f"FR:{text}" for text in texts})
        self.assertEqual(stats.untranslated_count, 0, errors)
        self.assertEqual(stats.retry_count, 1)
        self.assertEqual([len(items) for items in model.requests], [4, 4, 2, 2])

    def test_halves_of_a_dead_batch_do_not_resend(self) -> None:
        # A server that never answers costs the batch its resends plus two
        # halves, then the items are kept as source text and counted.
        texts = ["甲", "乙", "丙", "丁"]
        total, read = self._limits(total=2.0, read=0.3)
        with _FakeModel(lambda items, seen: 0.6) as model, total, read:
            results, stats, errors = self._translate(
                model, texts, batch_size=10, concurrency=1, retries=1,
            )
        self.assertEqual(results, {text: text for text in texts})
        self.assertEqual(stats.untranslated_count, 4)
        self.assertEqual([len(items) for items in model.requests], [4, 4, 2, 2])
        self.assertFalse(any("协议" in message for message in errors), errors)


class TransportWaiterTests(unittest.TestCase):
    def setUp(self) -> None:
        transport.clear_protocol_cache()
        self.addCleanup(transport.clear_protocol_cache)

    def test_waiter_does_not_inherit_the_leaders_read_timeout(self) -> None:
        leader_sending = threading.Event()
        calls: list[str] = []

        def send(route, api_key, payload, budget):
            budget.take()
            user = payload["messages"][-1]["content"]
            calls.append(user)
            if user == "slow":
                leader_sending.set()
                time.sleep(0.2)
                raise transport.TextTransportError("read_timeout：请求超时", kind="read_timeout")
            return "OK"

        def call(user):
            return transport.request_text(
                base_url="https://service.test/v1", api_key="k", model="m",
                system="s", user=user, connection_id="waiter", total_seconds=2,
            )

        errors: list[Exception] = []

        def leader():
            try:
                call("slow")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        with patch.object(transport, "_send", side_effect=send):
            thread = threading.Thread(target=leader)
            thread.start()
            self.assertTrue(leader_sending.wait(1))
            text, _route = call("fast")
            thread.join(2)
        self.assertEqual(text, "OK")
        self.assertEqual(calls, ["slow", "fast"])
        self.assertEqual([getattr(error, "kind", "") for error in errors], ["read_timeout"])

    def test_time_limit_message_names_the_limit_not_the_protocol(self) -> None:
        budget = transport.RequestBudget(deadline=time.monotonic() - 1, seconds=180)
        with self.assertRaises(transport.TextTransportError) as caught:
            budget.check()
        self.assertEqual(caught.exception.kind, "total_timeout")
        self.assertIn("180 秒", str(caught.exception))
        self.assertNotIn("协议", str(caught.exception))



class TimeoutAdviceTests(unittest.TestCase):
    def test_only_all_timeout_failures_get_the_timeout_advice(self) -> None:
        from core.task_runner import _all_timeouts

        slow = [
            {"source": "a", "error": "read_timeout：请求超时，未确定服务是否已执行；不自动切换协议。"},
            {"source": "b", "error": "模型服务在 180 秒内未返回完整结果，服务端可能正在排队。"},
        ]
        self.assertTrue(_all_timeouts(slow))
        self.assertFalse(_all_timeouts(slow + [{"source": "c", "error": "HTTP 401 Unauthorized"}]))
        self.assertFalse(_all_timeouts([]))

if __name__ == "__main__":
    unittest.main(verbosity=2)
