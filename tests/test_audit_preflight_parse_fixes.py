"""Regression tests for docs/CODE_AUDIT_2026-08-29.md cluster A3-preflight-parse.

高-3  8 种文字系统不在语言预检候选正则内，自动检测下整份文件原样输出。
中-10 模型返回对象数组时 str() 兜底，把 {'translation': 'Valve'} 这类字典
      字面量写进单元格并存入 TM（engines/base_engine.py 与
      core/language_preflight.py 两处同源逻辑）。
低-引擎 数组 null 元素转空串后不写入也不计未译，报告显示全部成功。
"""

from __future__ import annotations

import unittest

from core.engine_dispatcher import TranslationBatchRunStats, _translate_batch_with_fallback
from core.language_preflight import (
    extract_preflight_candidates,
    normalize_translation_language_result,
    preflight_file_source_languages,
)
from engines.base_engine import parse_response
from engines.base_engine import TranslationEngine


# ---------------------------------------------------------------------------
# 高-3：8 种文字系统必须能产出预检候选，从而触发一次预检请求。
# ---------------------------------------------------------------------------

_SCRIPT_SAMPLES = {
    "阿拉伯": "مرحبا بالعالم هذا نص تجريبي طويل بما يكفي",
    "希伯来": "שלום עולם זהו טקסט לדוגמה ארוך דיו",
    "泰": "สวัสดีชาวโลกนี่คือข้อความตัวอย่างที่ยาวพอ",
    "老挝": "ສະບາຍດີຊາວໂລກນີ້ແມ່ນຂໍ້ຄວາມຕົວຢ່າງ",
    "缅甸": "မင်္ဂလာပါကမ္ဘာဤသည်နမူနာစာသားရှည်လျားသည်",
    "高棉": "សួស្តីពិភពលោកនេះជាអត្ថបទគំរូវែងគ្រប់គ្រាន់",
    "埃塞俄比亚": "ሰላም ለዓለም ይህ በቂ ርዝመት ያለው የናሙና ጽሑፍ ነው",
    "谚文(韩语)": "안녕하세요 세계 이것은 충분히 긴 샘플 텍스트입니다",
}


class LanguagePreflightScriptCoverageTests(unittest.TestCase):
    def test_all_eight_scripts_produce_a_preflight_candidate(self) -> None:
        for label, sample in _SCRIPT_SAMPLES.items():
            with self.subTest(script=label):
                candidates = extract_preflight_candidates([sample, "=SUM(A1:A2)", "12345"])
                self.assertEqual(
                    candidates,
                    [sample],
                    f"{label} 文字样本未被识别为预检候选，回归到高-3 的行为",
                )

    def test_pure_arabic_file_triggers_one_detector_request_not_a_silent_skip(self) -> None:
        # 修复前：候选为空 -> requested=False -> 自动检测下预检零调用，整份
        # 文件回落默认 zh，补译模式下全部单元格判 ignored，原样输出。
        #
        # 审查 mustFix（高-3）：原用例混入了"工程合同"这样的中文候选，旧
        # 正则本就含 CJK 区间，靠它就能让 requested=True，测试等于空转。
        # 这里只留阿拉伯样本 + 必被过滤项（公式/纯数字/编号），并额外断言
        # 发给模型的 payload 与 result.candidates 里确实是阿拉伯文本本身，
        # 而不是碰巧被其他分支带出的 True。旧正则（不含阿拉伯区块）下
        # 重跑本用例必须转红。
        calls: list[str] = []

        def request(_system: str, user: str) -> str:
            calls.append(user)
            return '{"source_langs":["ar"]}'

        arabic_sample = _SCRIPT_SAMPLES["阿拉伯"]
        result = preflight_file_source_languages(
            [arabic_sample, "=A1+A2", "12345", "1.", "100%"],
            target_lang="zh",
            request=request,
        )
        self.assertTrue(result.requested, "阿拉伯语候选应当触发预检请求")
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.source_langs, ("ar",))
        # 端到端断言：候选集合与发给模型的 payload 里必须真的包含阿拉伯文
        # 本身，证明 requested=True 是阿拉伯样本产出候选带来的，而不是
        # 测试输入里混进了其他文字系统的巧合。
        self.assertEqual(result.candidates, (arabic_sample,))
        self.assertIn(arabic_sample, calls[0])

    def test_non_script_filters_still_excluded(self) -> None:
        # 回归保护：数字/公式/编号仍然不应被误判为候选。
        candidates = extract_preflight_candidates(["=SUM(A1:A2)", "2026-07-24", "1.", "100%"])
        self.assertEqual(candidates, [])


