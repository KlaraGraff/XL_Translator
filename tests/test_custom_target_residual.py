"""Audit 高-10: correct translations into custom Han-script languages must not
be failed as residual Chinese and reset to the source text."""

from core import residual_classifier
from core.language_registry import CUSTOM_TARGET_LANG_PREFIX, build_custom_target_lang_code
from core.residual_pipeline import run_residual_pass
from core.tm_hygiene import sanitize_tm_pairs
from core.translation_filter import is_translation_redundant, validate_translation

SOURCE = "本工程于2026年8月9日完工，工期为18个月，总金额500万元。"
TRADITIONAL = "本工程於2026年8月9日完工，工期為18個月，總金額500萬元。"


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


def test_regional_chinese_and_japanese_codes_are_exempt():
    assert residual_classifier.is_residual_exempt_target("zh-TW")
    assert residual_classifier.is_residual_exempt_target("ja-JP")
    assert not residual_classifier.is_residual_exempt_target("fr")
