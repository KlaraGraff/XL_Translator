"""Audit 高-10: correct translations into custom Han-script languages must not
be failed as residual Chinese and reset to the source text."""

from core import residual_classifier
from core.engine_dispatcher import _apply_quality_filter
from core.language_registry import CUSTOM_TARGET_LANG_PREFIX, build_custom_target_lang_code
from core.residual_pipeline import run_residual_pass
from core.tm_hygiene import sanitize_tm_pairs
from core.translation_filter import is_translation_redundant, validate_translation

SOURCE = "本工程于2026年8月9日完工，工期为18个月，总金额500万元。"
TRADITIONAL = "本工程於2026年8月9日完工，工期為18個月，總金額500萬元。"
# 真实繁中合同译文：条款名与标准号常带英文，汉字仍占多数。
TRADITIONAL_WITH_ENGLISH_TERMS = (
    "本工程於2026年8月9日完工（Practical Completion），工期為18個月。"
)


def test_custom_prefix_matches_the_language_registry():
    assert residual_classifier._CUSTOM_TARGET_LANG_PREFIX == CUSTOM_TARGET_LANG_PREFIX


def test_custom_han_script_translation_is_kept():
    for name in ("繁體中文", "粵語"):
        code = build_custom_target_lang_code(name)
        result = validate_translation(SOURCE, TRADITIONAL, target_lang=code)
        assert "residual_cn_date_unit" not in [issue.code for issue in result.issues], name
        assert not is_translation_redundant(SOURCE, TRADITIONAL, target_lang=code), name
        assert run_residual_pass([(SOURCE, TRADITIONAL)], target_lang=code).needs_review == [], name
        hygiene = sanitize_tm_pairs([(SOURCE, TRADITIONAL)], target_lang=code)
        assert dict(hygiene.pairs).get(SOURCE) == TRADITIONAL, name


def test_custom_latin_script_translation_still_catches_leftover_chinese_dates():
    code = build_custom_target_lang_code("法语（瑞士）")
    leftover = "Les travaux ont été achevés le 2026年8月9日, durée 18 mois."
    result = validate_translation(SOURCE, leftover, target_lang=code)
    assert "residual_cn_date_unit" in [issue.code for issue in result.issues]


def test_han_translation_with_english_contract_terms_is_kept():
    """繁中/粤语译文夹带英文条款名、标准号时仍是汉字书写，不能判成残留中文。

    按字符数一比一比较是旧逻辑的漏洞：汉字 14 个对拉丁字母 19 个，译文被判成
    「拉丁语里夹了中文」，「2026年8月9日」随即报 residual_cn_date_unit，
    ``_apply_quality_filter`` 把整句重置回中文原文（审计 高-10 的原始形态）。
    """
    source = "本工程于2026年8月9日完工，工期为18个月。"
    for name in ("繁體中文", "粵語"):
        code = build_custom_target_lang_code(name)
        result = validate_translation(source, TRADITIONAL_WITH_ENGLISH_TERMS, target_lang=code)
        codes = [issue.code for issue in result.issues]
        assert codes == [], (name, codes)
        assert run_residual_pass(
            [(source, TRADITIONAL_WITH_ENGLISH_TERMS)], target_lang=code
        ).needs_review == [], name
        hygiene = sanitize_tm_pairs([(source, TRADITIONAL_WITH_ENGLISH_TERMS)], target_lang=code)
        assert dict(hygiene.pairs).get(source) == TRADITIONAL_WITH_ENGLISH_TERMS, name

        results = {source: TRADITIONAL_WITH_ENGLISH_TERMS}
        _apply_quality_filter(results, code)
        assert results[source] == TRADITIONAL_WITH_ENGLISH_TERMS, (
            "质量校验把已经翻好的繁中译文重置回中文原文：" + name
        )


def test_latin_script_custom_translation_with_chinese_leftovers_is_still_flagged():
    """拉丁语正文里夹汉字短语仍然算残留，不能因为放宽汉字书写就不查。"""
    code = build_custom_target_lang_code("法语（瑞士）")
    source = "承包商应在开工前提交施工方案及安全措施。"
    leftover = "The Contractor shall provide 施工方案及安全措施 before commencement of the Works."
    assert not residual_classifier.is_residual_exempt_target(code, leftover)
    residual = run_residual_pass([(source, leftover)], target_lang=code)
    assert residual.needs_review, "拉丁语译文里的整段汉字短语被放行了"
    hygiene = sanitize_tm_pairs([(source, leftover)], target_lang=code)
    assert dict(hygiene.pairs) == {}, "带残留中文的配对不该写进记忆库"


def test_a_short_latin_sentence_with_a_chinese_phrase_is_still_flagged():
    """短句里汉字数接近字母数的三分之一，也不能因为按汉字密度折算就被放过。"""
    code = build_custom_target_lang_code("法语（瑞士）")
    leftover = "The Contractor shall submit 施工方案及安全措施."
    assert not residual_classifier.is_residual_exempt_target(code, leftover)


def test_chinese_translation_quoting_a_contract_term_is_exempt():
    code = build_custom_target_lang_code("繁體中文")
    assert residual_classifier.is_residual_exempt_target(
        code, "工期為 18 個月，以 Notice to Proceed 為準"
    )


def test_regional_chinese_and_japanese_codes_are_exempt():
    assert residual_classifier.is_residual_exempt_target("zh-TW")
    assert residual_classifier.is_residual_exempt_target("ja-JP")
    assert not residual_classifier.is_residual_exempt_target("fr")
