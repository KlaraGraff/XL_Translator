from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import httpx

os.environ.setdefault("TRANSLATOR_APP_DATA_DIR", tempfile.mkdtemp(prefix="xl-translator-text-"))

from core import text_transport as transport


class TextTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        transport.clear_protocol_cache()

    def tearDown(self) -> None:
        transport.clear_protocol_cache()

    def test_real_async_client_mock_transport_requires_completed_chat_response(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "choices": [{
                        "finish_reason": "stop",
                        "message": {"content": "OK"},
                    }],
                },
                request=request,
            )

        real_async_client = httpx.AsyncClient

        def build_client(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return real_async_client(*args, **kwargs)

        with patch.object(transport.httpx, "AsyncClient", side_effect=build_client):
            text, route = transport.request_text(
                base_url="https://service.test/v1",
                api_key="k",
                model="m",
                system="s",
                user="u",
                api_mode="chat",
                connection_id="real-async",
            )

        self.assertEqual(text, "OK")
        self.assertEqual(route.mode, "chat")
        self.assertEqual(requests[0].url, "https://service.test/v1/chat/completions")

    def test_cached_http_error_has_same_type_and_classifier_for_leader_and_follower(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(
                429,
                json={"error": {"message": "too many requests"}},
                request=request,
            )

        real_async_client = httpx.AsyncClient

        def build_client(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return real_async_client(*args, **kwargs)

        errors: list[Exception] = []
        with patch.object(transport.httpx, "AsyncClient", side_effect=build_client):
            for _ in range(2):
                try:
                    transport.request_text(
                        base_url="https://service.test/v1",
                        api_key="k",
                        model="m",
                        system="s",
                        user="u",
                        api_mode="chat",
                        connection_id="cached-http-error",
                    )
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

        self.assertEqual(calls, 1)
        self.assertEqual([type(error) for error in errors], [
            transport.TextTransportHTTPError,
            transport.TextTransportHTTPError,
        ])
        self.assertTrue(all(error.no_replay for error in errors))
        self.assertTrue(all(transport.classify_http_error(error) == ("transient", "") for error in errors))

    def test_auto_switches_only_after_explicit_route_error(self) -> None:
        calls: list[str] = []

        def send(route, api_key, payload, budget):
            calls.append(route.mode)
            if route.mode == "chat":
                request = httpx.Request("POST", route.url)
                response = httpx.Response(
                    404,
                    json={"error": {"code": "unsupported_endpoint"}},
                    request=request,
                )
                raise httpx.HTTPStatusError("unsupported", request=request, response=response)
            return "OK"

        routes = [transport.Route("chat", "https://service.test/chat"), transport.Route("responses", "https://service.test/responses")]
        with (
            patch.object(transport, "_send", side_effect=send),
            patch.object(transport, "candidate_routes", return_value=routes),
        ):
            text, route = transport.request_text(
                base_url="https://service.test/v1",
                api_key="k",
                model="m",
                system="s",
                user="u",
                connection_id="c",
            )
        self.assertEqual((text, route.mode), ("OK", "responses"))
        self.assertEqual(calls, ["chat", "responses"])

    def test_timeout_is_not_replayed_or_sent_to_next_protocol(self) -> None:
        calls: list[str] = []

        def send(route, api_key, payload, budget):
            calls.append(route.mode)
            raise transport.TextTransportError("read timeout", kind="read_timeout")

        routes = [transport.Route("chat", "https://service.test/chat")]
        routes.append(transport.Route("responses", "https://service.test/responses"))
        with (
            patch.object(transport, "_send", side_effect=send),
            patch.object(transport, "candidate_routes", return_value=routes),
        ):
            with self.assertRaises(transport.TextTransportError) as caught:
                transport.request_text(
                    base_url="https://service.test/v1",
                    api_key="k",
                    model="m",
                    system="s",
                    user="u",
                    connection_id="timeout",
                )
        self.assertEqual(caught.exception.kind, "read_timeout")
        self.assertEqual(calls, ["chat"])

    def test_responses_json_and_completed_event_are_complete_once(self) -> None:
        payload = {
            "status": "completed",
            "output": [{
                "type": "message",
                "status": "completed",
                "content": [{"type": "output_text", "text": "done"}],
            }],
        }
        self.assertEqual(transport._response_text(payload), "done")
        events = [
            'data: {"type":"response.output_text.delta","delta":"wrong"}',
            'data: {"type":"response.completed","response":' + str(payload).replace("'", '"') + '}',
        ]
        self.assertEqual(transport.extract_responses_events(events), "done")

    def test_empty_success_and_incomplete_stream_are_rejected(self) -> None:
        with self.assertRaises(transport.TextTransportError):
            transport.extract_chat_text({"choices": [{"finish_reason": "stop", "message": {"content": ""}}]})
        with self.assertRaises(transport.TextTransportError) as caught:
            transport.extract_responses_events([
                'data: {"type":"response.output_text.delta","delta":"partial"}',
                'data: [DONE]',
            ])
        self.assertEqual(caught.exception.kind, "incomplete")

    def test_concurrent_probe_is_single_flight_and_failure_is_shared(self) -> None:
        started = threading.Event()
        release = threading.Event()
        calls = 0
        lock = threading.Lock()

        def send(route, api_key, payload, budget):
            nonlocal calls
            with lock:
                calls += 1
            started.set()
            release.wait(2)
            raise transport.TextTransportError("probe failed", kind="probe_failed")

        def invoke(errors):
            try:
                transport.request_text(
                    base_url="https://service.test/v1", api_key="k", model="m",
                    system="s", user="u", connection_id="same",
                    total_seconds=2,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        errors: list[Exception] = []
        routes = [transport.Route("chat", f"https://service.test/r{index}") for index in range(8)]
        with (
            patch.object(transport, "_send", side_effect=send),
            patch.object(transport, "candidate_routes", return_value=routes),
        ):
            first = threading.Thread(target=invoke, args=(errors,))
            second = threading.Thread(target=invoke, args=(errors,))
            first.start()
            self.assertTrue(started.wait(1))
            second.start()
            time.sleep(0.05)
            release.set()
            first.join(2)
            second.join(2)
        self.assertEqual(calls, 1)
        self.assertEqual(len(errors), 2)
        self.assertTrue(all(getattr(error, "kind", "") == "probe_failed" for error in errors))

    def test_waiter_can_cancel_without_deadlocking_leader(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def send(route, api_key, payload, budget):
            started.set()
            release.wait(2)
            return "OK"

        routes = [transport.Route("chat", f"https://service.test/r{index}") for index in range(8)]
        with (
            patch.object(transport, "_send", side_effect=send),
            patch.object(transport, "candidate_routes", return_value=routes),
        ):
            leader_result: list[object] = []

            def leader():
                leader_result.append(transport.request_text(
                    base_url="https://service.test/v1", api_key="k", model="m",
                    system="s", user="u", connection_id="cancel", total_seconds=2,
                ))

            thread = threading.Thread(target=leader)
            thread.start()
            self.assertTrue(started.wait(1))
            with self.assertRaises(transport.TextTransportError) as caught:
                transport.request_text(
                    base_url="https://service.test/v1", api_key="k", model="m",
                    system="s", user="u", connection_id="cancel", total_seconds=0.01,
                    should_stop=lambda: True,
                )
            self.assertEqual(caught.exception.kind, "cancelled")
            release.set()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(leader_result[0][0], "OK")

    def test_attempt_budget_is_shared_across_protocol_candidates(self) -> None:
        calls = 0

        def send(route, api_key, payload, budget):
            nonlocal calls
            calls += 1
            request = httpx.Request("POST", route.url)
            response = httpx.Response(404, json={"error": {"code": "unsupported_endpoint"}}, request=request)
            raise httpx.HTTPStatusError("route", request=request, response=response)

        routes = [transport.Route("chat", f"https://service.test/r{index}") for index in range(8)]
        with (
            patch.object(transport, "_send", side_effect=send),
            patch.object(transport, "candidate_routes", return_value=routes),
        ):
            with self.assertRaises(transport.TextTransportError) as caught:
                transport.request_text(
                    base_url="https://service.test/v1", api_key="k", model="m",
                    system="s", user="u", connection_id="budget", total_seconds=2,
                )
        self.assertEqual(caught.exception.kind, "budget_exhausted")
        self.assertEqual(calls, transport.MAX_SENDS)

    def test_attempt_budget_caps_optional_parameter_retries(self) -> None:
        calls = []

        def send(route, api_key, payload, budget):
            budget.take()
            calls.append(payload)
            param = "store" if "store" in payload else "stream"
            request = httpx.Request("POST", route.url)
            response = httpx.Response(400, json={"error": {
                "param": param, "message": "unsupported parameter",
            }}, request=request)
            raise httpx.HTTPStatusError("parameter", request=request, response=response)

        with transport.request_budget(seconds=2) as budget:
            budget.remaining = 2
            with patch.object(transport, "_send", side_effect=send):
                with self.assertRaises(transport.TextTransportError) as caught:
                    transport.request_text(
                        base_url="https://service.test/v1", api_key="k", model="m",
                        system="s", user="u", api_mode="responses",
                        connection_id="parameter-budget",
                    )
        self.assertEqual(caught.exception.kind, "budget_exhausted")
        self.assertEqual(len(calls), 2)
        self.assertNotIn("store", calls[1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
