import unittest

from core.task_runner import (
    _detect_post_failure_source_language,
    _language_mismatch_message,
)


class _ProbeEngine:
    engine_name = "openai/test"

    def __init__(self, response: str = '{"source_langs":["fr"]}') -> None:
        self.response = response
        self.calls = 0

    def chat(self, _system: str, _user: str) -> str:
        self.calls += 1
        return self.response


class LanguageMismatchIssueTests(unittest.TestCase):
    def test_near_total_reset_probes_once_and_reports_real_mismatch(self):
        engine = _ProbeEngine()

        result = _detect_post_failure_source_language(
            engine=engine,
            samples=["Checklist de sécurité incendie"],
            requested_source="zh",
            target_lang="fr",
            reset_count=138,
            original_count=138,
        )

        self.assertEqual(result, "fr")
        self.assertEqual(engine.calls, 1)
        self.assertEqual(
            _language_mismatch_message(result, "zh", "fr"),
            "语言方向疑似选错：检测到的源文本更像法文，当前选择为“中文 → 法文”。"
            "建议反向选择“法文 → 中文”后重新翻译。",
        )

    def test_small_reset_does_not_run_probe(self):
        engine = _ProbeEngine()
        result = _detect_post_failure_source_language(
            engine=engine,
            samples=["Checklist de sécurité"],
            requested_source="zh",
            target_lang="fr",
            reset_count=2,
            original_count=10,
        )
        self.assertIsNone(result)
        self.assertEqual(engine.calls, 0)

    def test_matching_detected_language_keeps_normal_quality_warning(self):
        engine = _ProbeEngine('{"source_langs":["zh"]}')
        result = _detect_post_failure_source_language(
            engine=engine,
            samples=["施工检查表"],
            requested_source="zh",
            target_lang="fr",
            reset_count=10,
            original_count=10,
        )
        self.assertIsNone(result)
        self.assertEqual(engine.calls, 1)

    def test_uncertain_probe_keeps_normal_quality_warning(self):
        engine = _ProbeEngine("not valid language output")
        result = _detect_post_failure_source_language(
            engine=engine,
            samples=["Checklist text"],
            requested_source="zh",
            target_lang="fr",
            reset_count=10,
            original_count=10,
        )
        self.assertIsNone(result)
        self.assertEqual(engine.calls, 1)
