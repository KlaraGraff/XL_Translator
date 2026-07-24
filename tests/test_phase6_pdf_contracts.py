"""Phase 6 PDF/image route contracts executable without real model credentials.

These tests deliberately keep all inputs in temporary directories.  They
exercise the public API and frozen task contracts with deterministic runners,
so no user settings, TM database, API key, or external image service is used.
"""

from __future__ import annotations

import base64
import json
import tempfile
import threading
import time
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from api.app import create_app
from api.task_manager import TaskInputError, TaskOptions, TranslationTaskManager
from core.model_api_identity import TaskApiContext
from core.model_roles import SOURCE_INDEPENDENT
from core.pdf_image_translation import (
    PDF_MANIFEST_FILENAME,
    PDF_REPORT_FILENAME,
    SOURCE_TYPE_IMAGE,
    SOURCE_TYPE_PDF,
    PdfFileItem,
    PdfFileRecord,
    PdfImageTranslationRunner,
    PdfPageRecord,
    PdfTaskSummary,
    _write_review_json,
    write_pdf_manifest_and_report,
)
from core.pdf_review import PDF_PAGE_REVIEW_PROMPT, PdfPageReviewResult, PdfReviewIssue
from core.task_runner import DoneMsg, PdfPageRecoveryStatusMsg, PdfReviewStatusMsg, StoppedMsg
from settings import AppSettings, set_cloud_provider_config


def _write_png(path: Path, *, size: tuple[int, int] = (120, 160)) -> None:
    Image.new("RGB", size, "white").save(path, format="PNG")


def _write_single_page_pdf(path: Path) -> None:
    Image.new("RGB", (120, 160), "white").save(path, format="PDF")


def _write_three_page_pdf(path: Path) -> None:
    pages = [Image.new("RGB", (120, 160), "white") for _ in range(3)]
    try:
        pages[0].save(path, format="PDF", save_all=True, append_images=pages[1:])
    finally:
        for page in pages:
            page.close()


def _pdf_settings(root: Path, *, review_enabled: bool = False) -> AppSettings:
    settings = AppSettings()
    settings.pdf.target_lang = "en"
    settings.pdf.review_enabled = review_enabled
    settings.pdf_output.use_custom_output_dir = True
    settings.pdf_output.custom_output_dir = str(root / "pdf-output")
    settings.output.use_custom_output_dir = True
    settings.output.custom_output_dir = str(root / "legacy-shared-output")

    settings.image_model_role.source_role = SOURCE_INDEPENDENT
    settings.image_model_role.cloud_provider = "custom_openai"
    set_cloud_provider_config(
        settings.image_model_role,
        "custom_openai",
        cloud_model="mock-image-model",
        cloud_base_url="https://image.example/v1",
    )
    settings.image_model_role.cloud_model = "mock-image-model"
    settings.image_model_role.cloud_base_url = "https://image.example/v1"

    settings.pdf_review_model_role.source_role = SOURCE_INDEPENDENT
    settings.pdf_review_model_role.cloud_provider = "custom_openai"
    set_cloud_provider_config(
        settings.pdf_review_model_role,
        "custom_openai",
        cloud_model="mock-review-model",
        cloud_base_url="https://review.example/v1",
    )
    settings.pdf_review_model_role.cloud_model = "mock-review-model"
    settings.pdf_review_model_role.cloud_base_url = "https://review.example/v1"
    return settings


class _DormantPdfRunner:
    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def needs_poll(self) -> bool:
        return True

    def get_message(self, timeout: float = 0.05):  # noqa: ARG002
        return None


class _PdfLifecycleRunner:
    """Keeps one PDF task alive until the API ends its paused state."""

    def __init__(self) -> None:
        self.pause_calls = 0
        self.resume_calls = 0
        self.stop_calls = 0
        self._stopped = False
        self._messages = deque(
            [
                PdfPageRecoveryStatusMsg(
                    total_pages=4,
                    completed_pages=1,
                    submitted_page_count=2,
                    pending_submitted_page_count=1,
                    retrying_page_count=0,
                ),
                PdfReviewStatusMsg(
                    enabled=True,
                    review_round=1,
                    review_total=2,
                    review_passed_count=1,
                ),
            ]
        )

    def start(self) -> None:
        return None

    def pause(self) -> None:
        self.pause_calls += 1

    def resume(self) -> None:
        self.resume_calls += 1

    def stop(self) -> None:
        self.stop_calls += 1
        self._stopped = True
        self._messages.append(
            StoppedMsg(
                message="PDF 翻译已结束暂停任务。",
                output_dir="/isolated/pdf-output",
                report_path="/isolated/pdf-output/pdf_translation_report.md",
                manifest_path="/isolated/pdf-output/pdf_translation_manifest.json",
            )
        )

    def needs_poll(self) -> bool:
        return not self._stopped or bool(self._messages)

    def get_message(self, timeout: float = 0.05):  # noqa: ARG002
        if self._messages:
            return self._messages.popleft()
        time.sleep(min(timeout, 0.01))
        return None


