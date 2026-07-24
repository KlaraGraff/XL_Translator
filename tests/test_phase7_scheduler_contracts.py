"""Phase 7 contracts for multi-task scheduling, recovery, and privacy.

All tests use in-memory resource groups and isolated API fixtures.  They
intentionally exercise the public Phase 7 contracts rather than translation
providers, local Office automation, or a user's task history.
"""

from __future__ import annotations

from collections import deque
import json
import logging
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

import core.task_logger as task_logger_module
from api.app import create_app
from api.task_manager import TaskConflictError, TaskInputError, TaskOptions, TranslationTaskManager
from config import LOG_PATH
from core.api_scheduler import API_CONCURRENCY_ACTION_REDUCED
from core.model_api_identity import TaskApiContext
from core.task_history import TaskHistoryStore
from core.task_logger import TaskLogger
from core.task_resources import TaskResourceRegistry
from core.task_runner import DoneMsg, LogMsg
from settings import AppSettings


SHARED_TEXT_CONNECTION = (
    "cloud",
    "custom_openai",
    "https://shared.example/v1",
    "scoped-key-fingerprint",
)
OTHER_TEXT_CONNECTION = (
    "cloud",
    "custom_openai",
    "https://other.example/v1",
    "different-key-fingerprint",
)


class _HoldingRunner:
    """A no-network active runner whose lifecycle is controlled by the test."""

    def __init__(self, *, can_pause: bool = False) -> None:
        self.can_pause = can_pause
        self.pause_calls = 0
        self.resume_calls = 0
        self.stop_calls = 0

    def start(self) -> None:
        return None

    def stop(self) -> None:
        self.stop_calls += 1

    def pause(self) -> None:
        if not self.can_pause:
            raise AssertionError("only the PDF fixture should be paused")
        self.pause_calls += 1

    def resume(self) -> None:
        if not self.can_pause:
            raise AssertionError("only the PDF fixture should be resumed")
        self.resume_calls += 1

    def needs_poll(self) -> bool:
        return True

    def get_message(self, timeout: float = 0.05):
        time.sleep(min(timeout, 0.01))
        return None


class _MessageRunner:
    """Feeds a fixed finite message sequence into the manager's SSE bridge."""

    def __init__(self, messages: list[object]) -> None:
        self._messages = deque(messages)

    def start(self) -> None:
        return None

    def stop(self) -> None:
        self._messages.clear()

    def needs_poll(self) -> bool:
        return bool(self._messages)

    def get_message(self, timeout: float = 0.05):  # noqa: ARG002
        return self._messages.popleft() if self._messages else None


def _context_for(surface: str, *, group=SHARED_TEXT_CONNECTION, capacity: int = 2) -> TaskApiContext:
    roles = {
        "excel": ("translation",),
        "word": ("translation",),
        "pdf": ("image", "pdf_review"),
        "tm_clean": ("cleaner",),
    }[surface]
    snapshot = {
        role: {
            "role": role,
            "connection_id": "anonymous-shared-connection",
            "mode": "cloud",
            "provider": "custom_openai",
            "base_url": "https://shared.example/v1",
            "throughput": {"concurrency": capacity},
        }
        for role in roles
    }
    return TaskApiContext(
        api_groups=frozenset({group}),
        key_overrides={"scope": "fixture-api-key-must-not-leak"},
        model_snapshot=snapshot,
        role_groups={role: group for role in roles},
        group_concurrency={group: capacity},
    )


