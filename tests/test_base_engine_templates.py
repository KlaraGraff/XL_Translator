"""The shared task-instruction templates must survive str.format.

TASK_INSTRUCTION_WITH_SOURCE embeds a literal JSON example; if its braces are
not doubled, every translate_batch_with_sources call raises KeyError before a
single request is sent and the whole auto-source-language path silently
returns originals.
"""

from __future__ import annotations

import json
import unittest

from engines.base_engine import (
    TASK_INSTRUCTION,
    TASK_INSTRUCTION_WITH_SOURCE,
    TranslationEngine,
)


class _RecordingEngine(TranslationEngine):
    def __init__(self) -> None:
        self.system = ""

    def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
        return {}

    def chat(self, system: str, user: str) -> str:
        self.system = system
        items = json.loads(user)
        return json.dumps(
            [{"translation": f"T:{item}", "source_lang": "en"} for item in items],
            ensure_ascii=False,
        )


class TaskInstructionTemplateTests(unittest.TestCase):
    def test_both_templates_render_with_str_format(self) -> None:
        rendered = TASK_INSTRUCTION.format(
            source_lang_name="中文",
            target_lang_name="英语",
        )
        self.assertIn("英语", rendered)

        rendered_with_source = TASK_INSTRUCTION_WITH_SOURCE.format(
            target_lang_name="英语"
        )
        self.assertIn("英语", rendered_with_source)
        # The doubled braces must come out as a literal JSON example.
        self.assertIn('{"translation":"译文","source_lang":"ISO-639-1 代码"}', rendered_with_source)

    def test_translate_batch_with_sources_round_trip(self) -> None:
        engine = _RecordingEngine()
        results = engine.translate_batch_with_sources(["hello"], "zh", "prompt")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].source_text, "hello")
        self.assertEqual(results[0].translation, "T:hello")
        self.assertEqual(results[0].source_lang, "en")
        self.assertIn('{"translation"', engine.system)


if __name__ == "__main__":
    unittest.main(verbosity=2)
