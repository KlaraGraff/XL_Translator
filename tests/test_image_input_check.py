"""「模型能不能识别图片」：测试图、报错归类、结论存放、任务中的保底。

真实的服务商请求路线在 test_deepseek_provider / test_xiaomi_provider /
test_siliconflow_pdf_routes 里测；这里只测 core/image_input_check.py 本身。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from core.image_input_check import (
    IMAGE_INPUT_NO,
    IMAGE_INPUT_UNSEEN,
    IMAGE_INPUT_YES,
    MAX_STORED_CHECKS,
    PROBE_CODE_ALPHABET,
    PROBE_ERROR,
    PROBE_GLYPHS,
    ImageInputGuard,
    draw_probe_image,
    image_input_key,
    image_input_verdict,
    image_model_refused_page,
    looks_like_no_image_input,
    new_probe_code,
    probe_image_input,
    reply_has_code,
    save_image_input_verdict,
    save_role_availability,
    set_image_input_verdict,
)
from core.model_roles import (
    ROLE_IMAGE,
    ROLE_PDF_REVIEW,
    SOURCE_INDEPENDENT,
    resolve_effective_model_config,
)
from settings import AppSettings, ImageInputCheck, load_settings, save_settings
from tests.app_data_isolation import IsolatedAppDataTestCase
from tests.image_probe_reader import read_probe_png


def _http_error(status: int, body, url: str = "https://api.example/v1/chat/completions") -> httpx.HTTPStatusError:
    text = body if isinstance(body, str) else json.dumps(body)
    request = httpx.Request("POST", url)
    response = httpx.Response(status, text=text, request=request)
    return httpx.HTTPStatusError(f"{status} error", request=request, response=response)


def _wrapped(inner: BaseException, message: str = "审核模型请求失败") -> RuntimeError:
    try:
        raise RuntimeError(message) from inner
    except RuntimeError as exc:
        return exc


def _review_settings(model: str = "vision-model", base_url: str = "https://api.example/v1") -> AppSettings:
    settings = AppSettings()
    role = settings.pdf_review_model_role
    role.source_role = SOURCE_INDEPENDENT
    role.cloud_provider = "custom_openai"
    role.cloud_model = model
    role.cloud_base_url = base_url
    return settings


class ProbeImageTests(unittest.TestCase):
    def test_every_glyph_reads_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for index in range(0, len(PROBE_CODE_ALPHABET), 4):
                code = PROBE_CODE_ALPHABET[index:index + 4]
                path = draw_probe_image(code, Path(tmp) / f"{index}.png")
                self.assertEqual(read_probe_png(path.read_bytes()), code)

    def test_glyphs_are_distinct_and_cover_the_alphabet(self) -> None:
        self.assertEqual(set(PROBE_GLYPHS), set(PROBE_CODE_ALPHABET))
        self.assertEqual(len(set(PROBE_GLYPHS.values())), len(PROBE_GLYPHS))
        for shape in PROBE_GLYPHS.values():
            self.assertEqual(len(shape), 7)
            self.assertTrue(all(len(line) == 5 for line in shape))

    def test_codes_are_random_and_avoid_lookalikes(self) -> None:
        codes = {new_probe_code() for _ in range(50)}
        self.assertGreater(len(codes), 40)
        for code in codes:
            self.assertEqual(len(code), 4)
            self.assertFalse(set(code) & set("0O1I2Z5S8B6G"))

    def test_reply_match_ignores_case_spaces_and_dashes(self) -> None:
        self.assertTrue(reply_has_code("a c-d 3", "ACD3"))
        self.assertTrue(reply_has_code("The characters are: ACD3.", "ACD3"))
        self.assertFalse(reply_has_code("ACD", "ACD3"))
        self.assertFalse(reply_has_code('{"pass": true}', "ACD3"))
        self.assertFalse(reply_has_code("anything", ""))


class NoImageInputClassifierTests(unittest.TestCase):
    def test_refusals_from_real_providers(self) -> None:
        samples = [
            # DeepSeek：不收图的型号
            (400, {"error": {"message": "image input is not supported by this model"}}),
            # 硅基流动：非视觉模型（2026-10-21 实测原话）
            (400, {"code": 20041, "message": "The model is not a VLM (Vision Language Model). Please use text-only prompts."}),
            # 只认文本的 OpenAI 兼容服务
            (400, {"error": {"message": "unknown variant `image_url`, expected `text`"}}),
            (400, {"error": {"message": "This model does not support image input."}}),
            (422, {"detail": "Multimodal input is not supported for this model"}),
            (404, {"error": {"message": "No endpoints found that support image input"}}),
            (400, {"error": {"message": "该模型不支持图片输入"}}),
        ]
        for status, body in samples:
            with self.subTest(body=body):
                self.assertTrue(looks_like_no_image_input(_http_error(status, body)))

    def test_problems_a_new_model_would_not_fix(self) -> None:
        samples = [
            (401, {"error": {"message": "Invalid API key; image input not supported"}}),
            (429, {"error": {"message": "Too many requests"}}),
            (413, {"error": {"message": "image not supported: request too large"}}),
            (400, {"error": {"message": "Image too large: exceeds maximum pixels; not supported"}}),
            (400, {"error": {"message": "Unsupported image format"}}),
            (400, {"error": {"message": "image rejected by content_policy (not supported)"}}),
            (404, {"error": {"message": "The model `vision-x` does not exist"}}),
            (500, {"error": {"message": "image input is not supported by this model"}}),
            (400, {"error": {"message": "max_tokens is too large"}}),
            (400, {"error": {"message": "Model not supported"}}),
        ]
        for status, body in samples:
            with self.subTest(body=body):
                self.assertFalse(looks_like_no_image_input(_http_error(status, body)))

    def test_network_errors_and_plain_errors_are_not_refusals(self) -> None:
        request = httpx.Request("POST", "https://api.example/v1/chat/completions")
        self.assertFalse(looks_like_no_image_input(httpx.ConnectError("vision not supported", request=request)))
        self.assertFalse(looks_like_no_image_input(TimeoutError("image not supported")))
        self.assertFalse(looks_like_no_image_input(RuntimeError("image input is not supported")))

    def test_follows_wrapped_errors_to_the_provider_reply(self) -> None:
        inner = _http_error(400, {"error": {"message": "image input is not supported by this model"}})
        self.assertTrue(looks_like_no_image_input(_wrapped(inner)))

    def test_does_not_count_an_error_met_while_handling_another(self) -> None:
        # 自动模式先试 /responses 拿到 404，再试 chat：那个 404 不该算在 chat 这次的头上。
        earlier = _http_error(400, {"error": {"message": "image input is not supported by this model"}})
        try:
            try:
                raise earlier
            except httpx.HTTPStatusError:
                raise RuntimeError("chat 也失败了")
        except RuntimeError as exc:
            self.assertFalse(looks_like_no_image_input(exc))

    def test_words_in_the_request_address_do_not_count(self) -> None:
        exc = _http_error(400, {"error": {"message": "bad request: not supported"}}, url="https://vision.example/v1/chat")
        self.assertFalse(looks_like_no_image_input(exc))


class ProbeTests(unittest.TestCase):
    def test_reading_the_code_means_yes(self) -> None:
        def ask(_instructions, _text, images):
            self.assertEqual(len(images), 1)
            return f"The text is {read_probe_png(Path(images[0]).read_bytes())}"

        probe = probe_image_input(ask)
        self.assertEqual(probe.result, IMAGE_INPUT_YES)

    def test_a_refusal_means_no(self) -> None:
        def ask(*_args):
            raise _wrapped(_http_error(400, {"error": {"message": "image input is not supported by this model"}}))

        probe = probe_image_input(ask, attempts=3)
        self.assertEqual(probe.result, IMAGE_INPUT_NO)
        self.assertIsNotNone(probe.error)

    def test_a_reply_without_the_code_means_unseen(self) -> None:
        calls = []

        def ask(*_args):
            calls.append(1)
            return '{"pass": true, "summary": "ok"}'

        probe = probe_image_input(ask, attempts=3)
        self.assertEqual(probe.result, IMAGE_INPUT_UNSEEN)
        self.assertIn("pass", probe.reply)
        # 读不出也是结论，不再重试。
        self.assertEqual(len(calls), 1)

    def test_only_errors_without_a_verdict_are_retried(self) -> None:
        calls = []

        def ask(_instructions, _text, images):
            calls.append(1)
            if len(calls) == 1:
                raise _http_error(503, {"error": {"message": "overloaded"}})
            return read_probe_png(Path(images[0]).read_bytes())

        probe = probe_image_input(ask, attempts=2)
        self.assertEqual(probe.result, IMAGE_INPUT_YES)
        self.assertEqual(len(calls), 2)

    def test_errors_all_the_way_mean_no_verdict(self) -> None:
        def ask(*_args):
            raise _http_error(401, {"error": {"message": "invalid api key"}})

        probe = probe_image_input(ask, attempts=2)
        self.assertEqual(probe.result, PROBE_ERROR)
        self.assertEqual(probe.message, "")


class GuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.refusal = _http_error(400, {"error": {"message": "image input is not supported by this model"}})
        self.other = _http_error(400, {"error": {"message": "max_tokens is too large"}})

    def test_two_different_pages_trip_it_once(self) -> None:
        guard = ImageInputGuard()
        self.assertFalse(guard.failed(self.refusal, key="p1"))
        self.assertTrue(guard.failed(self.refusal, key="p2"))
        self.assertTrue(guard.tripped)
        self.assertFalse(guard.failed(self.refusal, key="p3"))

    def test_retries_of_one_page_count_once(self) -> None:
        guard = ImageInputGuard()
        for _ in range(4):
            self.assertFalse(guard.failed(self.refusal, key="p1"))
        self.assertFalse(guard.tripped)

    def test_never_trips_after_a_success(self) -> None:
        guard = ImageInputGuard()
        guard.succeeded()
        self.assertFalse(guard.failed(self.refusal, key="p1"))
        self.assertFalse(guard.failed(self.refusal, key="p2"))
        self.assertFalse(guard.tripped)

    def test_an_unrecognized_failure_breaks_the_streak(self) -> None:
        guard = ImageInputGuard()
        self.assertFalse(guard.failed(self.refusal, key="p1"))
        self.assertFalse(guard.failed(self.other, key="p2"))
        self.assertFalse(guard.failed(self.refusal, key="p3"))
        self.assertTrue(guard.failed(self.refusal, key="p4"))


class ImageModelRefusedPageTests(unittest.TestCase):
    def test_a_text_model_on_the_image_endpoint_counts(self) -> None:
        exc = _wrapped(_http_error(404, {"error": {"message": "model does not support image generation"}}))
        self.assertTrue(image_model_refused_page(exc))
        self.assertTrue(image_model_refused_page(ValueError("图像模型没有返回可用图片")))

    def test_page_specific_and_account_problems_do_not(self) -> None:
        for exc in (
            _http_error(400, {"error": {"message": "Image too large: exceeds maximum pixels"}}),
            _http_error(400, {"error": {"message": "Request rejected by content_policy"}}),
            _http_error(401, {"error": {"message": "invalid api key"}}),
            _http_error(429, {"error": {"message": "rate limit"}}),
        ):
            with self.subTest(exc=str(exc)):
                self.assertFalse(image_model_refused_page(exc))


class VerdictStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch("core.model_roles.get_key", return_value="sk-one")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_key_follows_the_connection_not_the_role(self) -> None:
        settings = _review_settings()
        review = resolve_effective_model_config(settings, ROLE_PDF_REVIEW)
        settings.engine.mode = "cloud"
        settings.engine.cloud_provider = "custom_openai"
        settings.engine.cloud_model = "vision-model"
        settings.engine.cloud_base_url = "https://api.example/v1/"
        settings.pdf_review_model_role.source_role = "translation"
        followed = resolve_effective_model_config(settings, ROLE_PDF_REVIEW)
        self.assertEqual(image_input_key(review), image_input_key(followed))

    def test_changing_model_address_or_key_forgets_the_verdict(self) -> None:
        settings = _review_settings()
        config = resolve_effective_model_config(settings, ROLE_PDF_REVIEW)
        set_image_input_verdict(settings, config, IMAGE_INPUT_NO, "no", "2026-10-31T10:00:00")
        self.assertEqual(image_input_verdict(settings, config), IMAGE_INPUT_NO)

        settings.pdf_review_model_role.cloud_model = "other-model"
        self.assertEqual(image_input_verdict(settings, resolve_effective_model_config(settings, ROLE_PDF_REVIEW)), "")
        settings.pdf_review_model_role.cloud_model = "vision-model"
        settings.pdf_review_model_role.cloud_base_url = "https://relay.example/v1"
        self.assertEqual(image_input_verdict(settings, resolve_effective_model_config(settings, ROLE_PDF_REVIEW)), "")
        settings.pdf_review_model_role.cloud_base_url = "https://api.example/v1"
        with patch("core.model_roles.get_key", return_value="sk-two"):
            self.assertEqual(image_input_verdict(settings, resolve_effective_model_config(settings, ROLE_PDF_REVIEW)), "")
        self.assertEqual(image_input_verdict(settings, resolve_effective_model_config(settings, ROLE_PDF_REVIEW)), IMAGE_INPUT_NO)

    def test_the_key_never_appears_in_settings(self) -> None:
        settings = _review_settings()
        config = resolve_effective_model_config(settings, ROLE_PDF_REVIEW)
        set_image_input_verdict(settings, config, IMAGE_INPUT_YES, "ok", "2026-10-31T10:00:00")
        self.assertNotIn("sk-one", settings.model_dump_json())

    def test_keeps_only_the_newest_checks(self) -> None:
        settings = _review_settings()
        for index in range(MAX_STORED_CHECKS + 5):
            settings.pdf_review_model_role.cloud_model = f"model-{index:02d}"
            config = resolve_effective_model_config(settings, ROLE_PDF_REVIEW)
            set_image_input_verdict(settings, config, IMAGE_INPUT_YES, "ok", f"2026-10-31T10:{index:02d}:00")
        self.assertEqual(len(settings.image_input_checks), MAX_STORED_CHECKS)
        models = {entry.model for entry in settings.image_input_checks.values()}
        self.assertNotIn("model-00", models)
        self.assertIn(f"model-{MAX_STORED_CHECKS + 4:02d}", models)

    def test_unknown_results_are_ignored(self) -> None:
        settings = _review_settings()
        config = resolve_effective_model_config(settings, ROLE_PDF_REVIEW)
        set_image_input_verdict(settings, config, PROBE_ERROR, "", "2026-10-31T10:00:00")
        self.assertEqual(settings.image_input_checks, {})
        self.assertEqual(ImageInputCheck(result="maybe").result, "")
        loaded = AppSettings.model_validate(
            {"image_input_checks": {"abc": {"result": "garbage", "model": " m "}}}
        )
        self.assertEqual(loaded.image_input_checks["abc"].result, "")
        self.assertEqual(loaded.image_input_checks["abc"].model, "m")


class PersistVerdictTests(IsolatedAppDataTestCase):
    def setUp(self) -> None:
        super().setUp()
        patcher = patch("core.model_roles.get_key", return_value="sk-one")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_saves_the_verdict_and_the_role_status_without_overwriting_other_edits(self) -> None:
        save_settings(_review_settings())
        task_copy = load_settings()
        config = resolve_effective_model_config(task_copy, ROLE_PDF_REVIEW)
        # 任务跑的时候用户改了别的设置：存结论时不能拿任务那份旧设置盖回去。
        latest = load_settings()
        latest.target_lang = "fr"
        save_settings(latest)

        save_image_input_verdict(
            config, IMAGE_INPUT_NO, "不能识别图片", settings=task_copy, role=ROLE_PDF_REVIEW,
            availability_message="图像翻译审核模型连接测试失败：换模型",
        )

        self.assertEqual(image_input_verdict(task_copy, config), IMAGE_INPUT_NO)
        stored = load_settings()
        self.assertEqual(stored.target_lang, "fr")
        self.assertEqual(image_input_verdict(stored, config), IMAGE_INPUT_NO)
        self.assertEqual(stored.pdf_review_model_role.availability_status, "unavailable")
        self.assertEqual(stored.pdf_review_model_role.availability_message, "图像翻译审核模型连接测试失败：换模型")

    def test_leaves_the_role_alone_when_the_user_switched_models_meanwhile(self) -> None:
        save_settings(_review_settings())
        config = resolve_effective_model_config(load_settings(), ROLE_PDF_REVIEW)
        save_settings(_review_settings("another-model"))
        self.assertEqual(resolve_effective_model_config(load_settings(), ROLE_PDF_REVIEW).model, "another-model")

        save_image_input_verdict(config, IMAGE_INPUT_NO, "不能识别图片", role=ROLE_PDF_REVIEW)

        stored = load_settings()
        # 结论照样记在旧那条路上（以后换回来还查得到），但新模型的状态不动。
        self.assertEqual(image_input_verdict(stored, config), IMAGE_INPUT_NO)
        self.assertNotEqual(stored.pdf_review_model_role.availability_status, "unavailable")

    def test_records_an_image_model_found_failing_in_a_task(self) -> None:
        settings = AppSettings()
        role = settings.image_model_role
        role.source_role = SOURCE_INDEPENDENT
        role.cloud_provider = "custom_openai"
        role.cloud_model = "text-model"
        role.cloud_base_url = "https://api.example/v1"
        save_settings(settings)
        config = resolve_effective_model_config(load_settings(), ROLE_IMAGE)

        save_role_availability(config, ROLE_IMAGE, ok=False, message="图像翻译模型连接测试失败：换模型")

        stored = load_settings()
        self.assertEqual(stored.image_model_role.availability_status, "unavailable")
        self.assertEqual(stored.image_model_role.availability_message, "图像翻译模型连接测试失败：换模型")


if __name__ == "__main__":
    unittest.main()