def _phase7_manager(root: Path, *, runners: list[object]) -> TranslationTaskManager:
    """Build an isolated manager without scanner, key, TM, or model traffic."""
    manager = TranslationTaskManager(
        settings_loader=AppSettings,
        history_store=TaskHistoryStore(root / "task_history.json"),
    )

    def scan(_root, surface, _options):
        suffix = {"excel": ".xlsx", "word": ".docx", "pdf": ".pdf"}[surface]
        return [
            SimpleNamespace(
                path=root / f"fixture{suffix}",
                name=f"fixture{suffix}",
                format=suffix.lstrip("."),
                source_type="pdf" if surface == "pdf" else "",
            )
        ]

    def build_runner(*, surface, **_kwargs):
        runner = _HoldingRunner(can_pause=surface == "pdf")
        runners.append(runner)
        return runner

    def build_clean_runner(**_kwargs):
        runner = _HoldingRunner()
        runners.append(runner)
        return runner

    manager._scan = scan
    manager._build_runner = build_runner
    manager._build_clean_runner = build_clean_runner
    manager._validate_excel_preflight = lambda **_kwargs: None
    manager._validate_word_preflight = lambda **_kwargs: None
    manager._validate_pdf_preflight = lambda **_kwargs: None
    return manager


def _context_side_effect(_settings: AppSettings, page: str) -> TaskApiContext:
    return _context_for(
        {
            "excel_translate": "excel",
            "word_translate": "word",
            "pdf_translate": "pdf",
            "tm_clean": "tm_clean",
        }[page],
        capacity=5 if page == "pdf_translate" else 2,
    )


