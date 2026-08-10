"""Runtime failover behaviour of the wrapping translation engine."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import settings as settings_module
from core.engine_dispatcher import build_role_engine
from core.failover_engine import (
    FailoverTranslationEngine,
    concrete_base_engine_members,
)
from core.language_preflight import TranslationLanguageResult
from core.model_api_identity import task_api_context_for_page
from core.model_roles import ROLE_TRANSLATION, add_role_connection
from engines.base_engine import TranslationEngine
from settings import (
    AppSettings,
    ModelConnection,
    connection_key_scope,
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
        self.chat_calls: list[tuple[str, str]] = []
        self.source_calls: list[list[str]] = []

    def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return {text: f"{self.label}:{text}" for text in texts}

    def chat(self, system: str, user: str) -> str:
        self.calls += 1
        self.chat_calls.append((system, user))
        if self.error is not None:
            raise self.error
        return f"{self.label}:chat"

    def translate_batch_with_sources(
        self, texts, target_lang, system_prompt, source_lang="auto"
    ):
        self.calls += 1
        self.source_calls.append(list(texts))
        if self.error is not None:
            raise self.error
        return [
            TranslationLanguageResult(
                text,
                f"{self.label}:{text}",
                source_lang="en",
                target_lang=target_lang,
            )
            for text in texts
        ]


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

    def test_chat_reaches_the_live_engine_instead_of_the_base_stub(self) -> None:
        # ``chat`` has a body on ``TranslationEngine``, so ordinary attribute
        # lookup found it before ``__getattr__`` could delegate and the wrapper
        # answered every call with NotImplementedError.
        pool = _pool(("A", "https://a.example/v1"), ("B", "https://b.example/v1"))
        engines = {pool[0].id: _StubEngine("A"), pool[1].id: _StubEngine("B")}
        engine = FailoverTranslationEngine(
            build_engine_for=lambda conn: engines[conn.id], candidates=pool
        )

        self.assertEqual(engine.chat("system", "user"), "A:chat")
        self.assertEqual(engines[pool[0].id].chat_calls, [("system", "user")])

    def test_chat_moves_to_the_next_connection_when_one_dies(self) -> None:
        pool = _pool(("A", "https://a.example/v1"), ("B", "https://b.example/v1"))
        engines = {
            pool[0].id: _StubEngine("A", _HttpError(401)),
            pool[1].id: _StubEngine("B"),
        }
        engine = FailoverTranslationEngine(
            build_engine_for=lambda conn: engines[conn.id], candidates=pool
        )

        self.assertEqual(engine.chat("system", "user"), "B:chat")
        self.assertEqual(engine.current_connection.label, "B")

    def test_chat_rate_limiting_stays_with_the_caller(self) -> None:
        pool = _pool(("A", "https://a.example/v1"), ("B", "https://b.example/v1"))
        engines = {
            pool[0].id: _StubEngine("A", _HttpError(429)),
            pool[1].id: _StubEngine("B"),
        }
        engine = FailoverTranslationEngine(
            build_engine_for=lambda conn: engines[conn.id], candidates=pool
        )

        with self.assertRaises(_HttpError):
            engine.chat("system", "user")
        self.assertEqual(engines[pool[1].id].calls, 0)
        self.assertEqual(engine.current_connection.label, "A")

    def test_translate_batch_with_sources_reaches_the_live_engine(self) -> None:
        # The automatic-source-language Excel path calls only this method; when
        # it hit the base class the fallback returned source text as translation.
        pool = _pool(("A", "https://a.example/v1"), ("B", "https://b.example/v1"))
        engines = {pool[0].id: _StubEngine("A"), pool[1].id: _StubEngine("B")}
        engine = FailoverTranslationEngine(
            build_engine_for=lambda conn: engines[conn.id], candidates=pool
        )

        items = engine.translate_batch_with_sources(["hello"], "en", "prompt")

        self.assertEqual([item.translation for item in items], ["A:hello"])
        self.assertEqual([item.source_lang for item in items], ["en"])
        self.assertEqual(engines[pool[0].id].source_calls, [["hello"]])

    def test_translate_batch_with_sources_switches_on_a_dead_connection(self) -> None:
        pool = _pool(("A", "https://a.example/v1"), ("B", "https://b.example/v1"))
        engines = {
            pool[0].id: _StubEngine("A", _HttpError(401)),
            pool[1].id: _StubEngine("B"),
        }
        engine = FailoverTranslationEngine(
            build_engine_for=lambda conn: engines[conn.id], candidates=pool
        )

        items = engine.translate_batch_with_sources(["hello"], "en", "prompt")

        self.assertEqual([item.translation for item in items], ["B:hello"])
        self.assertEqual(engine.current_connection.label, "B")

    def test_every_concrete_base_member_is_reimplemented_on_the_wrapper(self) -> None:
        # ``__getattr__`` cannot see through a base-class body, so any concrete
        # member added to ``TranslationEngine`` later must also be overridden
        # here or it silently stops reaching the real engine.
        missing = [
            name
            for name in concrete_base_engine_members()
            if name not in vars(FailoverTranslationEngine)
        ]
        self.assertEqual(missing, [])
        self.assertEqual(
            concrete_base_engine_members(),
            frozenset({"chat", "translate_batch_with_sources", "engine_name"}),
        )

    def test_the_wrapper_reports_chat_capability_like_a_real_engine(self) -> None:
        # ``_engine_supports_chat`` in mixed_language / word_task_runner probes
        # the class, so a wrapper that left ``chat`` on the base silently
        # disabled mixed-language handling and semantic review.
        from core.mixed_language import _engine_supports_chat

        pool = _pool(("A", "https://a.example/v1"), ("B", "https://b.example/v1"))
        engines = {pool[0].id: _StubEngine("A"), pool[1].id: _StubEngine("B")}
        engine = FailoverTranslationEngine(
            build_engine_for=lambda conn: engines[conn.id], candidates=pool
        )

        self.assertIsNot(FailoverTranslationEngine.chat, TranslationEngine.chat)
        self.assertTrue(_engine_supports_chat(engine))

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


class _IsolatedKeyStore(unittest.TestCase):
    """Point the key store at a throwaway directory for the whole test."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for patcher in (
            patch.object(settings_module, "APP_DATA_DIR", root),
            patch.object(settings_module, "KEYS_PATH", root / "keys.json"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)


