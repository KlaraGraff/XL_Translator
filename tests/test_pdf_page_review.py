"""Route contracts for the paused per-page PDF review panel.

The panel is pull-only: the SSE stream keeps its aggregate messages and the
frontend refreshes these routes when one arrives.  Everything here runs on a
deterministic fake runner, so no model credentials or real PDF work is needed.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from api.app import create_app
from api.task_manager import TranslationTaskManager
from core.model_api_identity import TaskApiContext
from core.pdf_image_translation import PdfPageActionError
from core.task_runner import StoppedMsg
from settings import AppSettings
from tests.app_data_isolation import IsolatedAppDataTestCase


def _write_png(path: Path) -> None:
    Image.new("RGB", (60, 80), "white").save(path, format="PNG")


class _ReviewPanelRunner:
    """Exposes exactly the per-page review surface the API depends on."""

    def __init__(self, *, source_image: Path, translated_image: Path) -> None:
        self.pause_calls = 0
        self.resume_calls = 0
        self.stop_calls = 0
        self.requests: list[tuple[str, str, int]] = []
        self._stopped = False
        self._messages: deque = deque()
        self._images = {
            ("source.pdf", 1, "source"): source_image,
            ("source.pdf", 1, "translated"): translated_image,
        }
        self._statuses = {1: "success", 2: "placeholder_pending"}

    # -- lifecycle ---------------------------------------------------------
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

    def get_message(self, timeout: float = 0.05):
        if self._messages:
            return self._messages.popleft()
        time.sleep(min(timeout, 0.01))
        return None

    # -- review panel ------------------------------------------------------
    def pdf_page_snapshot(self) -> dict:
        return {
            "files": [
                {
                    "name": "source.pdf",
                    "relative_path": "source.pdf",
                    "source_type": "pdf",
                    "status": "pending",
                    "error": "",
                    "page_count": 2,
                    "pages": [
                        {
                            "page_number": number,
                            "status": status,
                            "review_status": "passed" if status == "success" else "failed",
                            "attempts": 1,
                            "placeholder": status != "success",
                            "error": "" if status == "success" else "图像生成失败",
                            "review_summary": "",
                            "pending_action": "",
                            "user_skipped": False,
                            "has_source_image": number == 1,
                            "has_translated_image": status == "success",
                        }
                        for number, status in sorted(self._statuses.items())
                    ],
                }
            ]
        }

    def resolve_page_image_path(self, *, relative_path: str, page_number: int, kind: str):
        if kind not in {"source", "translated"}:
            raise PdfPageActionError("页图类型只能是 source 或 translated。")
        return self._images.get((relative_path, int(page_number), kind))

    def request_page_regenerate(self, *, relative_path: str, page_number: int) -> dict:
        return self._accept("regenerate", relative_path, page_number)

    def request_page_skip(self, *, relative_path: str, page_number: int) -> dict:
        if self._statuses.get(int(page_number)) == "success":
            raise PdfPageActionError("source.pdf 第 1 页没有失败，不能跳过；如需改动请选择重新生成。")
        return self._accept("skip", relative_path, page_number)

    def _accept(self, kind: str, relative_path: str, page_number: int) -> dict:
        if relative_path != "source.pdf":
            raise PdfPageActionError("该任务没有这个文件的逐页记录。")
        self.requests.append((kind, relative_path, int(page_number)))
        return {
            "action": kind,
            "relative_path": relative_path,
            "name": "source.pdf",
            "page_number": int(page_number),
            "applies_on": "resume",
        }


class _PlainPdfRunner:
    """A PDF runner from before the review panel existed."""

    def start(self) -> None:
        return None

    def pause(self) -> None:
        return None

    def needs_poll(self) -> bool:
        return True

    def get_message(self, timeout: float = 0.05):
        time.sleep(min(timeout, 0.01))
        return None


class PdfPageReviewRouteTests(IsolatedAppDataTestCase):
    def _client(self, root: Path, runner):
        source = root / "source.pdf"
        source.write_bytes(b"%PDF-1.4\n")
        manager = TranslationTaskManager(settings_loader=AppSettings)
        manager._scan = lambda *_args: [SimpleNamespace(path=source, source_type="pdf")]
        manager._build_runner = lambda **_kwargs: runner
        return manager, TestClient(create_app(task_manager=manager)), root

    def _start(self, client: TestClient, root: Path) -> str:
        started = client.post(
            "/api/tasks",
            json={"surface": "pdf", "source_path": str(root)},
        )
        self.assertEqual(started.status_code, 202, started.text)
        return started.json()["task_id"]

    def _review_runner(self, root: Path) -> _ReviewPanelRunner:
        source_image = root / "page_1_source.png"
        translated_image = root / "page_1_translated.png"
        _write_png(source_image)
        _write_png(translated_image)
        return _ReviewPanelRunner(
            source_image=source_image,
            translated_image=translated_image,
        )

    def _patches(self, manager: TranslationTaskManager):
        return (
            patch.object(manager, "_validate_pdf_preflight"),
            patch(
                "api.task_manager.task_api_context_for_page",
                return_value=TaskApiContext(frozenset(), {}),
            ),
        )

    def test_snapshot_route_reports_pages_and_flips_actionable_when_paused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = self._review_runner(root)
            manager, client, root = self._client(root, runner)
            preflight, context = self._patches(manager)
            with preflight, context:
                task_id = self._start(client, root)

                running = client.get(f"/api/tasks/{task_id}/pdf-pages")
                self.assertEqual(running.status_code, 200, running.text)
                running_body = running.json()
                self.assertEqual(running_body["state"], "running")
                self.assertFalse(running_body["actionable"])
                self.assertEqual(len(running_body["files"]), 1)
                pages = running_body["files"][0]["pages"]
                self.assertEqual([page["page_number"] for page in pages], [1, 2])
                self.assertEqual(pages[0]["status"], "success")
                self.assertTrue(pages[0]["has_translated_image"])

                client.post(f"/api/tasks/{task_id}/pause")
                paused = client.get(f"/api/tasks/{task_id}/pdf-pages").json()
                self.assertEqual(paused["state"], "paused")
                self.assertTrue(paused["actionable"])

    def test_snapshot_route_rejects_unknown_tasks_and_runners_without_the_panel(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager, client, root = self._client(root, _PlainPdfRunner())
            preflight, context = self._patches(manager)
            with preflight, context:
                missing = client.get("/api/tasks/does-not-exist/pdf-pages")
                self.assertEqual(missing.status_code, 404, missing.text)

                task_id = self._start(client, root)
                unsupported = client.get(f"/api/tasks/{task_id}/pdf-pages")
                self.assertEqual(unsupported.status_code, 422, unsupported.text)

    def test_single_page_actions_are_accepted_only_while_paused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = self._review_runner(root)
            manager, client, root = self._client(root, runner)
            preflight, context = self._patches(manager)
            with preflight, context:
                task_id = self._start(client, root)

                while_running = client.post(
                    f"/api/tasks/{task_id}/pdf-pages/regenerate",
                    json={"file": "source.pdf", "page": 1},
                )
                self.assertEqual(while_running.status_code, 409, while_running.text)
                self.assertEqual(while_running.json()["reason"], "task_not_paused")
                self.assertEqual(runner.requests, [])

                client.post(f"/api/tasks/{task_id}/pause")
                accepted = client.post(
                    f"/api/tasks/{task_id}/pdf-pages/regenerate",
                    json={"file": "source.pdf", "page": 1},
                )
                self.assertEqual(accepted.status_code, 200, accepted.text)
                body = accepted.json()
                self.assertEqual(body["state"], "paused")
                self.assertEqual(body["accepted"]["action"], "regenerate")
                self.assertEqual(body["accepted"]["applies_on"], "resume")
                self.assertEqual(runner.requests, [("regenerate", "source.pdf", 1)])

                skipped = client.post(
                    f"/api/tasks/{task_id}/pdf-pages/skip",
                    json={"file": "source.pdf", "page": 2},
                )
                self.assertEqual(skipped.status_code, 200, skipped.text)
                self.assertEqual(runner.requests[-1], ("skip", "source.pdf", 2))

                client.post(f"/api/tasks/{task_id}/end-paused")
                deadline = time.monotonic() + 1.0
                status = client.get(f"/api/tasks/{task_id}").json()
                while not status["terminal"] and time.monotonic() < deadline:
                    time.sleep(0.02)
                    status = client.get(f"/api/tasks/{task_id}").json()
                self.assertTrue(status["terminal"])

                after_end = client.post(
                    f"/api/tasks/{task_id}/pdf-pages/regenerate",
                    json={"file": "source.pdf", "page": 1},
                )
                self.assertEqual(after_end.status_code, 409, after_end.text)
                self.assertEqual(after_end.json()["reason"], "task_terminal")

                events = client.get(f"/api/tasks/{task_id}/events")
                self.assertEqual(events.status_code, 200, events.text)
                self.assertIn("event: pdf_page_action", events.text)

    def test_page_action_rejections_from_the_runner_become_input_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = self._review_runner(root)
            manager, client, root = self._client(root, runner)
            preflight, context = self._patches(manager)
            with preflight, context:
                task_id = self._start(client, root)
                client.post(f"/api/tasks/{task_id}/pause")

                on_success = client.post(
                    f"/api/tasks/{task_id}/pdf-pages/skip",
                    json={"file": "source.pdf", "page": 1},
                )
                self.assertEqual(on_success.status_code, 422, on_success.text)
                self.assertIn("不能跳过", on_success.json()["detail"])

                traversal = client.post(
                    f"/api/tasks/{task_id}/pdf-pages/regenerate",
                    json={"file": "../../etc/passwd", "page": 1},
                )
                self.assertEqual(traversal.status_code, 422, traversal.text)

                bad_page = client.post(
                    f"/api/tasks/{task_id}/pdf-pages/regenerate",
                    json={"file": "source.pdf", "page": 0},
                )
                self.assertEqual(bad_page.status_code, 422, bad_page.text)
                self.assertEqual(runner.requests, [])

    def test_page_image_route_serves_only_paths_the_runner_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = self._review_runner(root)
            manager, client, root = self._client(root, runner)
            preflight, context = self._patches(manager)
            with preflight, context:
                task_id = self._start(client, root)
                base = f"/api/tasks/{task_id}/pdf-pages/image"

                for kind in ("source", "translated"):
                    served = client.get(base, params={"file": "source.pdf", "page": 1, "kind": kind})
                    self.assertEqual(served.status_code, 200, served.text)
                    self.assertEqual(served.headers["content-type"], "image/png")
                    self.assertEqual(served.headers["cache-control"], "no-store")
                    self.assertTrue(served.content.startswith(b"\x89PNG"))

                traversal = client.get(
                    base,
                    params={"file": "../../etc/passwd", "page": 1, "kind": "source"},
                )
                self.assertEqual(traversal.status_code, 404, traversal.text)

                missing = client.get(base, params={"file": "source.pdf", "page": 2, "kind": "translated"})
                self.assertEqual(missing.status_code, 404, missing.text)

                bad_kind = client.get(
                    base,
                    params={"file": "source.pdf", "page": 1, "kind": "../secrets"},
                )
                self.assertEqual(bad_kind.status_code, 422, bad_kind.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