class Phase7ResourceBudgetContracts(unittest.TestCase):
    """S7A-01/06/08/09/10/11/12 without external model traffic."""

    def test_different_types_share_a_group_but_same_type_is_rejected(self) -> None:
        registry = TaskResourceRegistry()
        excel = registry.reserve_task(
            owner_key="excel-1",
            owner_label="Excel translation",
            task_type="excel",
            group_capacities={SHARED_TEXT_CONNECTION: 2},
        )
        self.assertIsNotNone(excel.lease)

        same_type = registry.reserve_task(
            owner_key="excel-2",
            owner_label="Excel translation",
            task_type="excel",
            group_capacities={SHARED_TEXT_CONNECTION: 3},
        )
        self.assertIsNone(same_type.lease)
        self.assertEqual(same_type.reason, "surface_busy")

        word = registry.reserve_task(
            owner_key="word-1",
            owner_label="Word translation",
            task_type="word",
            group_capacities={SHARED_TEXT_CONNECTION: 3},
        )
        self.assertIsNotNone(word.lease)
        assert excel.lease is not None
        assert word.lease is not None
        excel_group = excel.lease.scheduler_for(SHARED_TEXT_CONNECTION)
        word_group = word.lease.scheduler_for(SHARED_TEXT_CONNECTION)
        self.assertIsNotNone(excel_group)
        self.assertIsNotNone(word_group)
        assert excel_group is not None
        assert word_group is not None
        self.assertEqual(excel_group.snapshot().capacity, 5)
        self.assertEqual(word_group.snapshot().capacity, 5)

    def test_risk_view_explains_shared_capacity_without_exposing_key_material(self) -> None:
        registry = TaskResourceRegistry()
        reserved = registry.reserve_task(
            owner_key="excel-1",
            owner_label="Excel translation",
            task_type="excel",
            group_capacities={SHARED_TEXT_CONNECTION: 2},
        )
        self.assertIsNotNone(reserved.lease)

        risk = registry.scheduling_risk(
            task_type="word",
            group_capacities={SHARED_TEXT_CONNECTION: 3},
        )

        self.assertFalse(risk["surface_busy"])
        self.assertEqual(risk["revision"], 1)
        shared_groups = risk["shared_groups"]
        self.assertEqual(len(shared_groups), 1)
        group = shared_groups[0]
        self.assertEqual(group["active_capacity"], 2)
        self.assertEqual(group["candidate_capacity"], 3)
        self.assertEqual(group["total_potential_capacity"], 5)
        self.assertNotIn("api_key", repr(risk).lower())
        self.assertNotIn("secret", repr(risk).lower())

    def test_pdf_two_roles_on_one_connection_contribute_one_combined_budget(self) -> None:
        registry = TaskResourceRegistry()
        # Image generation is configured for two requests and PDF review for
        # three.  They resolve to one actual connection, so the task reserves
        # five slots, not two independent uncoordinated pools.
        pdf = registry.reserve_task(
            owner_key="pdf-1",
            owner_label="PDF/image translation",
            task_type="pdf",
            group_capacities={SHARED_TEXT_CONNECTION: 5},
        )
        self.assertIsNotNone(pdf.lease)

        risk = registry.scheduling_risk(
            task_type="tm_clean",
            group_capacities={SHARED_TEXT_CONNECTION: 2},
        )
        group = risk["shared_groups"][0]
        self.assertEqual(group["active_capacity"], 5)
        self.assertEqual(group["candidate_capacity"], 2)
        self.assertEqual(group["total_potential_capacity"], 7)

    def test_tm_cleaning_is_a_fourth_type_and_participates_in_shared_budget(self) -> None:
        registry = TaskResourceRegistry()
        cleaning = registry.reserve_task(
            owner_key="clean-1",
            owner_label="TM cleaning",
            task_type="tm_clean",
            group_capacities={SHARED_TEXT_CONNECTION: 1},
        )
        self.assertIsNotNone(cleaning.lease)

        duplicate_cleaning = registry.reserve_task(
            owner_key="clean-2",
            owner_label="TM cleaning",
            task_type="tm_clean",
            group_capacities={SHARED_TEXT_CONNECTION: 1},
        )
        self.assertIsNone(duplicate_cleaning.lease)
        self.assertEqual(duplicate_cleaning.reason, "surface_busy")

        translation = registry.reserve_task(
            owner_key="excel-1",
            owner_label="Excel translation",
            task_type="excel",
            group_capacities={SHARED_TEXT_CONNECTION: 2},
        )
        self.assertIsNotNone(translation.lease)
        assert translation.lease is not None
        group = translation.lease.scheduler_for(SHARED_TEXT_CONNECTION)
        self.assertIsNotNone(group)
        assert group is not None
        self.assertEqual(group.snapshot().capacity, 3)

    def test_429_reduces_only_the_live_shared_group_and_never_persists_after_release(self) -> None:
        registry = TaskResourceRegistry()
        first = registry.reserve_task(
            owner_key="excel-1",
            owner_label="Excel translation",
            task_type="excel",
            group_capacities={SHARED_TEXT_CONNECTION: 3},
        )
        second = registry.reserve_task(
            owner_key="word-1",
            owner_label="Word translation",
            task_type="word",
            group_capacities={SHARED_TEXT_CONNECTION: 2},
        )
        self.assertIsNotNone(first.lease)
        self.assertIsNotNone(second.lease)
        assert first.lease is not None
        assert second.lease is not None
        first_scheduler = first.lease.scheduler_for(SHARED_TEXT_CONNECTION)
        second_scheduler = second.lease.scheduler_for(SHARED_TEXT_CONNECTION)
        self.assertIsNotNone(first_scheduler)
        self.assertIsNotNone(second_scheduler)
        assert first_scheduler is not None
        assert second_scheduler is not None
        self.assertEqual(first_scheduler.snapshot().capacity, 5)

        decision = first_scheduler.register_concurrency_limit_hit(None)
        self.assertEqual(decision.action, API_CONCURRENCY_ACTION_REDUCED)
        self.assertLess(decision.current_capacity, 5)
        self.assertEqual(second_scheduler.snapshot().capacity, decision.current_capacity)

        # A different key/base URL is not affected by this group's temporary
        # 429 backoff.
        isolated = registry.reserve_task(
            owner_key="pdf-1",
            owner_label="PDF/image translation",
            task_type="pdf",
            group_capacities={OTHER_TEXT_CONNECTION: 4},
        )
        self.assertIsNotNone(isolated.lease)
        assert isolated.lease is not None
        isolated_scheduler = isolated.lease.scheduler_for(OTHER_TEXT_CONNECTION)
        self.assertIsNotNone(isolated_scheduler)
        assert isolated_scheduler is not None
        self.assertEqual(isolated_scheduler.snapshot().capacity, 4)

        first.lease.release()
        second.lease.release()
        new_group = registry.reserve_task(
            owner_key="clean-1",
            owner_label="TM cleaning",
            task_type="tm_clean",
            group_capacities={SHARED_TEXT_CONNECTION: 3},
        )
        self.assertIsNotNone(new_group.lease)
        assert new_group.lease is not None
        new_scheduler = new_group.lease.scheduler_for(SHARED_TEXT_CONNECTION)
        self.assertIsNotNone(new_scheduler)
        assert new_scheduler is not None
        self.assertEqual(new_scheduler.snapshot().capacity, 3)

    def test_waiting_tasks_receive_slots_in_fifo_order(self) -> None:
        registry = TaskResourceRegistry()
        first = registry.reserve_task(
            owner_key="excel-1",
            owner_label="Excel translation",
            task_type="excel",
            group_capacities={SHARED_TEXT_CONNECTION: 2},
        )
        second = registry.reserve_task(
            owner_key="word-1",
            owner_label="Word translation",
            task_type="word",
            group_capacities={SHARED_TEXT_CONNECTION: 2},
        )
        self.assertIsNotNone(first.lease)
        self.assertIsNotNone(second.lease)
        assert first.lease is not None
        assert second.lease is not None
        first_scheduler = first.lease.scheduler_for(SHARED_TEXT_CONNECTION)
        second_scheduler = second.lease.scheduler_for(SHARED_TEXT_CONNECTION)
        self.assertIsNotNone(first_scheduler)
        self.assertIsNotNone(second_scheduler)
        assert first_scheduler is not None
        assert second_scheduler is not None

        held = first_scheduler.acquire_lease(4)
        acquired: list[str] = []
        second_waiting = threading.Event()
        first_waiting = threading.Event()

        def wait_for_second() -> None:
            second_waiting.set()
            lease = second_scheduler.acquire_lease(1)
            acquired.append("word")
            second_scheduler.release(lease)

        def wait_for_first() -> None:
            first_waiting.set()
            lease = first_scheduler.acquire_lease(1)
            acquired.append("excel")
            first_scheduler.release(lease)

        word_thread = threading.Thread(target=wait_for_second)
        excel_thread = threading.Thread(target=wait_for_first)
        word_thread.start()
        self.assertTrue(second_waiting.wait(1))
        # Let the first waiter enter the scheduler after the Word task.
        time.sleep(0.03)
        excel_thread.start()
        self.assertTrue(first_waiting.wait(1))
        time.sleep(0.03)
        first_scheduler.release(held)
        word_thread.join(2)
        excel_thread.join(2)

        self.assertFalse(word_thread.is_alive())
        self.assertFalse(excel_thread.is_alive())
        self.assertEqual(acquired, ["word", "excel"])


