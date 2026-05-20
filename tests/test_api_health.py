from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from core.api_health import (
    API_HEALTH_FAILED,
    API_HEALTH_OK,
    ApiHealthRecord,
    build_connectivity_signature,
    load_api_health_record,
    run_api_health_check,
    save_api_health_record,
    should_check_api_health_on_startup,
)
from core.connectivity_check import ConnectivityResult
from settings import AppSettings, EngineSettings


class ApiHealthTests(unittest.TestCase):
    def _settings(self) -> AppSettings:
        return AppSettings(
            engine=EngineSettings(
                mode="cloud",
                cloud_provider="custom_openai",
                cloud_model="gpt-5.4",
                cloud_base_url="https://api.example.test/v1",
            )
        )

    def _record(
        self,
        *,
        signature: str,
        last_status: str = API_HEALTH_OK,
        last_checked_date: str = "2026-05-20",
    ) -> ApiHealthRecord:
        return ApiHealthRecord(
            signature=signature,
            last_status=last_status,
            last_checked_date=last_checked_date,
            checked_at="2026-05-20T00:00:00+00:00",
            result_status="ok" if last_status == API_HEALTH_OK else "request_failed",
            message="",
        )

    def test_signature_hashes_api_key_without_exposing_secret(self) -> None:
        settings = self._settings()

        with patch("core.api_health.get_key", return_value="secret-token"):
            signature = build_connectivity_signature(settings)

        self.assertIn("custom_openai", signature)
        self.assertNotIn("secret-token", signature)
        self.assertRegex(signature.split("|")[-1], r"^[0-9a-f]{12}$")

    def test_ok_record_checked_today_skips_startup_check(self) -> None:
        settings = self._settings()
        today = date(2026, 5, 20)
        with patch("core.api_health.get_key", return_value="secret"):
            signature = build_connectivity_signature(settings)
            record = self._record(signature=signature, last_checked_date=today.isoformat())
            should_check = should_check_api_health_on_startup(settings, record=record, today=today)

        self.assertFalse(should_check)

    def test_ok_record_from_previous_day_checks_again(self) -> None:
        settings = self._settings()
        today = date(2026, 5, 20)
        with patch("core.api_health.get_key", return_value="secret"):
            signature = build_connectivity_signature(settings)
            record = self._record(signature=signature, last_checked_date="2026-05-19")
            should_check = should_check_api_health_on_startup(settings, record=record, today=today)

        self.assertTrue(should_check)

    def test_failed_record_checks_on_startup_even_when_checked_today(self) -> None:
        settings = self._settings()
        today = date(2026, 5, 20)
        with patch("core.api_health.get_key", return_value="secret"):
            signature = build_connectivity_signature(settings)
            record = self._record(
                signature=signature,
                last_status=API_HEALTH_FAILED,
                last_checked_date=today.isoformat(),
            )
            should_check = should_check_api_health_on_startup(settings, record=record, today=today)

        self.assertTrue(should_check)

    def test_signature_change_checks_again(self) -> None:
        settings = self._settings()
        today = date(2026, 5, 20)
        record = self._record(signature="cloud|custom_openai|old")

        with patch("core.api_health.get_key", return_value="secret"):
            should_check = should_check_api_health_on_startup(settings, record=record, today=today)

        self.assertTrue(should_check)

    def test_save_and_load_record_round_trip(self) -> None:
        record = self._record(signature="cloud|custom_openai|gpt-5.4")
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "api_health_state.json"
            save_api_health_record(record, path)
            loaded = load_api_health_record(path)

        self.assertEqual(loaded, record)

    def test_corrupt_record_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "api_health_state.json"
            path.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")

            loaded = load_api_health_record(path)

        self.assertIsNone(loaded)

    def test_run_api_health_check_persists_failure(self) -> None:
        settings = self._settings()
        today = date(2026, 5, 20)

        def fake_checker(_settings):
            return ConnectivityResult(
                ok=False,
                status="request_failed",
                message="连接测试失败：401 Unauthorized",
                provider="custom_openai",
                model="gpt-5.4",
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "api_health_state.json"
            with patch("core.api_health.get_key", return_value="secret"):
                record = run_api_health_check(
                    settings,
                    today=today,
                    checker=fake_checker,
                    state_path=path,
                )
            loaded = load_api_health_record(path)

        self.assertEqual(record.last_status, API_HEALTH_FAILED)
        self.assertEqual(record.last_checked_date, today.isoformat())
        self.assertEqual(loaded, record)


if __name__ == "__main__":
    unittest.main(verbosity=2)