# ---------------------------------------------------------------------------
# 中-10 + 低-引擎：engines/base_engine.py::parse_response
# ---------------------------------------------------------------------------


class ParseResponseObjectAndNullTests(unittest.TestCase):
    def test_plain_string_array_still_works(self) -> None:
        result = parse_response(["Valve", "Pump"], '["阀门", "泵"]')
        self.assertEqual(result, {"Valve": "阀门", "Pump": "泵"})

    def test_dict_item_extracts_translation_field_instead_of_str_dumping(self) -> None:
        raw = '[{"translation": "阀门"}, "泵"]'
        result = parse_response(["Valve", "Pump"], raw)
        # 修复前：result["Valve"] == "{'translation': 'Valve'}"（str() 兜底）。
        self.assertEqual(result["Valve"], "阀门")
        self.assertNotIn("translation", result["Valve"])
        self.assertNotIn("{", result["Valve"])
        self.assertEqual(result["Pump"], "泵")

    def test_dict_item_without_translation_field_raises_instead_of_polluting(self) -> None:
        raw = '[{"note": "no translation key here"}]'
        with self.assertRaises(ValueError):
            parse_response(["Valve"], raw)

    def test_null_item_raises_instead_of_silently_becoming_empty_string(self) -> None:
        # 修复前：null -> ""，既不写入译文也不计入未译，报告显示全部成功。
        with self.assertRaises(ValueError):
            parse_response(["Valve", "Pump"], '["阀门", null]')

    def test_null_item_error_message_identifies_the_offending_index(self) -> None:
        with self.assertRaisesRegex(ValueError, "第 2 项"):
            parse_response(["Valve", "Pump"], '["阀门", null]')


class ParseResponseNullEndToEndCountsAsUntranslatedTests(unittest.TestCase):
    """确认 parse_response 抛出的异常真的接到了既有的批次降级/统计链路上，
    而不是把 null 静默吞成不痛不痒的空字符串。"""

    class _NullReturningEngine(TranslationEngine):
        def __init__(self, raw: str) -> None:
            self._raw = raw

        def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
            return parse_response(texts, self._raw, "Test")

    def test_single_null_item_ends_up_recorded_as_untranslated_with_original_kept(self) -> None:
        engine = self._NullReturningEngine('[null]')
        stats = TranslationBatchRunStats()
        result = _translate_batch_with_fallback(
            ["阀门"],
            engine=engine,
            target_lang="en",
            system_prompt="",
            source_lang="zh",
            api_scheduler=None,
            request_category="test",
            should_stop=None,
            error_callback=None,
            stats=stats,
        )
        # 修复前：parse_response 会把 null 转成 ""，_validate_batch_integrity
        # 只检查 key 是否存在就通过，整批"成功"但产出是空字符串，且
        # untranslated_count 不会增加。
        self.assertEqual(result, {"阀门": "阀门"})
        self.assertEqual(stats.untranslated_count, 1)


# ---------------------------------------------------------------------------
# 中-10 同源逻辑：core/language_preflight.py::normalize_translation_language_result
# ---------------------------------------------------------------------------


class NormalizeTranslationLanguageResultObjectTests(unittest.TestCase):
    def test_plain_string_translation_still_works(self) -> None:
        result = normalize_translation_language_result(
            "Valve",
            {"translation": "阀门", "source_lang": "en"},
            target_lang="zh",
        )
        self.assertEqual(result.translation, "阀门")
        self.assertEqual(result.source_lang, "en")

    def test_nested_dict_translation_field_is_extracted_not_str_dumped(self) -> None:
        # 某些供应商会把 translation 字段再包一层对象；修复前 str() 兜底会把
        # 整个嵌套字典写进产物。
        raw_item = {
            "translation": {"translation": "阀门"},
            "source_lang": "en",
        }
        result = normalize_translation_language_result("Valve", raw_item, target_lang="zh")
        self.assertEqual(result.translation, "阀门")

    def test_missing_translation_field_raises(self) -> None:
        raw_item = {"source_lang": "en"}
        with self.assertRaises(ValueError):
            normalize_translation_language_result("Valve", raw_item, target_lang="zh")

    def test_non_mapping_list_item_raises_instead_of_str_dumping(self) -> None:
        # raw_item 本身不是 Mapping（比如模型返回了裸数组元素），修复前会
        # 走 else 分支把整个列表 str() 进翻译文本。
        with self.assertRaises(ValueError):
            normalize_translation_language_result("Valve", ["阀门"], target_lang="zh")


if __name__ == "__main__":
    unittest.main(verbosity=2)