class Phase7SidecarRestartContracts(unittest.TestCase):
    """S7B-09: a new sidecar cannot resurrect prior in-memory runners."""

    def test_restart_marks_only_previously_active_records_interrupted(self) -> None:
        with TemporaryDirectory() as temporary:
            history = TaskHistoryStore(Path(temporary) / "task_history.json")
            history.upsert(
                {
                    "task_id": "excel-active",
                    "surface": "excel",
                    "state": "running",
                    "terminal": False,
                    "task_snapshot": {"connection": "anonymous"},
                }
            )
            history.upsert(
                {
                    "task_id": "pdf-paused",
                    "surface": "pdf",
                    "state": "paused",
                    "terminal": False,
                    "task_snapshot": {"connection": "anonymous"},
                }
            )
            history.upsert(
                {
                    "task_id": "word-done",
                    "surface": "word",
                    "state": "done",
                    "terminal": True,
                }
            )

            changed = set(history.mark_active_interrupted())
            records = {item["task_id"]: item for item in history.records()}

            self.assertEqual(changed, {"excel-active", "pdf-paused"})
            for task_id in changed:
                record = records[task_id]
                self.assertEqual(record["state"], "interrupted")
                self.assertTrue(record["terminal"])
                self.assertTrue(record["interrupted"])
                self.assertEqual(
                    record["recovery"],
                    {"can_resume": False, "reason": "sidecar_restarted"},
                )
            self.assertEqual(records["word-done"]["state"], "done")
            self.assertNotIn("recovery", records["word-done"])


