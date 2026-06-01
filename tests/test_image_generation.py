from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from core.image_generation import (
    GPT_IMAGE_2_MAX_EDGE,
    GPT_IMAGE_2_MAX_PIXELS,
    ImageModelUnavailableError,
    _pdf_page_image_size_for_model,
    build_pdf_image_translation_prompt,
    check_image_generation_connectivity,
)
from core.model_roles import SOURCE_INDEPENDENT
from settings import AppSettings


class ImageGenerationTests(unittest.TestCase):
    def test_pdf_prompt_uses_only_current_language_specific_rule(self) -> None:
        prompt = build_pdf_image_translation_prompt("法文", target_lang_code="fr")

        self.assertIn("Translate every readable visible text element", prompt)
        self.assertIn("concise professional French", prompt)
        self.assertIn("do not mirror, reorder, relayout", prompt)
        self.assertIn("do not omit source meaning", prompt)
        self.assertNotIn("concise professional English", prompt)
        self.assertNotIn("Simplified Chinese", prompt)
        self.assertNotIn("Modern Standard Arabic", prompt)

    def test_pdf_prompt_uses_english_specific_rule(self) -> None:
        prompt = build_pdf_image_translation_prompt("英文", target_lang_code="en")

        self.assertIn("concise professional English", prompt)
        self.assertIn("short noun phrases", prompt)
        self.assertNotIn("concise professional French", prompt)
        self.assertNotIn("Simplified Chinese", prompt)

    def test_pdf_prompt_uses_generic_rule_for_unconfigured_language(self) -> None:
        prompt = build_pdf_image_translation_prompt("德文", target_lang_code="de")

        self.assertIn("Use natural, concise 德文", prompt)
        self.assertIn("compact noun phrases", prompt)
        self.assertNotIn("concise professional English", prompt)
        self.assertNotIn("concise professional French", prompt)
        self.assertNotIn("Simplified Chinese", prompt)

    def test_pdf_prompt_uses_arabic_specific_rtl_without_table_mirroring(self) -> None:
        prompt = build_pdf_image_translation_prompt("阿拉伯语", target_lang_code="ar")

        self.assertIn("Modern Standard Arabic", prompt)
        self.assertIn("inside each original text box or table cell", prompt)
        self.assertIn("source leftmost column must remain leftmost", prompt)
        self.assertIn("Do not mirror the page, table, column order", prompt)
        self.assertIn("never move that cell", prompt)
        self.assertIn("mixed-language identifiers stable", prompt)
        self.assertNotIn("concise professional French", prompt)
        self.assertNotIn("concise professional English", prompt)

    def test_pdf_prompt_keeps_review_feedback_after_language_rule(self) -> None:
        prompt = build_pdf_image_translation_prompt(
            "中文",
            target_lang_code="zh",
            review_feedback="第 1 行仍有漏译。",
        )

        self.assertIn("Simplified Chinese", prompt)
        self.assertIn("Previous candidate review found blocking issues", prompt)
        self.assertTrue(prompt.endswith("第 1 行仍有漏译。"))

    def test_gpt_image_2_pdf_page_size_preserves_a4_ratio_and_limits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "page.png"
            Image.new("RGB", (2480, 3504), "white").save(source, format="PNG")

            size = _pdf_page_image_size_for_model(source, "gpt-image-2")

        width, height = [int(part) for part in size.split("x")]
        self.assertGreaterEqual(width, 1200)
        self.assertGreaterEqual(height, 1600)
        self.assertLessEqual(max(width, height), GPT_IMAGE_2_MAX_EDGE)
        self.assertLessEqual(width * height, GPT_IMAGE_2_MAX_PIXELS)
        self.assertEqual(width % 16, 0)
        self.assertEqual(height % 16, 0)
        self.assertLess(abs((width / height) - (2480 / 3504)) / (2480 / 3504), 0.01)

    def test_legacy_gpt_image_size_uses_supported_orientation_bucket(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            portrait = Path(tmp) / "portrait.png"
            landscape = Path(tmp) / "landscape.png"
            Image.new("RGB", (1200, 1800), "white").save(portrait, format="PNG")
            Image.new("RGB", (1800, 1200), "white").save(landscape, format="PNG")

            portrait_size = _pdf_page_image_size_for_model(portrait, "gpt-image-1")
            landscape_size = _pdf_page_image_size_for_model(landscape, "gpt-image-1")

        self.assertEqual(portrait_size, "1024x1536")
        self.assertEqual(landscape_size, "1536x1024")

    def test_image_connectivity_retries_model_level_errors_three_times(self) -> None:
        class FailingClient:
            def __init__(self) -> None:
                self.calls = 0

            def generate_page(self, **_kwargs):
                self.calls += 1
                raise ImageModelUnavailableError("invalid api key")

        settings = AppSettings()
        settings.image_model_role.source_role = SOURCE_INDEPENDENT
        settings.image_model_role.cloud_provider = "custom_openai"
        settings.image_model_role.cloud_model = "gpt-image-2"
        settings.image_model_role.cloud_base_url = "https://images.example/v1"
        client = FailingClient()

        with patch("core.model_roles.get_key", return_value="secret"):
            result = check_image_generation_connectivity(
                settings,
                client=client,
                max_attempts=3,
            )

        self.assertFalse(result.ok)
        self.assertEqual(client.calls, 3)
        self.assertEqual(settings.image_model_role.availability_status, "unavailable")


if __name__ == "__main__":
    unittest.main(verbosity=2)