class _FirstPageBlockingImageClient:
    """Lets the test pause after exactly one page has been submitted."""

    def __init__(self) -> None:
        self.calls = 0
        self.first_call_started = threading.Event()
        self.release_first_call = threading.Event()

    def generate_page(self, **_kwargs) -> bytes:
        self.calls += 1
        if self.calls == 1:
            self.first_call_started.set()
            self.release_first_call.wait(3)
        with tempfile.TemporaryDirectory() as temporary:
            image_path = Path(temporary) / "page.png"
            _write_png(image_path, size=(1200, 1600))
            return image_path.read_bytes()


class PdfScanApiContractTests(unittest.TestCase):
    def test_mixed_scan_keeps_broken_sources_visible_and_excludes_recursive_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_single_page_pdf(root / "source.pdf")
            _write_png(root / "photo.png", size=(321, 654))
            (root / "broken.pdf").write_bytes(b"not a readable pdf")
            (root / "_pdf_pages").mkdir()
            _write_png(root / "_pdf_pages" / "page_001.png")
            old_output = root / "folder_翻译输出_20260724_120000"
            old_output.mkdir()
            _write_single_page_pdf(old_output / "old.pdf")
            _write_png(old_output / "old.png")
            _write_single_page_pdf(root / "译文(英文)_source.pdf")
            (root / PDF_MANIFEST_FILENAME).write_text("{}", encoding="utf-8")
            (root / PDF_REPORT_FILENAME).write_text("# old report", encoding="utf-8")

            with TestClient(create_app()) as client:
                response = client.post(
                    "/api/sources/scan",
                    json={"surface": "pdf", "path": str(root), "include_images": True},
                )

            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()
            self.assertEqual(set(payload), {"items", "skipped", "summary", "risk", "result"})
            self.assertEqual(set(payload["result"]), {"items", "skipped", "summary", "risk"})
            selected = {item["relative_path"]: item for item in payload["items"]}
            self.assertEqual(set(selected), {"source.pdf", "photo.png"})
            self.assertEqual(selected["source.pdf"]["source_type"], SOURCE_TYPE_PDF)
            self.assertEqual(selected["source.pdf"]["page_count"], 1)
            self.assertEqual(selected["photo.png"]["source_type"], SOURCE_TYPE_IMAGE)
            self.assertEqual(selected["photo.png"]["width_px"], 321)
            self.assertEqual(selected["photo.png"]["height_px"], 654)
            self.assertEqual(payload["summary"]["pdf_count"], 1)
            self.assertEqual(payload["summary"]["image_count"], 1)
            self.assertEqual(payload["summary"]["total_page_or_image_count"], 2)
            self.assertEqual(payload["summary"]["skipped_count"], 1)
            self.assertTrue(payload["risk"]["mixed_input_supported"])
            self.assertTrue(payload["risk"]["generated_output_excluded"])
            self.assertTrue(payload["risk"]["has_skipped"])
            self.assertEqual(payload["skipped"][0]["relative_path"], "broken.pdf")
            self.assertIn("读取失败", payload["skipped"][0]["reason"])


