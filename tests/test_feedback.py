"""In-app feedback: what goes into the package, and how sending fails safely."""

from __future__ import annotations

import io
import json
import zipfile
from typing import Any
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from api.app import create_app
from api.task_contracts import TaskNotFoundError
from core import feedback
from tests.app_data_isolation import IsolatedAppDataTestCase


def _task_record(task_id: str = "task-1", *, state: str = "error") -> dict[str, Any]:
    return {
        "task_id": task_id,
        "surface": "excel",
        "state": state,
        "terminal": True,
        "source_label": "盛泰项目报价单.xlsx 等 2 个文件",
        "created_at": 1_760_000_000,
        "updated_at": 1_760_000_100,
        "model_snapshot": {"model": "secret-model", "base_url": "https://api.example.com"},
        "file_progress": {
            "revision": 3,
            "files": [
                {"file_id": "a", "name": "盛泰项目报价单.xlsx", "state": "failed"},
                {"file_id": "b", "name": "客户清单.docx", "state": "done"},
            ],
        },
        "result": {
            "output_dir": "/Users/someone/项目/输出",
            "message": "盛泰项目报价单.xlsx 翻译失败",
            "local_operations": [{"action": "open_output", "path": "/Users/someone/项目/输出"}],
        },
        "logs": [
            {"level": "INFO", "message": "开始读取 盛泰项目报价单.xlsx"},
            {"level": "INFO", "message": "已写出 盛泰项目报价单_FR.xlsx"},
            {"level": "ERROR", "message": "无法打开 /Users/someone/项目/旧合同.pdf"},
            {"level": "WARNING", "message": "其他文件 未知附件.dwg 跳过"},
        ],
    }


class _FakeTaskManager:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = {record["task_id"]: record for record in records}

    def list_tasks(self) -> dict[str, Any]:
        recent = [{k: v for k, v in record.items() if k != "logs"} for record in self._records.values()]
        return {"active": [], "recent": recent, "active_work_count": 0}

    def task_status(self, task_id: str) -> dict[str, Any]:
        if task_id not in self._records:
            raise TaskNotFoundError(task_id)
        return self._records[task_id]

    def shutdown(self) -> None:
        return None


