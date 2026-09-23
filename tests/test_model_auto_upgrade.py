"""Isolated startup upgrade and runtime rollback contract."""

from __future__ import annotations

import unittest
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch

import httpx

from core import model_auto_upgrade as upgrade
from core import text_transport
from core.model_catalog import ModelCatalogResult
from core.model_catalog import clear_model_catalog_cache
from core.model_roles import ROLE_TRANSLATION, add_role_connection, update_role_connection
from settings import (
    AppSettings, load_settings, save_connection_key, save_settings,
    set_cloud_provider_config,
)
from tests.app_data_isolation import IsolatedAppDataTestCase


def _metadata(version: str, family: str, date: str, cost: float) -> dict:
    return {
        "id": version,
        "family": family,
        "release_date": date,
        "modalities": {"input": ["text"], "output": ["text"]},
        "cost": {"input": cost, "output": cost},
    }


CATALOG = {"openai": {"models": {
    "gpt-5.6-luna": _metadata("gpt-5.6-luna", "gpt-luna", "2026-07-09", 2),
    "gpt-6-luna": _metadata("gpt-6-luna", "gpt-luna", "2026-09-22", 1),
    "gpt-6.1-luna": _metadata("gpt-6.1-luna", "gpt-luna", "2026-10-01", 1),
    "gpt-5.6-sol": _metadata("gpt-5.6-sol", "gpt-sol", "2026-07-09", 20),
    "gpt-6-sol": _metadata("gpt-6-sol", "gpt-sol", "2026-09-22", 10),
}}}