class RoleEngineChainTests(_IsolatedKeyStore):
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

        with patch("core.engine_dispatcher.build_engine", side_effect=_fake_build_engine):
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

        # No key patches: the key store is a throwaway empty directory, so the
        # only credential in play is the frozen one — which is the point.
        with patch("core.engine_dispatcher.build_engine", side_effect=_fake_build_engine):
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
        # Each build layers the candidate's own resolved key on top of the
        # snapshot, so the map is no longer the snapshot verbatim — what matters
        # is that the frozen entry survived onto the worker thread both times.
        self.assertEqual(len(overrides_seen), 2)
        for overrides in overrides_seen:
            self.assertEqual((overrides or {}).get("custom_openai"), "frozen-key")


class FailoverCredentialTests(_IsolatedKeyStore):
    """两条连接可以是同一家服务、同一个 Base URL，但用两个账号的 Key。

    这正是「一个账号额度用完了就切到另一个账号」的配置。切换后如果还拿着上一个
    账号的 Key 去拨，换来的只会是同一个拒绝——备用连接等于形同虚设。
    """

    def _two_accounts_on_one_endpoint(self) -> tuple[AppSettings, list[str]]:
        settings = AppSettings()
        settings.engine.mode = "cloud"
        settings.engine.cloud_provider = "custom_openai"
        settings.engine.cloud_base_url = "https://vendor.example/v1"
        settings.engine.cloud_model = "vendor-model"
        settings = AppSettings(**settings.model_dump())
        add_role_connection(
            settings,
            ROLE_TRANSLATION,
            label="账号 B",
            provider="custom_openai",
            model="vendor-model",
            base_url="https://vendor.example/v1",
        )
        settings = AppSettings(**settings.model_dump())
        ids = [conn.id for conn in settings.engine.connections]
        settings_module.save_connection_key(ids[0], "sk-ACCOUNT-A")
        settings_module.save_connection_key(ids[1], "sk-ACCOUNT-B")
        return settings, ids

    def test_the_task_snapshot_carries_every_candidates_key(self) -> None:
        settings, ids = self._two_accounts_on_one_endpoint()

        context = task_api_context_for_page(settings, "excel_translate")

        self.assertEqual(list(context.role_connection_chains[ROLE_TRANSLATION]), ids)
        self.assertEqual(
            context.key_overrides.get(connection_key_scope(ids[1])),
            "sk-ACCOUNT-B",
        )

    def test_a_switch_dials_the_second_account_with_its_own_key(self) -> None:
        settings, ids = self._two_accounts_on_one_endpoint()
        context = task_api_context_for_page(settings, "excel_translate")
        chain = context.role_connection_chains[ROLE_TRANSLATION]
        keys_seen: list[str] = []

        def _fake_build_engine(role_settings):
            engine_settings = role_settings.engine
            # What the real engine constructors ask for, on the thread and in
            # the override context the build actually happens in.
            keys_seen.append(
                settings_module.get_key(
                    engine_settings.cloud_provider,
                    engine_settings.cloud_base_url,
                )
            )
            if len(keys_seen) == 1:
                return _StubEngine("A", _HttpError(401))
            return _StubEngine("B")

        with patch("core.engine_dispatcher.build_engine", side_effect=_fake_build_engine):
            with provider_key_overrides(context.key_overrides):
                engine = build_role_engine(
                    settings,
                    ROLE_TRANSLATION,
                    connection_ids=chain,
                )
            # The switch builds on a pool worker thread, outside the snapshot.
            outcome: dict[str, dict[str, str]] = {}
            worker = threading.Thread(target=lambda: outcome.update(value=_run(engine)))
            worker.start()
            worker.join(timeout=10)

        self.assertEqual(outcome.get("value"), {"hello": "B:hello"})
        self.assertEqual(keys_seen, ["sk-ACCOUNT-A", "sk-ACCOUNT-B"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
