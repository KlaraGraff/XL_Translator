"""开始任务前：逐页审核开着、审核模型还没确认能识别图片时，当场发一张测试图。

结论决定这次能不能直接开始：
* 不能识别图片 → 拦下（pdf_review_no_image_input），出路是「本次不审核，直接开始」；
* 回了话却没读出测试图 → 拦下（pdf_review_image_unseen），可以不审核，也可以仍用它审核；
* 读对了 → 直接开始，以后不再测；
* 没结论（网络、密钥） → 不拦，交给任务中的保底，也不让用户在「开始」时再等一轮。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from PIL import Image

import api.surfaces.pdf as pdf_surface
from api.app import create_app
from api.task_manager import TranslationTaskManager
from core.image_input_check import IMAGE_INPUT_NO, IMAGE_INPUT_UNSEEN, IMAGE_INPUT_YES, image_input_verdict
from core.model_api_identity import TaskApiContext
from core.model_roles import ROLE_PDF_REVIEW, SOURCE_INDEPENDENT, resolve_effective_model_config
from core.pdf_image_translation import PdfFileItem
from settings import AppSettings, load_settings, save_settings, set_cloud_provider_config
from tests.app_data_isolation import IsolatedAppDataTestCase
from tests.image_probe_reader import read_probe_png


def _refusal() -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://review.example/v1/chat/completions")
    response = httpx.Response(
        400,
        json={"error": {"message": "image input is not supported by this model"}},
        request=request,
    )
    return httpx.HTTPStatusError("400", request=request, response=response)


class _FakeReviewClient:
    """代替 OpenAICompatiblePdfReviewClient；``mode`` 决定它像哪种模型。"""

    mode = "reads"
    calls: list[float] = []

    def __init__(self, *, timeout_seconds: float = 0.0, **_kwargs) -> None:
        self.timeout_seconds = timeout_seconds

    def read_images(self, *, model_config, instructions, user_text, images):  # noqa: ARG002
        type(self).calls.append(self.timeout_seconds)
        if self.mode == "reads":
            return read_probe_png(Path(images[0]).read_bytes())
        if self.mode == "refuses":
            raise RuntimeError("审核模型请求失败") from _refusal()
        if self.mode == "drops_image":
            return '{"pass": true, "summary": "ok"}'
        request = httpx.Request("POST", "https://review.example/v1/chat/completions")
        raise httpx.ConnectError("connection refused", request=request)


def _write_pdf(path: Path) -> None:
    Image.new("RGB", (120, 160), "white").save(path, format="PDF")


def _settings(root: Path) -> AppSettings:
    settings = AppSettings()
    settings.pdf.target_lang = "en"
    settings.pdf.review_enabled = True
    settings.pdf_output.use_custom_output_dir = True
    settings.pdf_output.custom_output_dir = str(root / "pdf-output")
    for role, model, base_url in (
        (settings.image_model_role, "mock-image-model", "https://image.example/v1"),
        (settings.pdf_review_model_role, "mock-review-model", "https://review.example/v1"),
    ):
        role.source_role = SOURCE_INDEPENDENT
        role.cloud_provider = "custom_openai"
        set_cloud_provider_config(role, "custom_openai", cloud_model=model, cloud_base_url=base_url)
        role.cloud_model = model
        role.cloud_base_url = base_url
    return settings


class ReviewImageInputPreflightTests(IsolatedAppDataTestCase):
    def setUp(self) -> None:
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        _write_pdf(self.root / "source.pdf")
        _FakeReviewClient.calls = []
        pdf_surface._INCONCLUSIVE_PROBES.clear()
        self.addCleanup(pdf_surface._INCONCLUSIVE_PROBES.clear)
        self.captured: list[dict] = []
        for patcher in (
            patch("core.model_roles.get_key", return_value="review-secret"),
            patch("core.pdf_review.OpenAICompatiblePdfReviewClient", _FakeReviewClient),
            patch("api.task_manager.task_api_context_for_page", return_value=TaskApiContext(frozenset(), {})),
            patch("api.task_manager.threading.Thread"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _client(self, settings: AppSettings | None = None) -> TestClient:
        save_settings(settings or _settings(self.root))
        # 和正式运行一样每次从磁盘读设置：开始前存下的结论下一次就查得到。
        manager = TranslationTaskManager(settings_loader=load_settings)
        manager._scan = lambda *_args: [PdfFileItem(self.root / "source.pdf", "source", 1.0, page_count=1)]

        class _Dormant:
            def start(self) -> None:
                return None

            def stop(self) -> None:
                return None

            def needs_poll(self) -> bool:
                return True

            def get_message(self, timeout: float = 0.05):  # noqa: ARG002
                return None

        manager._build_runner = lambda **kwargs: self.captured.append(kwargs) or _Dormant()
        return TestClient(create_app(task_manager=manager))

    def _start(self, client: TestClient, **extra):
        return client.post("/api/tasks", json={"surface": "pdf", "source_path": str(self.root), **extra})

    def _preflight(self, client: TestClient, **extra):
        # 界面点「开始」时先预检、再开始，两次都会走到开始前检查。
        return client.post("/api/tasks/preflight", json={"surface": "pdf", "source_path": str(self.root), **extra})

    def _stored_verdict(self) -> str:
        stored = load_settings()
        return image_input_verdict(stored, resolve_effective_model_config(stored, ROLE_PDF_REVIEW))

    def test_a_model_that_refuses_images_is_stopped_before_any_page_is_sent(self) -> None:
        _FakeReviewClient.mode = "refuses"
        client = self._client()

        blocked = self._start(client)

        self.assertEqual(blocked.status_code, 422, blocked.text)
        self.assertEqual(blocked.json()["reason"], "pdf_review_no_image_input")
        self.assertIn("不能识别图片", blocked.json()["detail"])
        self.assertEqual(self.captured, [])
        # 只等一次、等得不久。
        self.assertEqual(_FakeReviewClient.calls, [pdf_surface.PREFLIGHT_PROBE_TIMEOUT_SECONDS])
        # 结论和「测试失败」都存进了设置：设置页看得到，下次不用再测。
        self.assertEqual(self._stored_verdict(), IMAGE_INPUT_NO)
        self.assertEqual(load_settings().pdf_review_model_role.availability_status, "unavailable")

        again = self._start(client)
        self.assertEqual(again.json()["reason"], "pdf_review_no_image_input")
        self.assertEqual(len(_FakeReviewClient.calls), 1)

        # 「仍用它审核」对不收图的模型没有意义，不放行。
        forced = self._start(client, allow_known_review_failure=True)
        self.assertEqual(forced.json()["reason"], "pdf_review_no_image_input")

    def test_skip_review_this_time_starts_without_touching_the_setting(self) -> None:
        _FakeReviewClient.mode = "refuses"
        client = self._client()
        self.assertEqual(self._start(client).status_code, 422)

        accepted = self._start(client, allow_skip_pdf_review=True)

        self.assertEqual(accepted.status_code, 202, accepted.text)
        self.assertFalse(self.captured[0]["settings"].pdf.review_enabled)
        self.assertTrue(load_settings().pdf.review_enabled)

    def test_a_reply_that_misses_the_code_asks_the_user(self) -> None:
        _FakeReviewClient.mode = "drops_image"
        client = self._client()

        blocked = self._start(client)

        self.assertEqual(blocked.status_code, 422, blocked.text)
        self.assertEqual(blocked.json()["reason"], "pdf_review_image_unseen")
        self.assertEqual(self._stored_verdict(), IMAGE_INPUT_UNSEEN)

        # 用户选「仍用它审核」：照常审核，不再测。
        accepted = self._start(client, allow_known_review_failure=True)
        self.assertEqual(accepted.status_code, 202, accepted.text)
        self.assertTrue(self.captured[0]["settings"].pdf.review_enabled)
        self.assertEqual(len(_FakeReviewClient.calls), 1)

    def test_a_model_that_reads_the_code_starts_and_is_not_tested_again(self) -> None:
        _FakeReviewClient.mode = "reads"
        settings = _settings(self.root)
        # 以前测试失败过；刚刚实测读对了，比那次新，不再拿旧结论拦。
        settings.pdf_review_model_role.availability_status = "unavailable"
        client = self._client(settings)

        first = self._preflight(client)
        second = self._start(client)

        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 202, second.text)
        self.assertEqual(len(_FakeReviewClient.calls), 1)
        self.assertEqual(self._stored_verdict(), IMAGE_INPUT_YES)
        self.assertEqual(load_settings().pdf_review_model_role.availability_status, "available")

    def test_no_verdict_does_not_block_and_is_not_retried_right_away(self) -> None:
        _FakeReviewClient.mode = "unreachable"
        client = self._client()

        first = self._preflight(client)
        second = self._start(client)

        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 202, second.text)
        self.assertEqual(len(_FakeReviewClient.calls), 1)
        self.assertEqual(self._stored_verdict(), "")

    def test_review_switched_off_sends_no_test_image(self) -> None:
        settings = _settings(self.root)
        settings.pdf.review_enabled = False
        client = self._client(settings)

        self.assertEqual(self._start(client).status_code, 202)
        self.assertEqual(_FakeReviewClient.calls, [])
