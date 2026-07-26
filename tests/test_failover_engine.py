"""Runtime failover behaviour of the wrapping translation engine."""

from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

from core.engine_dispatcher import build_role_engine
from core.failover_engine import FailoverTranslationEngine
from core.model_roles import ROLE_TRANSLATION, add_role_connection
from settings import (
    AppSettings,
    ModelConnection,
    current_key_overrides,
    provider_key_overrides,
)


class _HttpError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class _StubEngine:
    def __init__(self, label: str, error: Exception | None = None) -> None:
        self.label = label
        self.error = error
        self.calls = 0

    def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return {text: f"{self.label}:{text}" for text in texts}


def _pool(*specs: tuple[str, str]) -> list[ModelConnection]:
    return [
        ModelConnection(label=label, provider="custom_openai", base_url=url)
        for label, url in specs
    ]


def _run(engine: FailoverTranslationEngine) -> dict[str, str]:
    return engine.translate_batch(["hello"], "en", "prompt", source_lang="zh")


class FailoverEngineTests(unittest.TestCase):
    def test_a_healthy_connection_is_used_without_switching(self) -> None:
        pool = _pool(("A", "https://a.example/v1"), ("B", "https://b.example/v1"))
        engines = {pool[0].id: _StubEngine("A"), pool[1].id: _StubEngine("B")}
        engine = FailoverTranslationEngine(
            build_engine_for=lambda conn: engines[conn.id], candidates=pool
        )
        self.assertEqual(_run(engine), {"hello": "A:hello"})
        self.assertEqual(engines[pool[1].id].calls, 0)

    def test_a_rejected_credential_moves_to_the_next_connection(self) -> None:
        pool = _pool(("A", "https://a.example/v1"), ("B", "https://b.example/v1"))
        engines = {
            pool[0].id: _StubEngine("A", _HttpError(401)),
            pool[1].id: _StubEngine("B"),
        }
        switches: list[tuple[str, str, str]] = []
        engine = FailoverTranslationEngine(
            build_engine_for=lambda conn: engines[conn.id],
            candidates=pool,
            on_switch=lambda old, new, kind, _err: switches.append(
                (old.label, new.label, kind)
            ),
        )
        self.assertEqual(_run(engine), {"hello": "B:hello"})
        self.assertEqual(switches, [("A", "B", "credential")])
        self.assertEqual(engine.current_connection.label, "B")

    def test_a_dead_endpoint_skips_the_other_account_on_that_server(self) -> None:
        pool = _pool(
            ("A1", "https://a.example/v1"),
            ("A2", "https://a.example/v1"),
            ("B", "https://b.example/v1"),
        )
        engines = {
            pool[0].id: _StubEngine("A1", _HttpError(503)),
            pool[1].id: _StubEngine("A2"),
            pool[2].id: _StubEngine("B"),
        }
        engine = FailoverTranslationEngine(
            build_engine_for=lambda conn: engines[conn.id], candidates=pool
        )
        self.assertEqual(_run(engine), {"hello": "B:hello"})
        # The second account on the dead server must never be contacted.
        self.assertEqual(engines[pool[1].id].calls, 0)

    def test_rate_limiting_does_not_burn_a_connection(self) -> None:
        pool = _pool(("A", "https://a.example/v1"), ("B", "https://b.example/v1"))
        engines = {
            pool[0].id: _StubEngine("A", _HttpError(429)),
            pool[1].id: _StubEngine("B"),
        }
        engine = FailoverTranslationEngine(
            build_engine_for=lambda conn: engines[conn.id], candidates=pool
        )
        with self.assertRaises(_HttpError):
            _run(engine)
        self.assertEqual(engines[pool[1].id].calls, 0)
        self.assertEqual(engine.current_connection.label, "A")

    def test_exhausting_every_connection_raises(self) -> None:
        pool = _pool(("A", "https://a.example/v1"), ("B", "https://b.example/v1"))
        engines = {
            pool[0].id: _StubEngine("A", _HttpError(401)),
            pool[1].id: _StubEngine("B", _HttpError(403)),
        }
        engine = FailoverTranslationEngine(
            build_engine_for=lambda conn: engines[conn.id], candidates=pool
        )
        with self.assertRaises(_HttpError):
            _run(engine)

    def test_a_connection_is_not_retried_after_it_was_ruled_out(self) -> None:
        pool = _pool(
            ("A", "https://a.example/v1"),
            ("B", "https://b.example/v1"),
            ("C", "https://c.example/v1"),
        )
        engines = {
            pool[0].id: _StubEngine("A", _HttpError(401)),
            pool[1].id: _StubEngine("B", _HttpError(401)),
            pool[2].id: _StubEngine("C"),
        }
        engine = FailoverTranslationEngine(
            build_engine_for=lambda conn: engines[conn.id], candidates=pool
        )
        self.assertEqual(_run(engine), {"hello": "C:hello"})
        # A second batch stays on C rather than walking the dead entries again.
        self.assertEqual(_run(engine), {"hello": "C:hello"})
        self.assertEqual(engines[pool[0].id].calls, 1)
        self.assertEqual(engines[pool[1].id].calls, 1)

    def test_an_empty_chain_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            FailoverTranslationEngine(build_engine_for=lambda conn: None, candidates=[])

    def test_a_dead_default_endpoint_only_burns_its_own_provider(self) -> None:
        # zhipu and dashscope both leave base_url empty without sharing a
        # server; a raw string comparison used to take them down together.
        pool = [
            ModelConnection(label="Z", provider="zhipu", base_url=""),
            ModelConnection(label="D", provider="dashscope", base_url=""),
        ]
        engines = {
            pool[0].id: _StubEngine("Z", _HttpError(503)),
            pool[1].id: _StubEngine("D"),
        }
        engine = FailoverTranslationEngine(
            build_engine_for=lambda conn: engines[conn.id], candidates=pool
        )
        self.assertEqual(_run(engine), {"hello": "D:hello"})

    def test_endpoint_contagion_matches_differently_written_urls(self) -> None:
        pool = _pool(
            ("A1", "https://a.example/v1"),
            ("A2", "https://a.example"),
            ("B", "https://b.example/v1"),
        )
        engines = {
            pool[0].id: _StubEngine("A1", _HttpError(503)),
            pool[1].id: _StubEngine("A2"),
            pool[2].id: _StubEngine("B"),
        }
        engine = FailoverTranslationEngine(
            build_engine_for=lambda conn: engines[conn.id], candidates=pool
        )
        self.assertEqual(_run(engine), {"hello": "B:hello"})
        self.assertEqual(engines[pool[1].id].calls, 0)

    def test_concurrent_failures_burn_only_the_connection_that_failed(self) -> None:
        pool = _pool(("A", "https://a.example/v1"), ("B", "https://b.example/v1"))
        barrier = threading.Barrier(2)

        class _BarrierFailEngine:
            def __init__(self) -> None:
                self.calls = 0

            def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
                self.calls += 1
                # Both threads must be inside A's request before either fails,
                # so both come back reporting a failure on A.
                barrier.wait(timeout=5)
                raise _HttpError(401)

        a_engine = _BarrierFailEngine()
        engines = {pool[0].id: a_engine, pool[1].id: _StubEngine("B")}
        engine = FailoverTranslationEngine(
            build_engine_for=lambda conn: engines[conn.id], candidates=pool
        )

        results: list[dict[str, str]] = []
        errors: list[BaseException] = []

        def _worker() -> None:
            try:
                results.append(_run(engine))
            except BaseException as exc:  # noqa: BLE001 - collected for the assert
                errors.append(exc)

        threads = [threading.Thread(target=_worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        # The second stale report on A must retry on B, not burn B as well.
        self.assertEqual(errors, [])
        self.assertEqual(results, [{"hello": "B:hello"}, {"hello": "B:hello"}])
        self.assertEqual(a_engine.calls, 2)
        self.assertEqual(engine.current_connection.label, "B")


class RoleEngineChainTests(unittest.TestCase):
    """build_role_engine must honour follow resolution and frozen keys."""

    def _settings_with_backup(self) -> tuple[AppSettings, list[str]]:
        settings = AppSettings()
        add_role_connection(
            settings,
            ROLE_TRANSLATION,
            label="备用厂商",
            provider="custom_openai",
            base_url="https://vendor-b.example/v1",
        )
        return settings, [conn.id for conn in settings.engine.connections]

    def test_a_following_role_resolves_its_chain_in_the_source_pool(self) -> None:
        settings, chain = self._settings_with_backup()
        # cleaner follows translation out of the box, so its task chain holds
        # translation-pool ids; resolving them in cleaner's own idle pool used
        # to degrade every entry to the primary and drop failover entirely.
        built: list[object] = []

        def _fake_build_engine(role_settings):
            built.append(role_settings)
            return _StubEngine("stub")

        with (
            patch("core.engine_dispatcher.build_engine", side_effect=_fake_build_engine),
            patch("core.model_roles.get_key", return_value="secret"),
            patch("core.model_roles.get_connection_scoped_key", return_value=""),
        ):
            engine = build_role_engine(settings, "cleaner", connection_ids=chain)
        self.assertIsInstance(engine, FailoverTranslationEngine)

    def test_runtime_switch_reuses_the_frozen_key_snapshot_on_worker_threads(self) -> None:
        settings, chain = self._settings_with_backup()
        overrides_seen: list[dict[str, str] | None] = []

        def _fake_build_engine(role_settings):
            overrides_seen.append(current_key_overrides())
            if role_settings.engine.cloud_base_url == "https://vendor-b.example/v1":
                return _StubEngine("B")
            return _StubEngine("A", _HttpError(401))

        with (
            patch("core.engine_dispatcher.build_engine", side_effect=_fake_build_engine),
            patch("core.model_roles.get_key", return_value="secret"),
            patch("core.model_roles.get_connection_scoped_key", return_value=""),
        ):
            with provider_key_overrides({"custom_openai": "frozen-key"}):
                engine = build_role_engine(
                    settings,
                    ROLE_TRANSLATION,
                    connection_ids=chain,
                )
            self.assertIsInstance(engine, FailoverTranslationEngine)

            # The switch happens on a pool worker thread that never entered
            # the task's provider_key_overrides context.
            outcome: dict[str, dict[str, str]] = {}
            worker = threading.Thread(target=lambda: outcome.update(value=_run(engine)))
            worker.start()
            worker.join(timeout=10)

        self.assertEqual(outcome.get("value"), {"hello": "B:hello"})
        self.assertEqual(
            overrides_seen,
            [{"custom_openai": "frozen-key"}, {"custom_openai": "frozen-key"}],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
