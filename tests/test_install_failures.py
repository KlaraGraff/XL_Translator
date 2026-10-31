"""组件一键安装失败时，原因存进诊断目录，并随反馈包和「导出诊断」带上。"""

from __future__ import annotations

import io
import json
import threading
import zipfile
from pathlib import Path

import httpx
from loguru import logger

from api.component_install_job import InstallJob
from core import diagnostics, feedback, install_failures
from tests.app_data_isolation import IsolatedAppDataTestCase


class _InstallError(RuntimeError):
    pass


def _run(job: InstallJob) -> dict:
    job.start(1)
    for _ in range(200):
        state = job.progress()
        if not state["running"]:
            return state
        threading.Event().wait(0.02)
    raise AssertionError("install job did not finish")


def _failing_install(*, progress, cancel):
    progress("download", 512, 2048)
    logger.warning("[组件安装] 下载失败，换下一个下载源：https://files.pythonhosted.org/packages/x.whl: "
                   "ConnectError('[Errno 61] Connection refused')")
    logger.warning("无法写入 /Users/someone/Library/Application Support/Translator/plugins/a.part "
                   "api_key=sk-abcdefghijklmnop")
    try:
        raise httpx.ConnectError("[Errno 61] Connection refused")
    except httpx.ConnectError as exc:
        raise _InstallError("组件下载失败，请检查网络连接。") from exc


class InstallFailureRecordTest(IsolatedAppDataTestCase):
    def test_failure_reason_is_recorded_without_paths_or_keys(self) -> None:
        job = InstallJob("PDF 处理组件", lambda: _failing_install, lambda: _InstallError)
        state = _run(job)
        self.assertEqual(state["error"], "组件下载失败，请检查网络连接。")

        records = install_failures.recent_failures()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["component"], "PDF 处理组件")
        self.assertEqual((record["phase"], record["done"], record["total"]), ("download", 512, 2048))
        self.assertEqual(record["message"], "组件下载失败，请检查网络连接。")
        self.assertEqual(record["errors"][0], "_InstallError: 组件下载失败，请检查网络连接。")
        self.assertIn("ConnectError: [Errno 61] Connection refused", record["errors"][1])
        self.assertTrue(record["where"] and "_failing_install" in record["where"][-1])
        log = "\n".join(record["log"])
        self.assertIn("https://files.pythonhosted.org/packages/x.whl", log)
        text = json.dumps(record, ensure_ascii=False)
        self.assertNotIn("someone", text)
        self.assertNotIn("sk-abcdefghijklmnop", text)
        self.assertNotIn("/Users/", text)

    def test_unexpected_error_and_cancel(self) -> None:
        def broken(*, progress, cancel):
            raise ValueError("unexpected layout")

        _run(InstallJob("文字识别组件", lambda: broken, lambda: _InstallError))
        record = install_failures.recent_failures()[0]
        self.assertEqual(record["message"], "组件安装失败。")
        self.assertEqual(record["errors"], ["ValueError: unexpected layout"])

        def cancelled(*, progress, cancel):
            cancel.set()
            raise _InstallError("安装已取消。")

        _run(InstallJob("文字识别组件", lambda: cancelled, lambda: _InstallError))
        self.assertEqual(len(install_failures.recent_failures()), 1, "a cancel is not a failure")

    def test_only_recent_failures_are_kept(self) -> None:
        for index in range(install_failures.MAX_RECORDS + 3):
            install_failures.record_failure(f"组件{index}", message="失败", exc=None, progress={})
        records = install_failures.recent_failures()
        self.assertEqual(len(records), install_failures.MAX_RECORDS)
        self.assertEqual(records[0]["component"], f"组件{install_failures.MAX_RECORDS + 2}")

    def test_feedback_package_and_diagnostics_export_include_failures(self) -> None:
        _run(InstallJob("图纸转换工具", lambda: _failing_install, lambda: _InstallError))
        draft = feedback.FeedbackDraft(category="problem", description="装不上", include_system=True)
        data, _payload = feedback.build_package(draft, settings=None)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            system = json.loads(archive.read("system.json"))
        failures = system["component_install_failures"]
        self.assertEqual(failures[0]["component"], "图纸转换工具")

        payload, _name, _count = diagnostics.build_diagnostics_history_zip_bytes()
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            exported = json.loads(archive.read(install_failures.FILENAME))
        self.assertEqual(exported[0]["component"], "图纸转换工具")
        self.assertTrue(Path(install_failures.failures_path()).is_relative_to(self.app_data_dir))
