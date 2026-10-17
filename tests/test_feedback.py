"""In-app feedback: what goes into the package, and how sending fails safely."""

from __future__ import annotations

import base64
import io
import json
import re
import time
import zipfile
from typing import Any
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from api.app import create_app
from api.task_contracts import TaskNotFoundError
from core import feedback, task_logger
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

    def task_source(self, task_id: str) -> dict[str, Any]:
        # 历史记录里的任务不再保留原文件路径。
        raise TaskNotFoundError(task_id)

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
        self.assertEqual(payload["includes"], {"system": False, "task_logs": 0, "files": [], "images": 0})

    def test_kind_checkboxes_choose_what_comes_from_the_chosen_tasks(self) -> None:
        output = self.app_data_dir.parent / "报价单_FR.xlsx"
        output.write_bytes(b"PK-out")
        record = _task_record()
        record["result"]["file_results"] = [{"file_id": "a", "output_path": str(output)}]
        self.client.app.state.task_manager = _FakeTaskManager([record])

        archive = self._package(include_logs=False)
        self.assertFalse([name for name in archive.namelist() if name.startswith(("tasks/", "files/"))])

        archive = self._package(include_logs=False, include_output_files=True)
        self.assertEqual(archive.read("files/报价单_FR.xlsx"), b"PK-out")
        self.assertFalse([name for name in archive.namelist() if name.startswith("tasks/")])

    def test_pasted_screenshots_are_packed_and_checked(self) -> None:
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
        archive = self._package(images=[{"data": base64.b64encode(png).decode()}])
        self.assertEqual(archive.read("images/截图1.png"), png)
        self.assertEqual(json.loads(archive.read("feedback.json"))["includes"]["images"], 1)
        # 截图不限张数，只受附件总大小限制。
        many = self._package(images=[{"data": base64.b64encode(png).decode()}] * 12)
        self.assertIn("images/截图12.png", many.namelist())
        # 截图的额度（合计 10 MB）和文件分开算。
        with patch.object(feedback, "MAX_IMAGES_TOTAL_BYTES", len(png) * 3):
            over = self.client.post(
                "/api/feedback/preview", json=self._body(images=[{"data": base64.b64encode(png).decode()}] * 4)
            )
        self.assertEqual(over.json()["reason"], "images_too_large")

        response = self.client.post(
            "/api/feedback/package", json=self._body(images=[{"data": base64.b64encode(b"not an image").decode()}])
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["reason"], "invalid_image")

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
        empty = self.client.post("/api/feedback/preview", json=self._body(description="  ", task_ids=[]))
        self.assertEqual(empty.json()["reason"], "empty_description")
        # 附上了任务（「反馈此任务」）时描述可以不写，issue 里写明用户没填。
        archive = self._package(description="")
        self.assertEqual(json.loads(archive.read("feedback.json"))["description"], feedback.NO_DESCRIPTION)
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

    def _send_with(self, handler: Any, **overrides: Any) -> dict[str, Any]:
        transport = httpx.MockTransport(handler)
        with patch.object(feedback, "FEEDBACK_ENDPOINT", "https://relay.example/"), patch.object(
            feedback, "_open_client", lambda: httpx.Client(transport=transport)
        ):
            started = self.client.post("/api/feedback/send", json=self._body(**overrides))
            self.assertEqual(started.status_code, 200, started.text)
            job_id = started.json()["job_id"]
            self.assertEqual(started.json()["sent"], 0)
            deadline = time.monotonic() + 10
            while True:
                status = self.client.get(f"/api/feedback/send/{job_id}").json()
                if status["state"] != "uploading" or time.monotonic() > deadline:
                    return status
                time.sleep(0.02)

    def test_send_posts_meta_and_package_and_returns_the_reference(self) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["body"] = request.read()
            return httpx.Response(200, json={"ok": True, "id": "FB-20261012-ABCD"})

        status = self._send_with(handler)
        self.assertEqual(status["state"], "done", status)
        self.assertEqual(status["id"], "FB-20261012-ABCD")
        self.assertEqual(status["sent"], status["total"])
        self.assertEqual(seen["url"], "https://relay.example/v1/feedback")
        self.assertIn(b'name="meta"', seen["body"])
        self.assertIn(b'name="package"', seen["body"])
        self.assertNotIn("盛泰".encode("utf-8"), seen["body"])

    def test_send_failures_map_to_reasons(self) -> None:
        def offline(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("offline", request=request)

        self.assertEqual(self._send_with(offline)["reason"], "network")
        self.assertEqual(self._send_with(lambda r: httpx.Response(429))["reason"], "rate_limited")
        failed = self._send_with(lambda r: httpx.Response(500))
        self.assertEqual((failed["state"], failed["reason"]), ("error", "server"))

    def test_email_of_a_sent_feedback_is_remembered_for_the_next_one(self) -> None:
        def ok(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"id": "FB-1"})

        def context_email() -> str:
            return self.client.get("/api/feedback/context").json()["saved_email"]

        def asks_for_email() -> bool:
            return self.client.get("/api/feedback/context").json()["ask_email"]

        self.assertEqual(context_email(), "")
        self.assertTrue(asks_for_email())
        # 只是保存 ZIP 或发送失败，邮箱并没有交出去，不记。
        self.client.post("/api/feedback/package", json=self._body(contact="a@example.com"))
        self._send_with(lambda r: httpx.Response(500), contact="a@example.com")
        self.assertEqual(context_email(), "")

        self.assertEqual(self._send_with(ok, contact=" a@example.com ")["state"], "done")
        self.assertEqual(context_email(), "a@example.com")
        self.assertTrue(feedback.saved_contact_path().is_relative_to(self.app_data_dir))
        # 改成别的邮箱就记新的；清空后发送就忘掉。
        self._send_with(ok, contact="b@example.com")
        self.assertEqual(context_email(), "b@example.com")
        self.assertFalse(asks_for_email())
        # 不留邮箱发出一次，就是用户做了选择：忘掉旧邮箱，以后也不再问。
        self._send_with(ok, contact="")
        self.assertEqual(context_email(), "")
        self.assertFalse(asks_for_email())

    def _app_log(self) -> str:
        path = self.app_data_dir / "app.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def _assert_nothing_private(self, log: str) -> None:
        for private in ("导出时报错", "a@example.com", "盛泰", "客户清单", "/Users/someone"):
            self.assertNotIn(private, log)

    def test_every_send_attempt_leaves_a_private_free_trace_in_app_log(self) -> None:
        self.addCleanup(task_logger._remove_file_handlers)

        def ok(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": True, "id": "FB-20261017-ABCDEF"})

        def tls_failure(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed", request=request)

        def slow(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        def relay_error(request: httpx.Request) -> httpx.Response:
            return httpx.Response(502, json={"error": "upstream"})

        def portal(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": "http://portal.example/login?mac=1"}, text="<html>")

        self.assertEqual(self._send_with(ok, contact="a@example.com")["state"], "done")
        for handler in (tls_failure, slow, relay_error, portal):
            self.assertEqual(self._send_with(handler, contact="a@example.com")["state"], "error")
        with patch.object(feedback, "_upload", side_effect=RuntimeError("boom")):
            self.assertEqual(self._send_with(ok)["reason"], "server")
        self.client.post("/api/feedback/send", json=self._body(task_ids=[], description=" "))

        log = self._app_log()
        lines = [line for line in log.splitlines() if "[task:feedback]" in line]
        self.assertEqual(sum("反馈发送开始" in line for line in lines), 7, log)
        success = next(line for line in lines if "反馈发送成功" in line)
        self.assertIn("编号=FB-20261017-ABCDEF", success)
        self.assertIn("耗时=", success)
        failures = [line for line in lines if "反馈发送失败" in line]
        self.assertEqual(len(failures), 5, log)
        tls, timeout, relay, redirect, internal = failures
        self.assertIn("阶段=连接", tls)
        self.assertIn("ConnectError: [SSL: CERTIFICATE_VERIFY_FAILED]", tls)
        self.assertIn("阶段=等待服务器确认", timeout)
        self.assertIn("ReadTimeout", timeout)
        self.assertIn("HTTP=502 | 服务器错误=upstream", relay)
        self.assertIn("HTTP=302", redirect)
        self.assertIn("跳转到=portal.example", redirect)
        self.assertIn("非中转站应答", redirect)
        self.assertIn("阶段=程序内部 | 异常=RuntimeError: boom", internal)
        for line in failures:
            self.assertIn("已上传=", line)
            self.assertIn("耗时=", line)
            # 开始、失败两行用同一个发送编号串起来。
            tag = re.search(r"发送=(\w+)", line).group(1)
            self.assertTrue(any("反馈发送开始" in other and f"发送={tag}" in other for other in lines))
        self.assertTrue(any("反馈发送未开始" in line and "原因=empty_description" in line for line in lines), log)
        self.assertIn("邮箱=已填", log)
        self._assert_nothing_private(log)

    def test_error_text_drops_paths_file_names_and_credentials(self) -> None:
        try:
            raise PermissionError(13, "Permission denied", "/Users/someone/项目/盛泰报价单.xlsx")
        except PermissionError as exc:
            text = feedback._error_text(exc)
        self.assertTrue(text.startswith("PermissionError: [Errno 13] Permission denied"), text)
        self.assertNotIn("盛泰", text)
        self.assertNotIn("/Users", text)
        self.assertIn("@ test_feedback.py:", text)
        try:
            raise httpx.ProxyError("http://bob:hunter2@proxy.local:8080 refused; token=abc123")
        except httpx.ProxyError as exc:
            text = feedback._error_text(exc)
        self.assertNotIn("hunter2", text)
        self.assertNotIn("abc123", text)

    def test_proxy_label_names_only_the_kind_of_proxy(self) -> None:
        relay = "https://relay.example"
        with patch("urllib.request.getproxies", return_value={"https": "http://u:p@127.0.0.1:7890"}):
            self.assertEqual(feedback._proxy_label(relay), "有（http，本机）")
        with patch("urllib.request.getproxies", return_value={"https": "http://10.0.0.2:3128", "no": "localhost,.example"}):
            self.assertEqual(feedback._proxy_label(relay), "无（不走代理）")
            self.assertEqual(feedback._proxy_label("https://other.org"), "有（http，其他机器）")
        with patch("urllib.request.getproxies", return_value={}):
            self.assertEqual(feedback._proxy_label(relay), "无")

    def test_unknown_send_job_is_reported(self) -> None:
        response = self.client.get("/api/feedback/send/nope")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["reason"], "job_missing")


def test_upload_progress_counts_only_bytes_already_handed_over():
    data = bytes(range(256)) * 1024  # 256 KiB，httpx 每次取 64 KiB
    reported: list[int] = []
    received: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        for chunk in request.stream:
            # 服务端每收到一块时，进度不会超过已经收到的字节数。
            received.append(len(chunk))
        return httpx.Response(200, json={"id": "FB-1"})

    with patch.object(feedback, "_open_client", lambda: httpx.Client(transport=httpx.MockTransport(handler))):
        reference = feedback._upload("https://relay.example", data, {"category": "problem"}, on_progress=reported.append)

    assert reference == "FB-1"
    assert reported == sorted(reported)
    assert reported[0] == 0
    assert reported[-1] == len(data)
    assert len(reported) > 3


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