def _model_error(model: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    response = httpx.Response(
        404,
        json={"error": {"code": "model_not_found", "message": model}},
        request=request,
    )
    return httpx.HTTPStatusError("model unavailable", request=request, response=response)


class ModelAutoUpgradeTests(IsolatedAppDataTestCase):
    def setUp(self) -> None:
        super().setUp()
        clear_model_catalog_cache()
        settings = AppSettings()
        connection = settings.engine.connections[0]
        update_role_connection(
            settings, ROLE_TRANSLATION, connection.id,
            provider="openai", model="gpt-5.6-luna",
            base_url="https://example.test/v1", api_mode="chat",
        )
        save_settings(settings)
        self.connection_id = connection.id
        self.state_key = f"translation:{connection.id}"
        save_connection_key(connection.id, "isolated-fake-key")
        self.addCleanup(text_transport.clear_protocol_cache)
        self.addCleanup(clear_model_catalog_cache)

    def test_fake_http_end_to_end_catalog_models_and_live_probe(self) -> None:
        """Exercise all three HTTP hops with a fake endpoint and fake key."""
        calls: list[tuple[str, str]] = []
        catalog = {"openai": {"models": {
            key: value for key, value in CATALOG["openai"]["models"].items()
            if key != "gpt-6.1-luna"
        }}}

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append((request.method, str(request.url)))
            if str(request.url) == upgrade.CATALOG_URL:
                return httpx.Response(200, json=catalog, request=request)
            self.assertEqual(request.headers.get("authorization"), "Bearer isolated-fake-key")
            if str(request.url) == "https://example.test/v1/models":
                return httpx.Response(
                    200, json={"data": [{"id": "gpt-6-luna"}]}, request=request,
                )
            self.assertEqual(str(request.url), "https://example.test/v1/chat/completions")
            self.assertEqual(json.loads(request.content)["model"], "gpt-6-luna")
            return httpx.Response(200, json={
                "choices": [{"finish_reason": "stop", "message": {"content": "Bonjour"}}],
            }, request=request)

        real_client = httpx.Client
        real_async_client = httpx.AsyncClient

        def client(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return real_client(*args, **kwargs)

        def async_client(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return real_async_client(*args, **kwargs)

        with (
            patch.object(httpx, "Client", side_effect=client),
            patch.object(httpx, "AsyncClient", side_effect=async_client),
        ):
            upgrade.auto_upgrade_models_on_startup()

        self.assertEqual(calls, [
            ("GET", upgrade.CATALOG_URL),
            ("GET", "https://example.test/v1/models"),
            ("POST", "https://example.test/v1/chat/completions"),
        ])
        self.assertEqual(load_settings().engine.cloud_model, "gpt-6-luna")

    def _run(self, catalog=None, probe=None, listed=None):
        if catalog is None:
            catalog = {"openai": {"models": {
                k: v for k, v in CATALOG["openai"]["models"].items()
                if k != "gpt-6.1-luna"
            }}}
        if probe is None:
            probe = ("Bonjour", None)
        if listed is None:
            listed = ["gpt-6-luna", "gpt-6.1-luna", "gpt-6-sol"]
        with (
            patch.object(upgrade, "_catalog", return_value=catalog),
            patch.object(upgrade, "fetch_openai_compatible_models", return_value=ModelCatalogResult(
                ok=True, models=listed, message="ok",
            )),
            patch.object(
                upgrade, "request_text",
                side_effect=probe if isinstance(probe, Exception) or callable(probe) else None,
                return_value=probe if not isinstance(probe, Exception) and not callable(probe) else None,
            ) as request,
        ):
            upgrade.auto_upgrade_models_on_startup()
        return request

    def test_promotes_only_after_endpoint_lists_and_answers_candidate(self) -> None:
        request = self._run()
        self.assertEqual(request.call_args.kwargs["model"], "gpt-6-luna")
        settings = load_settings()
        self.assertEqual(settings.engine.connections[0].model, "gpt-6-luna")
        self.assertEqual(settings.engine.cloud_model, "gpt-6-luna")
        self.assertEqual(settings.model_upgrade_states[self.state_key].source_model, "gpt-5.6-luna")

    def test_missing_endpoint_model_never_changes_settings(self) -> None:
        request = self._run(listed=["gpt-5.6-luna"])
        self.assertEqual(request.call_count, 0)
        settings = load_settings()
        self.assertEqual(settings.engine.cloud_model, "gpt-5.6-luna")
        self.assertEqual(settings.model_upgrade_states[self.state_key].consecutive_failures, 1)

    def test_catalog_request_failure_does_not_count(self) -> None:
        with (
            patch.object(upgrade, "_catalog", return_value=CATALOG),
            patch.object(upgrade, "fetch_openai_compatible_models", return_value=ModelCatalogResult(
                ok=False, models=[], message="offline",
            )),
        ):
            upgrade.auto_upgrade_models_on_startup()
        self.assertEqual(load_settings().model_upgrade_states, {})

    def test_active_candidate_withdrawn_from_endpoint_restores_verified_old_model(self) -> None:
        self._run()
        request = self._run(listed=["gpt-5.6-luna"])
        self.assertEqual(request.call_args.kwargs["model"], "gpt-5.6-luna")
        settings = load_settings()
        self.assertEqual(settings.engine.cloud_model, "gpt-5.6-luna")
        self.assertEqual(settings.model_upgrade_states[self.state_key].status, "failed")

    def test_active_candidate_stays_when_predecessor_cannot_be_verified(self) -> None:
        self._run()
        request = self._run(listed=[])
        self.assertEqual(request.call_count, 0)
        self.assertEqual(load_settings().engine.cloud_model, "gpt-6-luna")

    def test_three_model_failures_pause_until_newer_candidate(self) -> None:
        for count in (1, 2, 3):
            self._run(probe=_model_error("gpt-6-luna"))
            settings = load_settings()
            state = settings.model_upgrade_states[self.state_key]
            self.assertEqual(state.consecutive_failures, count)
            self.assertEqual(settings.engine.cloud_model, "gpt-5.6-luna")
        self.assertEqual(state.status, "paused")
        request = self._run(probe=_model_error("gpt-6-luna"))
        self.assertEqual(request.call_count, 0)
        self.assertEqual(load_settings().model_upgrade_states[self.state_key].consecutive_failures, 3)
        request = self._run(catalog=CATALOG)
        self.assertEqual(request.call_args.kwargs["model"], "gpt-6.1-luna")
        self.assertEqual(load_settings().engine.cloud_model, "gpt-6.1-luna")

    def test_transient_and_auth_errors_do_not_count(self) -> None:
        for status in (429, 401):
            request = httpx.Request("POST", "https://example.test/v1/chat/completions")
            response = httpx.Response(status, request=request)
            self._run(probe=httpx.HTTPStatusError("unavailable", request=request, response=response))
            self.assertEqual(load_settings().model_upgrade_states, {})

    def test_runtime_model_error_rolls_back_and_retries_current_text(self) -> None:
        self._run()
        text_transport.clear_protocol_cache()

        def send(route, api_key, payload, budget):
            budget.take()
            if payload["model"] == "gpt-6-luna":
                raise _model_error("gpt-6-luna")
            return "Bonjour"

        with (
            patch.object(text_transport, "_send", side_effect=send),
        ):
            text, _route = text_transport.request_text(
                base_url="https://example.test/v1", api_key="isolated-fake-key",
                model="gpt-6-luna", system="Translate", user="Hello",
                api_mode="chat", connection_id=self.connection_id,
            )
            again, _route = text_transport.request_text(
                base_url="https://example.test/v1", api_key="isolated-fake-key",
                model="gpt-6-luna", system="Translate", user="Again",
                api_mode="chat", connection_id=self.connection_id,
            )
        self.assertEqual((text, again), ("Bonjour", "Bonjour"))
        settings = load_settings()
        self.assertEqual(settings.engine.cloud_model, "gpt-5.6-luna")
        self.assertEqual(settings.model_upgrade_states[self.state_key].status, "failed")

    def test_simultaneous_waiter_also_retries_restored_model(self) -> None:
        self._run()
        text_transport.clear_protocol_cache()
        candidate_started = Event()
        release_candidate = Event()

        def send(route, api_key, payload, budget):
            budget.take()
            if payload["model"] == "gpt-6-luna":
                candidate_started.set()
                self.assertTrue(release_candidate.wait(2))
                raise _model_error("gpt-6-luna")
            return "Bonjour"

        def ask():
            return text_transport.request_text(
                base_url="https://example.test/v1", api_key="isolated-fake-key",
                model="gpt-6-luna", system="Translate", user="Hello",
                api_mode="chat", connection_id=self.connection_id,
            )[0]

        with (
            patch.object(text_transport, "_send", side_effect=send),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            first = executor.submit(ask)
            self.assertTrue(candidate_started.wait(2))
            second = executor.submit(ask)
            release_candidate.set()
            self.assertEqual((first.result(2), second.result(2)), ("Bonjour", "Bonjour"))

    def test_manual_model_edit_pins_connection(self) -> None:
        self._run()
        settings = load_settings()
        update_role_connection(settings, ROLE_TRANSLATION, self.connection_id, model="gpt-5.6-luna")
        save_settings(settings)
        self.assertEqual(load_settings().model_upgrade_states[self.state_key].status, "manual")
        self.assertEqual(self._run().call_count, 0)

    def test_single_connection_follower_has_own_upgrade_and_rollback(self) -> None:
        settings = load_settings()
        owner = settings.cleaner_model_role
        set_cloud_provider_config(owner, owner.cloud_provider, cloud_model="gpt-5.6-sol")
        save_settings(settings)

        def probe(**kwargs):
            if kwargs["model"] == "gpt-6-sol":
                raise _model_error("gpt-6-sol")
            return ("Bonjour", None)

        self._run(probe=probe)
        settings = load_settings()
        self.assertEqual(settings.engine.cloud_model, "gpt-6-luna")
        self.assertEqual(settings.cleaner_model_role.cloud_model, "gpt-5.6-sol")
        self.assertEqual(
            settings.model_upgrade_states[f"cleaner:{self.connection_id}"].status,
            "failed",
        )
        self._run()
        self.assertEqual(load_settings().cleaner_model_role.cloud_model, "gpt-6-sol")

        def send(route, api_key, payload, budget):
            budget.take()
            if payload["model"] == "gpt-6-sol":
                raise _model_error("gpt-6-sol")
            return "Bonjour"

        with patch.object(text_transport, "_send", side_effect=send):
            text_transport.request_text(
                base_url="https://example.test/v1", api_key="isolated-fake-key",
                model="gpt-6-sol", system="Translate", user="Hello",
                api_mode="chat", connection_id=self.connection_id,
                model_role="cleaner",
            )
        settings = load_settings()
        self.assertEqual(settings.cleaner_model_role.cloud_model, "gpt-5.6-sol")
        self.assertEqual(settings.engine.cloud_model, "gpt-6-luna")

    def test_multi_connection_follower_is_not_auto_upgraded(self) -> None:
        settings = load_settings()
        owner = settings.cleaner_model_role
        set_cloud_provider_config(owner, owner.cloud_provider, cloud_model="gpt-5.6-sol")
        add_role_connection(
            settings, ROLE_TRANSLATION, provider="openai", model="gpt-5.6-luna",
            base_url="https://second.example.test/v1",
        )
        save_settings(settings)
        self._run()
        self.assertEqual(load_settings().cleaner_model_role.cloud_model, "gpt-5.6-sol")
        self.assertNotIn(f"cleaner:{self.connection_id}", load_settings().model_upgrade_states)

    def test_deepseek_only_known_official_alias_is_renamed(self) -> None:
        settings = load_settings()
        update_role_connection(
            settings, ROLE_TRANSLATION, self.connection_id,
            provider="deepseek", model="deepseek-v4-flash",
        )
        save_settings(settings)
        deepseek_catalog = {"deepseek": {"models": {
            "deepseek-v4-flash": _metadata(
                "deepseek-v4-flash", "deepseek-flash", "2026-09-10", 1,
            ),
            "deepseek-flash": _metadata(
                "deepseek-flash", "deepseek-flash", "2026-09-10", 1,
            ),
            "deepseek-v4-pro": _metadata(
                "deepseek-v4-pro", "deepseek-thinking", "2026-08-12", 5,
            ),
        }}}
        request = self._run(catalog=deepseek_catalog, listed=["deepseek-flash"])
        self.assertEqual(request.call_args.kwargs["model"], "deepseek-flash")
        self.assertEqual(load_settings().engine.cloud_model, "deepseek-flash")
        self.assertEqual(self._run(catalog=deepseek_catalog, listed=["deepseek-flash"]).call_count, 0)


if __name__ == "__main__":
    unittest.main()