class FeedbackPackageTests(IsolatedAppDataTestCase):
    def setUp(self) -> None:
        super().setUp()
        app = create_app()
        app.state.task_manager = _FakeTaskManager([_task_record()])
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def _body(self, **overrides: Any) -> dict[str, Any]:
        body = {"category": "problem", "description": "导出时报错", "task_ids": ["task-1"]}
        body.update(overrides)
        return body

    def _package(self, **overrides: Any) -> zipfile.ZipFile:
        response = self.client.post("/api/feedback/package", json=self._body(**overrides))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("translator-feedback-", response.headers["content-disposition"])
        return zipfile.ZipFile(io.BytesIO(response.content))

    def test_task_log_hides_file_names_paths_and_model(self) -> None:
        archive = self._package()
        names = archive.namelist()
        self.assertIn("feedback.json", names)
        self.assertIn("system.json", names)
        self.assertIn("tasks/1-excel/log.txt", names)
        self.assertFalse([name for name in names if name.startswith("files/")])

        everything = "\n".join(archive.read(name).decode("utf-8") for name in names)
        for leaked in ("盛泰", "客户清单", "旧合同", "未知附件", "/Users/someone", "secret-model", "api.example.com"):
            self.assertNotIn(leaked, everything)

        log = archive.read("tasks/1-excel/log.txt").decode("utf-8")
        self.assertIn("开始读取 文件1.xlsx", log)
        self.assertIn("已写出 文件1_FR.xlsx", log)
        self.assertIn("[ERROR]", log)
        task = json.loads(archive.read("tasks/1-excel/task.json"))
        self.assertEqual(task["file_progress"]["files"][1]["name"], "文件2.docx")
        self.assertNotIn("output_dir", task["result"])

    def test_nothing_optional_is_included_unless_ticked(self) -> None:
        archive = self._package(include_system=False, task_ids=[])
        self.assertEqual(archive.namelist(), ["feedback.json"])
        payload = json.loads(archive.read("feedback.json"))
        self.assertEqual(payload["description"], "导出时报错")
        self.assertEqual(payload["includes"], {"system": False, "task_logs": 0, "files": []})

    def test_picked_files_are_attached_under_their_names(self) -> None:
        attached = self.app_data_dir.parent / "样例.xlsx"
        attached.write_bytes(b"PK-sample")
        archive = self._package(file_paths=[str(attached)])
        self.assertEqual(archive.read("files/样例.xlsx"), b"PK-sample")

    def test_attachments_over_the_limit_are_refused_with_a_reason(self) -> None:
        big = self.app_data_dir.parent / "big.pdf"
        with big.open("wb") as handle:
            handle.truncate(feedback.MAX_ATTACHMENT_BYTES + 1)
        response = self.client.post("/api/feedback/preview", json=self._body(file_paths=[str(big)]))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["reason"], "too_large")

    def test_empty_description_and_missing_task_are_refused(self) -> None:
        empty = self.client.post("/api/feedback/preview", json=self._body(description="  "))
        self.assertEqual(empty.json()["reason"], "empty_description")
        missing = self.client.post("/api/feedback/preview", json=self._body(task_ids=["gone"]))
        self.assertEqual(missing.json()["reason"], "task_missing")

    def test_preview_shows_the_masked_log_and_context_lists_tasks(self) -> None:
        preview = self.client.post("/api/feedback/preview", json=self._body()).json()
        self.assertIn("文件1.xlsx", preview["tasks"][0]["log"])
        self.assertGreater(preview["package_bytes"], 0)
        context = self.client.get("/api/feedback/context").json()
        self.assertEqual([task["task_id"] for task in context["tasks"]], ["task-1"])

    def test_send_without_a_relay_says_so(self) -> None:
        with patch.object(feedback, "FEEDBACK_ENDPOINT", ""), patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("TRANSLATOR_FEEDBACK_ENDPOINT", None)
            response = self.client.post("/api/feedback/send", json=self._body())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["reason"], "not_configured")

    def _send_with(self, handler: Any) -> httpx.Response:
        transport = httpx.MockTransport(handler)
        with patch.object(feedback, "FEEDBACK_ENDPOINT", "https://relay.example/"), patch.object(
            feedback, "_open_client", lambda: httpx.Client(transport=transport)
        ):
            return self.client.post("/api/feedback/send", json=self._body())

    def test_send_posts_meta_and_package_and_returns_the_reference(self) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["body"] = request.read()
            return httpx.Response(200, json={"ok": True, "id": "FB-20261012-ABCD"})

        response = self._send_with(handler)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["id"], "FB-20261012-ABCD")
        self.assertEqual(seen["url"], "https://relay.example/v1/feedback")
        self.assertIn(b'name="meta"', seen["body"])
        self.assertIn(b'name="package"', seen["body"])
        self.assertNotIn("盛泰".encode("utf-8"), seen["body"])

    def test_send_failures_map_to_reasons(self) -> None:
        def offline(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("offline", request=request)

        self.assertEqual(self._send_with(offline).json()["reason"], "network")
        self.assertEqual(self._send_with(lambda r: httpx.Response(429)).json()["reason"], "rate_limited")
        self.assertEqual(self._send_with(lambda r: httpx.Response(500)).json()["reason"], "server")


class NameMaskerTests(IsolatedAppDataTestCase):
    def test_longer_names_win_and_unknown_document_names_are_hidden(self) -> None:
        masker = feedback._NameMasker(["报价单.xlsx", "报价单_2026.xlsx"])
        self.assertEqual(masker("报价单_2026.xlsx 和 报价单.xlsx"), "文件2.xlsx 和 文件1.xlsx")
        self.assertEqual(masker("见 合同.docx"), "见 [文件名]")
        self.assertEqual(masker("进度 3/9 批"), "进度 3/9 批")


def test_task_files_lists_existing_source_and_outputs(tmp_path):
    source = tmp_path / "报价单.xlsx"
    source.write_bytes(b"src")
    output = tmp_path / "报价单_FR.xlsx"
    output.write_bytes(b"out!")
    record = {
        "file_progress": {"files": [{"file_id": "f1", "name": "报价单.xlsx", "result": {}}]},
        "result": {
            "file_results": [
                {"file_id": "f1", "output_path": str(output), "compressed_output": str(tmp_path / "gone.pdf")},
                {"output": "[path]"},
            ]
        },
    }

    files = feedback.task_files(record, {"f1": str(source)})

    assert [(item["kind"], item["name"], item["bytes"]) for item in files] == [
        ("source", "报价单.xlsx", 3),
        ("output", "报价单_FR.xlsx", 4),
    ]
    # 历史记录里没有原文件路径时，只列出译文。
    assert [item["kind"] for item in feedback.task_files(record)] == ["output"]
