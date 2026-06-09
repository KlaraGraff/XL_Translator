from __future__ import annotations

import json
import unittest

from core.mixed_language import (
    MIXED_ACTION_EXISTING_BILINGUAL,
    MIXED_ACTION_FOREIGN_NOISE,
    MIXED_ACTION_TRANSLATE,
    classify_mixed_language_source,
    split_mixed_language_sources,
    translate_mixed_language_texts,
)
from engines.base_engine import TranslationEngine


class StructuredMixedEngine(TranslationEngine):
    @property
    def engine_name(self) -> str:
        return "fake/structured-mixed"

    def translate_batch(
        self,
        texts: list[str],
        target_lang: str,
        system_prompt: str,
        source_lang: str = "zh",
    ) -> dict[str, str]:
        return {text: text for text in texts}

    def chat(self, system: str, user: str) -> str:
        payload = json.loads(user)
        results = []
        for item in reversed(payload):
            text = item["text"]
            if text == "内容 Contenu":
                results.append(
                    {
                        "id": item["id"],
                        "action": MIXED_ACTION_EXISTING_BILINGUAL,
                        "translation": "",
                        "note": "already translated",
                    }
                )
            elif text == "沉降观测 asdfg":
                results.append(
                    {
                        "id": item["id"],
                        "action": MIXED_ACTION_FOREIGN_NOISE,
                        "translation": "Observation du tassement",
                        "note": "noise",
                    }
                )
            else:
                results.append(
                    {
                        "id": item["id"],
                        "action": MIXED_ACTION_TRANSLATE,
                        "translation": "Projet BTR",
                        "note": "proper noun",
                    }
                )
        return json.dumps(results, ensure_ascii=False)


class MixedLanguageTests(unittest.TestCase):
    def test_short_label_routes_to_mixed_path(self) -> None:
        decision = classify_mixed_language_source(
            "内容 Contenu",
            target_lang="fr",
            source_lang="zh",
        )

        self.assertTrue(decision.is_mixed)
        self.assertEqual(decision.reason, "short_label")

    def test_long_body_whitelist_allows_units_models_and_abbreviations(self) -> None:
        source = "本工程采用 BTR 工艺，C30 混凝土，DN50 管道，GB/T 标准，厚度 300mm。"

        decision = classify_mixed_language_source(
            source,
            target_lang="fr",
            source_lang="zh",
        )

        self.assertFalse(decision.is_mixed)

    def test_long_body_whitelist_allows_units_attached_to_numbers(self) -> None:
        source = (
            "1.灌木种类：金叶女贞（具体草种由甲方确认）\n"
            "2.灌木规格：高度65-70cm，冠幅35cm（修剪后高度为60cm），9株/平方米\n"
            "3.其他：满足设计图纸、招标文件及相关技术规范要求"
        )

        decision = classify_mixed_language_source(
            source,
            target_lang="fr",
            source_lang="zh",
        )

        self.assertFalse(decision.is_mixed)

    def test_long_body_whitelist_allows_compound_units(self) -> None:
        source = "使用商品有机肥50Kg/亩，草籽按20g/㎡，操作平台荷载不超过1.5kN/m²。"

        decision = classify_mixed_language_source(
            source,
            target_lang="fr",
            source_lang="zh",
        )

        self.assertFalse(decision.is_mixed)

    def test_short_label_keeps_non_unit_abbreviation_on_mixed_path(self) -> None:
        decision = classify_mixed_language_source(
            "不含税单价/HT",
            target_lang="fr",
            source_lang="zh",
        )

        self.assertTrue(decision.is_mixed)
        self.assertEqual(decision.reason, "short_label")

    def test_long_body_residual_foreign_text_routes_to_mixed_path(self) -> None:
        source = "本段说明沉降观测点布置与复测要求 asdfg，其他内容均为中文工程说明。"

        decision = classify_mixed_language_source(
            source,
            target_lang="fr",
            source_lang="zh",
        )

        self.assertTrue(decision.is_mixed)
        self.assertEqual(decision.reason, "long_body")

    def test_target_chinese_disables_mixed_path(self) -> None:
        normal, mixed = split_mixed_language_sources(
            ["内容 Contenu"],
            target_lang="zh",
            source_lang="zh",
        )

        self.assertEqual(normal, ["内容 Contenu"])
        self.assertEqual(mixed, [])

    def test_structured_results_are_matched_by_id_not_order(self) -> None:
        texts = ["内容 Contenu", "沉降观测 asdfg", "贝特瑞 BTR 项目"]

        results = translate_mixed_language_texts(
            texts,
            engine=StructuredMixedEngine(),
            target_lang="fr",
            system_prompt="base",
            source_lang="zh",
            concurrency=1,
        )

        self.assertEqual(results["内容 Contenu"].action, MIXED_ACTION_EXISTING_BILINGUAL)
        self.assertEqual(results["沉降观测 asdfg"].action, MIXED_ACTION_FOREIGN_NOISE)
        self.assertEqual(results["沉降观测 asdfg"].translation, "Observation du tassement")
        self.assertEqual(results["贝特瑞 BTR 项目"].action, MIXED_ACTION_TRANSLATE)
        self.assertEqual(results["贝特瑞 BTR 项目"].translation, "Projet BTR")


if __name__ == "__main__":
    unittest.main(verbosity=2)