class Phase7ManagedTaskContracts(unittest.TestCase):
    """Manager-level S7A/S7B contracts with isolated fake runners."""

    def test_shared_connection_needs_one_time_token_and_stale_tokens_reject_races(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            runners: list[object] = []
            manager = _phase7_manager(root, runners=runners)
            with (
                patch("api.task_manager.task_api_context_for_page", side_effect=_context_side_effect),
                patch("api.task_manager.tm_manager.init_db"),
                patch("api.task_manager.threading.Thread"),
            ):
                excel = manager.start_task(surface="excel", source_path=str(root))
                word_preflight = manager.preflight_task(surface="word", source_path=str(root))
                self.assertTrue(word_preflight["requires_confirmation"])
                self.assertIn("confirmation_token", word_preflight)
                risk_dump = json.dumps(word_preflight, ensure_ascii=False)
                self.assertNotIn("fixture-api-key-must-not-leak", risk_dump)
                self.assertIn("429", risk_dump)

                with self.assertRaises(TaskConflictError) as missing:
                    manager.start_task(surface="word", source_path=str(root))
                self.assertEqual(missing.exception.reason, "confirmation_required")

                word = manager.start_task(
                    surface="word",
                    source_path=str(root),
                    confirmation_token=word_preflight["confirmation_token"],
                )
                self.assertEqual(word["surface"], "word")
                with self.assertRaises(TaskConflictError) as consumed:
                    manager.start_task(
                        surface="word",
                        source_path=str(root),
                        confirmation_token=word_preflight["confirmation_token"],
                    )
                self.assertEqual(consumed.exception.reason, "expired_or_consumed")

                # A token from the current resource revision becomes stale as
                # soon as another eligible task joins the shared group.
                pdf_preflight = manager.preflight_task(surface="pdf", source_path=str(root))
                clean_preflight = manager.preflight_task(
                    surface="tm_clean",
                    options=TaskOptions(lang_pair="zh-en"),
                )
                manager.start_task(
                    surface="pdf",
                    source_path=str(root),
                    confirmation_token=pdf_preflight["confirmation_token"],
                )
                with self.assertRaises(TaskConflictError) as stale:
                    manager.start_task(
                        surface="tm_clean",
                        options=TaskOptions(lang_pair="zh-en"),
                        confirmation_token=clean_preflight["confirmation_token"],
                    )
                self.assertEqual(stale.exception.reason, "stale")
                self.assertEqual(excel["surface"], "excel")

    def test_tm_cleaning_is_managed_and_pdf_pause_keeps_its_resource_slot(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            runners: list[object] = []
            manager = _phase7_manager(root, runners=runners)
            with (
                patch("api.task_manager.task_api_context_for_page", side_effect=_context_side_effect),
                patch("api.task_manager.tm_manager.init_db"),
                patch("api.task_manager.threading.Thread"),
            ):
                pdf = manager.start_task(surface="pdf", source_path=str(root))
                paused = manager.pause_task(pdf["task_id"])
                self.assertEqual(paused["state"], "paused")
                active = manager.list_tasks()["active"]
                self.assertEqual({item["task_id"] for item in active}, {pdf["task_id"]})
                self.assertEqual(active[0]["state"], "paused")

                with self.assertRaises(TaskConflictError) as duplicate_pdf:
                    manager.preflight_task(surface="pdf", source_path=str(root))
                self.assertEqual(duplicate_pdf.exception.reason, "surface_busy")

                clean = manager.preflight_task(
                    surface="tm_clean",
                    options=TaskOptions(lang_pair="zh-en"),
                )
                self.assertTrue(clean["requires_confirmation"])
                started_clean = manager.start_task(
                    surface="tm_clean",
                    options=TaskOptions(lang_pair="zh-en"),
                    confirmation_token=clean["confirmation_token"],
                )
                self.assertEqual(started_clean["surface"], "tm_clean")
                with self.assertRaises(TaskConflictError) as duplicate_clean:
                    manager.preflight_task(
                        surface="tm_clean",
                        options=TaskOptions(lang_pair="zh-en"),
                    )
                self.assertEqual(duplicate_clean.exception.reason, "surface_busy")

    def test_hard_restart_interrupts_live_pdf_and_releases_its_same_type_slot(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            runners: list[object] = []
            manager = _phase7_manager(root, runners=runners)
            with (
                patch("api.task_manager.task_api_context_for_page", side_effect=_context_side_effect),
                patch("api.task_manager.tm_manager.init_db"),
                patch("api.task_manager.threading.Thread"),
            ):
                started = manager.start_task(surface="pdf", source_path=str(root))
                manager.pause_task(started["task_id"])
                self.assertEqual(manager.mark_active_tasks_interrupted(), [started["task_id"]])
                interrupted = manager.task_status(started["task_id"])
                self.assertEqual(interrupted["state"], "interrupted")
                self.assertTrue(interrupted["terminal"])
                self.assertFalse(interrupted["result"]["recovery"]["can_resume"])
                with self.assertRaises(TaskInputError):
                    manager.resume_task(started["task_id"])

                # The old PDF pause is terminal, so a fresh task can reserve
                # the PDF type.  It is never silently resumed.
                replacement = manager.preflight_task(surface="pdf", source_path=str(root))
                self.assertFalse(replacement["requires_confirmation"])


class Phase7SseAndPrivacyContracts(unittest.TestCase):
    """S7B-04/05 and S7B-10/S7C-04/05 under a finite mock runner."""

    def test_sse_replays_only_events_after_last_id_and_results_are_redacted(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_secret = "never expose source text: concrete quantity"
            provider_secret = "api_key=live-secret-value"
            absolute_source = "/private/input/source.xlsx"
            message_runner = _MessageRunner(
                [
                    LogMsg(level="INFO", message=f"{source_secret}; {provider_secret}; {absolute_source}"),
                    DoneMsg(
                        output_dir=str(root / "output"),
                        report_path=str(root / "output" / "report.md"),
                        file_results=[
                            {
                                "source_path": absolute_source,
                                "source_text": source_secret,
                                "target_text": "translated secret text",
                            }
                        ],
                        elapsed_sec=0.1,
                        tm_hit_count=0,
                        api_call_count=1,
                        error={"raw_response": "complete provider response"},
                    ),
                ]
            )
            manager = TranslationTaskManager(
                settings_loader=AppSettings,
                history_store=TaskHistoryStore(root / "task_history.json"),
            )
            manager._scan = lambda *_args: [
                SimpleNamespace(path=root / "fixture.xlsx", name="fixture.xlsx", format="xlsx")
            ]
            manager._validate_excel_preflight = lambda **_kwargs: None
            manager._build_runner = lambda **_kwargs: message_runner

            with (
                patch("api.task_manager.task_api_context_for_page", side_effect=_context_side_effect),
                patch("api.task_manager.tm_manager.init_db"),
            ):
                started = manager.start_task(surface="excel", source_path=str(root))
                deadline = time.monotonic() + 2
                status = manager.task_status(started["task_id"])
                while not status["terminal"] and time.monotonic() < deadline:
                    time.sleep(0.01)
                    status = manager.task_status(started["task_id"])

                replay = list(manager.iter_sse(started["task_id"], after_event_id=0))
                resumed = list(manager.iter_sse(started["task_id"], after_event_id=1))

            self.assertTrue(status["terminal"])
            ids = [int(chunk.split("\n", 1)[0].split(":", 1)[1].strip()) for chunk in replay]
            resumed_ids = [int(chunk.split("\n", 1)[0].split(":", 1)[1].strip()) for chunk in resumed]
            self.assertEqual(ids, sorted(ids))
            self.assertEqual(ids, list(range(1, len(ids) + 1)))
            self.assertTrue(all(item > 1 for item in resumed_ids))
            self.assertEqual(resumed_ids, ids[1:])

            delivered = json.dumps({"status": status, "events": replay}, ensure_ascii=False)
            for sensitive in (
                source_secret,
                provider_secret,
                absolute_source,
                "translated secret text",
                "complete provider response",
                "fixture-api-key-must-not-leak",
            ):
                self.assertNotIn(sensitive, delivered)
            result = manager.task_results(started["task_id"])["result"]
            self.assertEqual(
                [item["action"] for item in result["local_operations"]],
                ["open_output", "open_report", "copy_output_path"],
            )

    def test_persistent_task_log_does_not_write_sensitive_runner_details(self) -> None:
        root_logger = logging.getLogger(task_logger_module._LOGGER_NAME)
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)
            handler.close()
        task_logger_module._handler_installed = False
        LOG_PATH.unlink(missing_ok=True)

        logger = TaskLogger(enabled=True, task_id="phase7-log-sanitization")
        logger.info("source_text=private source sentence")
        logger.warning("Authorization: Bearer real-secret-token /private/input.docx")
        logger.error("raw_response=private provider response", exc_info=True)
        for handler in logging.getLogger(task_logger_module._LOGGER_NAME).handlers:
            handler.flush()

        persisted = LOG_PATH.read_text(encoding="utf-8")
        for sensitive in (
            "private source sentence",
            "real-secret-token",
            "/private/input.docx",
            "private provider response",
        ):
            self.assertNotIn(sensitive, persisted)
        self.assertIn("正文、译文、提示词和模型响应未写入日志", persisted)
        self.assertIn("原始错误详情未写入日志", persisted)


class Phase7HttpContracts(unittest.TestCase):
    """The sidecar routes preserve the manager contracts without real services."""

    def test_preflight_token_tm_clean_wrapper_and_task_center_routes(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            runners: list[object] = []
            manager = _phase7_manager(root, runners=runners)
            client = TestClient(create_app(task_manager=manager))
            with (
                patch("api.task_manager.task_api_context_for_page", side_effect=_context_side_effect),
                patch("api.task_manager.tm_manager.init_db"),
            ):
                excel = client.post(
                    "/api/tasks",
                    json={"surface": "excel", "source_path": str(root)},
                )
                self.assertEqual(excel.status_code, 202, excel.text)
                preflight = client.post(
                    "/api/tasks/preflight",
                    json={"surface": "word", "source_path": str(root)},
                )
                self.assertEqual(preflight.status_code, 200, preflight.text)
                payload = preflight.json()
                self.assertTrue(payload["requires_confirmation"])
                self.assertIn("confirmation_token", payload)
                self.assertNotIn("fixture-api-key-must-not-leak", preflight.text)
                self.assertIn("shared_connections", payload["risk"])

                missing_token = client.post(
                    "/api/tasks",
                    json={"surface": "word", "source_path": str(root)},
                )
                self.assertEqual(missing_token.status_code, 409, missing_token.text)
                self.assertEqual(missing_token.json()["reason"], "confirmation_required")

                started_word = client.post(
                    "/api/tasks",
                    json={
                        "surface": "word",
                        "source_path": str(root),
                        "confirmation_token": payload["confirmation_token"],
                    },
                )
                self.assertEqual(started_word.status_code, 202, started_word.text)
                token_replay = client.post(
                    "/api/tasks",
                    json={
                        "surface": "word",
                        "source_path": str(root),
                        "confirmation_token": payload["confirmation_token"],
                    },
                )
                self.assertEqual(token_replay.status_code, 409, token_replay.text)
                self.assertEqual(token_replay.json()["reason"], "expired_or_consumed")

                tm_preflight = client.post(
                    "/api/tasks/preflight",
                    json={"surface": "tm_clean", "lang_pair": "zh-en"},
                )
                self.assertEqual(tm_preflight.status_code, 200, tm_preflight.text)
                clean = client.post(
                    "/api/tm/clean",
                    json={
                        "lang_pair": "zh-en",
                        "confirmation_token": tm_preflight.json()["confirmation_token"],
                    },
                )
                self.assertEqual(clean.status_code, 202, clean.text)
                self.assertEqual(clean.json()["surface"], "tm_clean")

                tasks = client.get("/api/tasks")
                self.assertEqual(tasks.status_code, 200, tasks.text)
                active = tasks.json()["active"]
                self.assertEqual({item["surface"] for item in active}, {"excel", "word", "tm_clean"})
                self.assertTrue(all("source_path" not in item for item in active))
                resources = client.get("/api/tasks/resources")
                self.assertEqual(resources.status_code, 200, resources.text)
                self.assertEqual(len(resources.json()["groups"]), 1)

    def test_sse_last_event_id_and_result_operations_work_through_http(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            message_runner = _MessageRunner(
                [
                    LogMsg(level="INFO", message="source phrase must not reach SSE"),
                    DoneMsg(
                        output_dir=str(root / "output"),
                        report_path=str(root / "output" / "report.md"),
                        file_results=[],
                        elapsed_sec=0.1,
                        tm_hit_count=0,
                        api_call_count=1,
                    ),
                ]
            )
            manager = TranslationTaskManager(
                settings_loader=AppSettings,
                history_store=TaskHistoryStore(root / "task_history.json"),
            )
            manager._scan = lambda *_args: [
                SimpleNamespace(path=root / "fixture.xlsx", name="fixture.xlsx", format="xlsx")
            ]
            manager._validate_excel_preflight = lambda **_kwargs: None
            manager._build_runner = lambda **_kwargs: message_runner
            client = TestClient(create_app(task_manager=manager))
            with (
                patch("api.task_manager.task_api_context_for_page", side_effect=_context_side_effect),
                patch("api.task_manager.tm_manager.init_db"),
            ):
                started = client.post(
                    "/api/tasks",
                    json={"surface": "excel", "source_path": str(root)},
                )
                self.assertEqual(started.status_code, 202, started.text)
                task_id = started.json()["task_id"]
                deadline = time.monotonic() + 2
                status = client.get(f"/api/tasks/{task_id}")
                while not status.json()["terminal"] and time.monotonic() < deadline:
                    time.sleep(0.01)
                    status = client.get(f"/api/tasks/{task_id}")
                first_stream = client.get(f"/api/tasks/{task_id}/events")
                resumed_stream = client.get(
                    f"/api/tasks/{task_id}/events",
                    headers={"Last-Event-ID": "1"},
                )
                result = client.get(f"/api/tasks/{task_id}/results")

            self.assertTrue(status.json()["terminal"])
            self.assertEqual(first_stream.status_code, 200, first_stream.text)
            self.assertEqual(resumed_stream.status_code, 200, resumed_stream.text)
            first_ids = [
                int(line.split(":", 1)[1].strip())
                for line in first_stream.text.splitlines()
                if line.startswith("id:")
            ]
            resumed_ids = [
                int(line.split(":", 1)[1].strip())
                for line in resumed_stream.text.splitlines()
                if line.startswith("id:")
            ]
            self.assertEqual(first_ids, [1, 2, 3])
            self.assertEqual(resumed_ids, [2, 3])
            self.assertNotIn("source phrase must not reach SSE", first_stream.text)
            self.assertEqual(result.status_code, 200, result.text)
            actions = [item["action"] for item in result.json()["result"]["local_operations"]]
            self.assertEqual(actions, ["open_output", "open_report", "copy_output_path"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
