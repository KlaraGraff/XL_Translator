"""Phase 3 acceptance contracts using isolated settings and mock providers.

The tests deliberately exercise the public API and role-level connectivity
helpers without sending traffic to a real provider.  They are the regression
gate for M3A/M3B/M3C decisions that cannot be demonstrated by a catalogue
listing alone.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

import settings as settings_module
from api.app import create_app
from api.task_manager import TranslationTaskManager
from core import diagnostics, tm_manager
from core.image_generation import check_image_generation_connectivity
from core.model_config import apply_model_config_import, parse_model_config_import
from core.model_roles import (
    ROLE_CLEANER,
    ROLE_IMAGE,
    ROLE_PDF_REVIEW,
    ROLE_TRANSLATION,
    model_config_signature,
    resolve_effective_model_config,
)
from core.model_throughput import set_model_throughput
from core.pdf_review import (
    PdfPageReviewResult,
    check_pdf_review_connectivity,
)
from settings import AppSettings, api_key_scope, set_cloud_provider_config


class _MockResponse:
    status_code = 200
    text = ""

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, str]:
        return {"id": "mock-response"}


class _MockTextProvider:
    """Minimal OpenAI-compatible transport; never makes a network request."""

    def __init__(self) -> None:
        self.post_calls: list[dict[str, object]] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:  # noqa: ANN001
        return None

    def post(self, url: str, *, headers=None, json=None) -> _MockResponse:
        self.post_calls.append({"url": url, "headers": headers, "json": json})
        return _MockResponse()


class _DormantRunner:
    """Runner used to inspect the immutable task inputs before work begins."""

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def needs_poll(self) -> bool:
        return True

    def get_message(self, timeout: float = 0.05):  # noqa: ARG002
        return None


class Phase3ApiAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = TemporaryDirectory()
        self.root = Path(self._temporary_directory.name)
        self.addCleanup(self._temporary_directory.cleanup)
        self._patchers = [
            patch.multiple(
                settings_module,
                APP_DATA_DIR=self.root / "app-data",
                SETTINGS_PATH=self.root / "app-data" / "settings.json",
                KEYS_PATH=self.root / "app-data" / "keys.json",
            ),
            patch.object(tm_manager, "DB_PATH", self.root / "app-data" / "tm.db"),
            patch.object(diagnostics, "DIAGNOSTIC_RECORDS_DIR", self.root / "diagnostics"),
            patch.object(diagnostics, "LOG_PATH", self.root / "app-data" / "app.log"),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(create_app())

    def _configure_mock_translation(self, *, model: str = "mock-text-v1") -> None:
        response = self.client.put(
            "/api/models/roles/translation",
            json={
                "mode": "cloud",
                "provider": "custom_openai",
                "model": model,
                "base_url": "https://mock-provider.example/v1",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        response = self.client.put(
            "/api/keys/custom_openai",
            json={
                "api_key": "mock-provider-secret",
                "base_url": "https://mock-provider.example/v1",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)

    def test_mock_text_provider_records_current_signature_then_model_change_invalidates_it(self) -> None:
        """M3A-08/M3C-05/06: test status is tied to one effective config."""
        self._configure_mock_translation()
        provider = _MockTextProvider()

        with patch(
            "core.connectivity_check.httpx.Client",
            autospec=True,
            return_value=provider,
        ):
            tested = self.client.post("/api/models/connectivity/translation")

        self.assertEqual(tested.status_code, 200, tested.text)
        self.assertTrue(tested.json()["ok"])
        self.assertEqual(len(provider.post_calls), 1)
        request = provider.post_calls[0]
        self.assertEqual(request["url"], "https://mock-provider.example/v1/chat/completions")
        self.assertEqual(
            request["headers"]["Authorization"],  # type: ignore[index]
            "Bearer mock-provider-secret",
        )

        role = self.client.get("/api/models/roles").json()["roles"]["translation"]
        self.assertEqual(role["availability_status"], "available")
        self.assertTrue(role["availability_signature"])
        self.assertNotIn("mock-provider-secret", repr(role))

        changed = self.client.put(
            "/api/models/roles/translation",
            json={"model": "mock-text-v2"},
        )
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(changed.json()["availability_status"], "unknown")
        # The role payload may expose the current computed signature for
        # diagnosis; the persisted previously-tested signature is cleared.
        self.assertEqual(
            self.client.get("/api/settings").json()["engine"]["availability_signature"],
            "",
        )

    def test_mock_cleaner_provider_test_persists_the_cleaner_own_status(self) -> None:
        """M3A-03/M3C-05: following a connection does not borrow its test state."""
        self._configure_mock_translation()
        configured = self.client.put(
            "/api/models/roles/cleaner",
            json={"source_role": "translation", "model": "mock-cleaner-v1"},
        )
        self.assertEqual(configured.status_code, 200, configured.text)
        class CleanerProvider:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            def chat(self, system: str, user: str) -> str:
                self.calls.append((system, user))
                return '[{"id":"connectivity-probe","suggested":"Concrete"}]'

        provider = CleanerProvider()
        with patch("core.engine_dispatcher.build_engine", return_value=provider):
            tested = self.client.post("/api/models/connectivity/cleaner")

        self.assertEqual(tested.status_code, 200, tested.text)
        self.assertTrue(tested.json()["ok"])
        self.assertEqual(len(provider.calls), 1)
        self.assertIn("JSON", provider.calls[0][0])
        self.assertIn("connectivity-probe", provider.calls[0][1])
        cleaner = self.client.get("/api/models/roles").json()["roles"]["cleaner"]
        self.assertEqual(cleaner["availability_status"], "available")
        self.assertTrue(cleaner["availability_signature"])
        self.assertNotIn("mock-provider-secret", repr(cleaner))

    def test_capability_rejection_happens_before_any_image_provider_request(self) -> None:
        """M3A-02: a text-only provider cannot be saved for the image role."""
        with patch("api.app.check_image_generation_connectivity") as checker:
            response = self.client.put(
                "/api/models/roles/image",
                json={
                    "source_role": "independent",
                    "provider": "claude",
                    "model": "text-only-model",
                    "base_url": "https://mock-provider.example/v1",
                },
            )

        self.assertEqual(response.status_code, 422)
        self.assertIn("不支持", response.json()["detail"])
        checker.assert_not_called()

    def test_sensitive_v3_export_requires_confirmation_and_default_export_never_leaks_key(self) -> None:
        """M3C-09/10: config exchange is v3 and secret output is explicit."""
        self._configure_mock_translation()

        default_export = self.client.get("/api/model-config/export")
        self.assertEqual(default_export.status_code, 200)
        self.assertEqual(default_export.json()["version"], 3)
        self.assertNotIn("mock-provider-secret", default_export.text)

        unconfirmed = self.client.get("/api/model-config/export?include_api_key=true")
        self.assertEqual(unconfirmed.status_code, 422)

        sensitive = self.client.get(
            "/api/model-config/export?include_api_key=true&confirm_sensitive=true"
        )
        self.assertEqual(sensitive.status_code, 200)
        self.assertIn("mock-provider-secret", sensitive.text)


class Phase3RoleMockProviderTests(unittest.TestCase):
    def _settings(self) -> AppSettings:
        settings = AppSettings()
        settings.image_model_role.source_role = "independent"
        set_cloud_provider_config(
            settings.image_model_role,
            "custom_openai",
            cloud_model="mock-image-v1",
            cloud_base_url="https://mock-image.example/v1",
        )
        settings.pdf_review_model_role.source_role = "independent"
        set_cloud_provider_config(
            settings.pdf_review_model_role,
            "custom_openai",
            cloud_model="mock-review-v1",
            cloud_base_url="https://mock-review.example/v1",
        )
        return settings

    def test_mock_image_and_review_providers_validate_role_specific_protocols(self) -> None:
        """M3C-03/05: image and review need their own successful protocol tests."""
        settings = self._settings()
        image_bytes = BytesIO()
        Image.new("RGB", (8, 8), "white").save(image_bytes, format="PNG")

        class ImageProvider:
            def __init__(self) -> None:
                self.config = None

            def generate_page(self, **kwargs):
                self.config = kwargs["model_config"]
                return image_bytes.getvalue()

        class ReviewProvider:
            def __init__(self) -> None:
                self.config = None

            def review_page(self, **kwargs):
                self.config = kwargs["model_config"]
                return PdfPageReviewResult(
                    passed=True,
                    blocking_issues=[],
                    minor_suggestions=[],
                    summary="mock review ok",
                    raw_text='{"pass": true}',
                )

        image_provider = ImageProvider()
        review_provider = ReviewProvider()
        with patch("core.model_roles.get_key", return_value="mock-role-secret"):
            image_result = check_image_generation_connectivity(
                settings,
                client=image_provider,
                max_attempts=1,
            )
            review_result = check_pdf_review_connectivity(
                settings,
                client=review_provider,
                max_attempts=1,
            )

        self.assertTrue(image_result.ok)
        self.assertTrue(review_result.ok)
        self.assertEqual(image_provider.config.role, ROLE_IMAGE)
        self.assertEqual(review_provider.config.role, ROLE_PDF_REVIEW)
        self.assertEqual(settings.image_model_role.availability_status, "available")
        self.assertEqual(settings.pdf_review_model_role.availability_status, "available")
        self.assertNotIn("mock-role-secret", settings.image_model_role.availability_signature)
        self.assertNotIn("mock-role-secret", settings.pdf_review_model_role.availability_signature)


class Phase3SnapshotAndExchangeAcceptanceTests(unittest.TestCase):
    def test_v3_import_rejects_invalid_effective_role_before_key_persistence(self) -> None:
        """M3A-02/M3C-11: import rejects an unusable role before saving keys."""
        imported = parse_model_config_import(
            {
                "type": "translator_model_config",
                "version": 3,
                "model_profiles": {
                    "pdf_translation": {
                        "source_role": "independent",
                        "cloud": {
                            "provider": "claude",
                            "model": "text-only-model",
                            "base_url": "https://mock-provider.example/v1",
                            "api_key": "must-not-save",
                        },
                    }
                },
            }
        )
        with patch("core.model_config.save_key") as save_key:
            with self.assertRaisesRegex(ValueError, "不支持"):
                apply_model_config_import(AppSettings(), imported)
        save_key.assert_not_called()

    def test_partial_v3_import_merges_explicit_values_but_invalidates_all_role_tests(self) -> None:
        """M3C-11: imports preserve unrelated config but no old test result remains valid."""
        settings = AppSettings()
        settings.engine.mode = "cloud"
        set_cloud_provider_config(
            settings.engine,
            "custom_openai",
            cloud_model="before-translation",
            cloud_base_url="https://before-translation.example/v1",
        )
        settings.image_model_role.source_role = "independent"
        set_cloud_provider_config(
            settings.image_model_role,
            "custom_openai",
            cloud_model="keep-image-model",
            cloud_base_url="https://keep-image.example/v1",
        )
        image_before = settings.image_model_role.model_copy(deep=True)

        owners = {
            ROLE_TRANSLATION: settings.engine,
            ROLE_CLEANER: settings.cleaner_model_role,
            ROLE_IMAGE: settings.image_model_role,
            ROLE_PDF_REVIEW: settings.pdf_review_model_role,
        }
        for role, owner in owners.items():
            owner.availability_status = "available"
            owner.availability_message = "old success"
            owner.availability_checked_at = "2026-07-24T00:00:00"
            owner.availability_signature = model_config_signature(
                resolve_effective_model_config(settings, role)
            )

        imported = parse_model_config_import(
            {
                "type": "translator_model_config",
                "version": 3,
                "model_profiles": {
                    "translation": {
                        "mode": "cloud",
                        "cloud": {
                            "provider": "custom_openai",
                            "model": "imported-translation",
                            "base_url": "https://imported.example/v1",
                        },
                        "throughput": {"batch_size": 7, "concurrency": 2},
                    }
                },
            }
        )
        with patch("core.model_config.save_key"):
            updated = apply_model_config_import(settings, imported)

        self.assertEqual(updated.engine.cloud_model, "imported-translation")
        self.assertEqual(updated.image_model_role.cloud_model, image_before.cloud_model)
        self.assertEqual(updated.image_model_role.cloud_base_url, image_before.cloud_base_url)
        for owner in (
            updated.engine,
            updated.cleaner_model_role,
            updated.image_model_role,
            updated.pdf_review_model_role,
        ):
            self.assertEqual(owner.availability_status, "unknown")
            self.assertEqual(owner.availability_signature, "")
            self.assertEqual(owner.availability_checked_at, "")

    def test_task_start_freezes_model_throughput_key_scope_and_page_prompt_state(self) -> None:
        """M3A-09/M3B-02/M3C-07: later settings edits cannot mutate a running task."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.xlsx"
            source.touch()
            settings = AppSettings(
                excel_domain_preset="资料管理场景",
                excel_target_lang="fr",
            )
            settings.engine.mode = "cloud"
            set_cloud_provider_config(
                settings.engine,
                "custom_openai",
                cloud_model="snapshot-model-v1",
                cloud_base_url="https://snapshot.example/v1",
            )
            config = resolve_effective_model_config(settings, ROLE_TRANSLATION)
            set_model_throughput(settings, config, batch_size=12, concurrency=2)

            captured: dict[str, object] = {}
            manager = TranslationTaskManager(settings_loader=lambda: settings)
            manager._scan = lambda *_args: [SimpleNamespace(path=source)]
            manager._build_runner = lambda **kwargs: (
                captured.update(kwargs) or _DormantRunner()
            )

            with (
                patch("api.task_manager.tm_manager.init_db"),
                patch("api.task_manager.threading.Thread") as thread_type,
                patch("core.api_config_check.get_key", return_value="mock-snapshot-key"),
                patch("core.model_roles.get_key", return_value="mock-snapshot-key"),
            ):
                started = manager.start_task(surface="excel", source_path=str(root))
                thread_type.return_value.start.assert_called_once_with()

            frozen_settings = captured["settings"]
            self.assertIsInstance(frozen_settings, AppSettings)
            settings.engine.cloud_model = "future-model-v2"
            settings.excel_domain_preset = "行政生活化场景"
            settings.excel_target_lang = "en"
            settings.model_throughput_profiles.clear()

            self.assertEqual(frozen_settings.engine.cloud_model, "snapshot-model-v1")
            self.assertEqual(frozen_settings.excel_domain_preset, "资料管理场景")
            self.assertEqual(frozen_settings.excel_target_lang, "fr")
            snapshot = started["model_snapshot"][ROLE_TRANSLATION]
            self.assertEqual(snapshot["model"], "snapshot-model-v1")
            self.assertEqual(snapshot["throughput"]["batch_size"], 12)
            self.assertEqual(snapshot["throughput"]["concurrency"], 2)
            self.assertEqual(
                snapshot["api_scope"],
                api_key_scope("custom_openai", "https://snapshot.example/v1"),
            )
            self.assertEqual(started["task_snapshot"]["target_lang"], "fr")
            self.assertEqual(started["task_snapshot"]["domain_preset"], "资料管理场景")


if __name__ == "__main__":
    unittest.main(verbosity=2)