class PdfTaskSnapshotAndPreflightContractTests(unittest.TestCase):
    def test_pdf_task_freezes_its_own_output_settings_and_never_enables_tm(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.pdf"
            _write_single_page_pdf(source)
            settings = _pdf_settings(root)
            captured: dict[str, object] = {}
            manager = TranslationTaskManager(settings_loader=lambda: settings)
            manager._scan = lambda *_args: [
                PdfFileItem(
                    path=source,
                    name="source",
                    size_kb=1.0,
                    page_count=1,
                    source_type=SOURCE_TYPE_PDF,
                )
            ]
            manager._build_runner = lambda **kwargs: (
                captured.update(kwargs) or _DormantPdfRunner()
            )

            with (
                patch("core.model_roles.get_key", return_value="pdf-route-secret"),
                patch(
                    "api.task_manager.task_api_context_for_page",
                    return_value=TaskApiContext(frozenset(), {}),
                ),
                patch("api.task_manager.threading.Thread") as thread_type,
            ):
                started = manager.start_task(
                    surface="pdf",
                    source_path=str(root),
                    options=TaskOptions(include_images=True),
                )
                thread_type.return_value.start.assert_called_once_with()

            frozen = captured["settings"]
            self.assertIsInstance(frozen, AppSettings)
            self.assertIsNot(frozen.pdf_output, frozen.output)
            self.assertTrue(frozen.pdf_output.use_custom_output_dir)
            self.assertEqual(frozen.pdf_output.custom_output_dir, str(root / "pdf-output"))
            snapshot = started["task_snapshot"]
            self.assertEqual(snapshot["surface"], "pdf")
            self.assertEqual(snapshot["target_lang"], "en")
            self.assertEqual(snapshot["pdf_output"]["custom_output_dir"], str(root / "pdf-output"))
            self.assertEqual(snapshot["tm"], {"enabled": False})
            self.assertEqual(snapshot["pdf_file_count"], 1)
            self.assertEqual(snapshot["image_file_count"], 0)
            self.assertTrue(captured["options"].include_images)

    def test_review_known_failure_requires_confirmation_then_allows_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.pdf"
            _write_single_page_pdf(source)
            settings = _pdf_settings(root, review_enabled=True)
            settings.pdf_review_model_role.availability_status = "unavailable"
            captured: dict[str, object] = {}
            manager = TranslationTaskManager(settings_loader=lambda: settings)
            manager._scan = lambda *_args: [
                PdfFileItem(source, "source", 1.0, page_count=1)
            ]
            manager._build_runner = lambda **kwargs: (
                captured.update(kwargs) or _DormantPdfRunner()
            )
            client = TestClient(create_app(task_manager=manager))

            with (
                patch("core.model_roles.get_key", return_value="pdf-route-secret"),
                patch(
                    "api.task_manager.task_api_context_for_page",
                    return_value=TaskApiContext(frozenset(), {}),
                ),
                patch("api.task_manager.threading.Thread"),
            ):
                blocked = client.post(
                    "/api/tasks",
                    json={"surface": "pdf", "source_path": str(root)},
                )
                accepted = client.post(
                    "/api/tasks",
                    json={
                        "surface": "pdf",
                        "source_path": str(root),
                        "allow_known_review_failure": True,
                    },
                )

            self.assertEqual(blocked.status_code, 422, blocked.text)
            self.assertIn("当前配置已测试失败", blocked.json()["detail"])
            self.assertEqual(accepted.status_code, 202, accepted.text)
            self.assertTrue(captured["options"].allow_known_review_failure)

    def test_pdf_preflight_rejects_a_custom_output_path_that_is_a_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = _pdf_settings(root)
            output_file = root / "not-a-directory"
            output_file.write_text("blocked", encoding="utf-8")
            settings.pdf_output.custom_output_dir = str(output_file)
            files = [PdfFileItem(root / "source.pdf", "source", 1.0, page_count=1)]

            with patch("core.model_roles.get_key", return_value="pdf-route-secret"):
                with self.assertRaisesRegex(TaskInputError, "输出路径不是目录"):
                    TranslationTaskManager._validate_pdf_preflight(
                        files=files,
                        settings=settings,
                        options=TaskOptions(),
                    )


class PdfPauseAndSseContractTests(unittest.TestCase):
    def _start_real_paused_runner(self, root: Path):
        source = root / "source.pdf"
        _write_three_page_pdf(source)
        settings = _pdf_settings(root)
        settings.pdf.page_retry_attempts = 0
        client = _FirstPageBlockingImageClient()
        runner = PdfImageTranslationRunner(
            [PdfFileItem(source, "source", 1.0, page_count=3)],
            settings,
            source_root=root,
            image_client=client,
            task_logger_enabled=False,
        )
        throughput = SimpleNamespace(concurrency=1)
        patches = (
            patch("core.model_roles.get_key", return_value="pdf-route-secret"),
            patch("core.pdf_image_translation.get_model_throughput", return_value=throughput),
            patch("core.pdf_image_translation.PDF_PAGE_RENDER_AHEAD_COUNT", 0),
        )
        return runner, client, patches

    def test_real_runner_pause_stops_new_page_submission_then_resume_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runner, client, patches = self._start_real_paused_runner(Path(temporary))
            with patches[0], patches[1], patches[2]:
                runner.start()
                self.assertTrue(client.first_call_started.wait(3))
                runner.pause()
                client.release_first_call.set()
                time.sleep(0.15)
                self.assertEqual(client.calls, 1)
                runner.resume()
                assert runner._thread is not None
                runner._thread.join(5)

            self.assertFalse(runner.is_running())
            messages = _drain_runner_messages(runner)
            self.assertEqual(client.calls, 3)
            self.assertTrue(any(isinstance(message, DoneMsg) for message in messages))
            self.assertFalse(any(isinstance(message, StoppedMsg) for message in messages))

    def test_real_runner_end_while_paused_writes_stopped_manifest_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runner, client, patches = self._start_real_paused_runner(Path(temporary))
            with patches[0], patches[1], patches[2]:
                runner.start()
                self.assertTrue(client.first_call_started.wait(3))
                runner.pause()
                client.release_first_call.set()
                time.sleep(0.15)
                self.assertEqual(client.calls, 1)
                runner.end_paused()
                assert runner._thread is not None
                runner._thread.join(5)

            self.assertFalse(runner.is_running())
            terminal = [
                message
                for message in _drain_runner_messages(runner)
                if isinstance(message, StoppedMsg)
            ]
            self.assertEqual(len(terminal), 1)
            self.assertIn("结束暂停任务", terminal[0].message)
            manifest = json.loads(Path(terminal[0].manifest_path).read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "stopped")
            self.assertEqual(manifest["terminal_reason"], "end_paused")
            self.assertTrue(Path(terminal[0].report_path).exists())

    def test_end_paused_remains_stopped_after_all_submitted_pages_finish(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.pdf"
            _write_single_page_pdf(source)
            settings = _pdf_settings(root)
            settings.pdf.page_retry_attempts = 0
            client = _FirstPageBlockingImageClient()
            runner = PdfImageTranslationRunner(
                [PdfFileItem(source, "source", 1.0, page_count=1)],
                settings,
                source_root=root,
                image_client=client,
                task_logger_enabled=False,
            )
            throughput = SimpleNamespace(concurrency=1)
            with (
                patch("core.model_roles.get_key", return_value="pdf-route-secret"),
                patch("core.pdf_image_translation.get_model_throughput", return_value=throughput),
                patch("core.pdf_image_translation.PDF_PAGE_RENDER_AHEAD_COUNT", 0),
            ):
                runner.start()
                self.assertTrue(client.first_call_started.wait(3))
                runner.pause()
                client.release_first_call.set()
                time.sleep(0.15)
                self.assertEqual(client.calls, 1)
                runner.end_paused()
                assert runner._thread is not None
                runner._thread.join(5)

            terminal = [
                message
                for message in _drain_runner_messages(runner)
                if isinstance(message, StoppedMsg)
            ]
            self.assertEqual(len(terminal), 1)
            manifest = json.loads(Path(terminal[0].manifest_path).read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "stopped")
            self.assertEqual(manifest["terminal_reason"], "end_paused")

    def test_pause_resume_end_paused_routes_preserve_sse_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.pdf"
            _write_single_page_pdf(source)
            runner = _PdfLifecycleRunner()
            manager = TranslationTaskManager(settings_loader=AppSettings)
            manager._scan = lambda *_args: [SimpleNamespace(path=source, source_type="pdf")]
            manager._build_runner = lambda **_kwargs: runner
            client = TestClient(create_app(task_manager=manager))

            with (
                patch.object(manager, "_validate_pdf_preflight"),
                patch(
                    "api.task_manager.task_api_context_for_page",
                    return_value=TaskApiContext(frozenset(), {}),
                ),
            ):
                started = client.post(
                    "/api/tasks",
                    json={"surface": "pdf", "source_path": str(root)},
                )
                self.assertEqual(started.status_code, 202, started.text)
                task_id = started.json()["task_id"]
                self.assertEqual(
                    client.post(f"/api/tasks/{task_id}/pause").json()["state"],
                    "paused",
                )
                self.assertEqual(
                    client.post(f"/api/tasks/{task_id}/resume").json()["state"],
                    "running",
                )
                self.assertEqual(
                    client.post(f"/api/tasks/{task_id}/pause").json()["state"],
                    "paused",
                )
                ending = client.post(f"/api/tasks/{task_id}/end-paused")

                self.assertEqual(ending.status_code, 200, ending.text)
                deadline = time.monotonic() + 1.0
                status = client.get(f"/api/tasks/{task_id}").json()
                while not status["terminal"] and time.monotonic() < deadline:
                    time.sleep(0.02)
                    status = client.get(f"/api/tasks/{task_id}").json()
                events = client.get(f"/api/tasks/{task_id}/events")

            self.assertEqual(runner.pause_calls, 2)
            self.assertEqual(runner.resume_calls, 1)
            self.assertEqual(runner.stop_calls, 1)
            self.assertTrue(status["terminal"])
            self.assertEqual(status["state"], "stopped")
            self.assertEqual(events.status_code, 200, events.text)
            self.assertIn("event: paused", events.text)
            self.assertIn("event: resumed", events.text)
            self.assertIn('"reason":"end_paused"', events.text)
            self.assertIn("event: pdf_page_recovery", events.text)
            self.assertIn("event: pdf_review", events.text)
            self.assertIn("event: stopped", events.text)


class PdfManifestPrivacyContractTests(unittest.TestCase):
    def test_review_evidence_omits_the_raw_model_response(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            review_path = Path(temporary) / "review.json"
            raw_response = "provider raw response must not persist"
            _write_review_json(
                review_path,
                PdfPageReviewResult(
                    passed=False,
                    blocking_issues=[
                        PdfReviewIssue(type="layout", problem="missing text")
                    ],
                    minor_suggestions=["check footer"],
                    summary="审核未通过。",
                    raw_text=raw_response,
                ),
            )

            evidence = json.loads(review_path.read_text(encoding="utf-8"))
            self.assertFalse(evidence["pass"])
            self.assertNotIn("raw_text", evidence)
            self.assertNotIn(raw_response, review_path.read_text(encoding="utf-8"))

    def test_manifest_and_report_keep_only_references_not_secrets_prompts_responses_or_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary)
            secret = "pdf-route-secret-must-not-leak"
            raw_response = "full raw review response must not leak"
            image_bytes = _write_png_bytes()
            review_path = output_dir / "_pdf_pages" / "review" / "attempt_01_review.json"
            review_path.parent.mkdir(parents=True)
            review_path.write_text(
                json.dumps({"raw_text": raw_response, "key": secret}),
                encoding="utf-8",
            )
            page = PdfPageRecord(
                page_number=1,
                source_image_path="/isolated/source.png",
                translated_image_path="/isolated/translated.png",
                status="success",
                candidate_artifacts=[
                    {
                        "attempt": 1,
                        "candidate_image_path": "/isolated/candidate.png",
                        "review_path": str(review_path),
                        "quality_status": "ok",
                        "review_status": "passed",
                        "summary": "审核通过",
                    }
                ],
            )
            summary = PdfTaskSummary(
                status="completed",
                output_dir=str(output_dir),
                target_lang="en",
                target_lang_label="英文",
                started_at="2026-07-24T12:00:00",
                completed_at="2026-07-24T12:01:00",
                elapsed_sec=60,
                file_count=1,
                total_page_count=1,
                generated_pdf_count=1,
                placeholder_page_count=0,
                emergency_ratio_normalized_count=0,
                retry_count=0,
                image_model_signature="image|mock|sha256:8f2d",
                pdf_review_model_signature="review|mock|sha256:1ab4",
                files=[
                    PdfFileRecord(
                        name="source.pdf",
                        source_path="/isolated/source.pdf",
                        relative_path="source.pdf",
                        translated_pdf_path="/isolated/translated.pdf",
                        status="completed",
                        page_count=1,
                        generated_page_count=1,
                        pages=[page],
                    )
                ],
            )

            manifest_path, report_path = write_pdf_manifest_and_report(summary)
            delivered = manifest_path.read_text(encoding="utf-8") + report_path.read_text(
                encoding="utf-8"
            )

            self.assertTrue(manifest_path.exists())
            self.assertTrue(report_path.exists())
            self.assertIn(str(review_path), manifest_path.read_text(encoding="utf-8"))
            self.assertNotIn(secret, delivered)
            self.assertNotIn(raw_response, delivered)
            self.assertNotIn(PDF_PAGE_REVIEW_PROMPT, delivered)
            self.assertNotIn(base64.b64encode(image_bytes).decode("ascii"), delivered)


def _write_png_bytes() -> bytes:
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "source.png"
        _write_png(path)
        return path.read_bytes()


def _drain_runner_messages(runner: PdfImageTranslationRunner) -> list[object]:
    messages: list[object] = []
    while True:
        message = runner.get_message(timeout=0.0)
        if message is None:
            return messages
        messages.append(message)


if __name__ == "__main__":
    unittest.main(verbosity=2)
