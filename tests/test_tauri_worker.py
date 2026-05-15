from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.connectivity_check import ConnectivityResult
from settings import AppSettings
from scripts.tauri_worker import connectivity_check_payload


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class TauriWorkerProtocolTests(unittest.TestCase):
    def run_worker(self, requests: list[dict]) -> list[dict]:
        with tempfile.TemporaryDirectory() as temp_dir:
            env = dict(os.environ)
            env["HOME"] = temp_dir
            env["USERPROFILE"] = temp_dir
            env["PYTHONUTF8"] = "1"
            payload = "".join(
                json.dumps(request, ensure_ascii=False) + "\n"
                for request in requests
            )
            completed = subprocess.run(
                [sys.executable, str(PROJECT_ROOT / "scripts" / "tauri_worker.py")],
                cwd=PROJECT_ROOT,
                input=payload,
                text=True,
                encoding="utf-8",
                capture_output=True,
                env=env,
                check=True,
                timeout=20,
            )

        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        return [json.loads(line) for line in lines]

    def test_ping_returns_versioned_json_response(self) -> None:
        messages = self.run_worker(
            [{"id": "ping-1", "command": "ping", "payload": {}}]
        )

        self.assertEqual(messages[0]["event"], "worker.ready")
        self.assertEqual(messages[1]["id"], "ping-1")
        self.assertTrue(messages[1]["ok"])
        self.assertEqual(messages[1]["result"]["version"], "5.0")

    def test_bootstrap_exposes_word_paragraph_range_for_v5_ui(self) -> None:
        messages = self.run_worker(
            [{"id": "boot-1", "command": "app.bootstrap", "payload": {}}]
        )

        response = messages[1]

        self.assertTrue(response["ok"])
        self.assertEqual(response["result"]["wordBatch"]["paragraphs"], [1, 16, 8])

    def test_connectivity_check_uses_draft_settings_payload(self) -> None:
        settings = AppSettings()
        settings.engine.cloud_model = "draft-model"

        def fake_check(received: AppSettings) -> ConnectivityResult:
            self.assertEqual(received.engine.cloud_model, "draft-model")
            return ConnectivityResult(
                ok=True,
                status="ok",
                message="draft settings used",
                provider=received.engine.cloud_provider,
                model=received.engine.cloud_model,
            )

        with patch("scripts.tauri_worker.check_connectivity", side_effect=fake_check):
            result = connectivity_check_payload({"settings": settings.model_dump(mode="json")})

        self.assertTrue(result["ok"])
        self.assertEqual(result["model"], "draft-model")


if __name__ == "__main__":
    unittest.main(verbosity=2)
