"""Shared helpers for translation result handling."""

REPLACE_TRANSLATION_PREFIX = "__XL_REPLACE__::"


def is_replace_translation(text: str) -> bool:
    return isinstance(text, str) and text.startswith(REPLACE_TRANSLATION_PREFIX)


def extract_replace_translation(text: str) -> str:
    if not is_replace_translation(text):
        return text
    return text[len(REPLACE_TRANSLATION_PREFIX):]


def should_store_translation_in_tm(original: str, translated: str) -> bool:
    if not translated:
        return False
    if translated == original:
        return False
    if is_replace_translation(translated):
        return False
    return True


def should_apply_quality_filter(translated: str) -> bool:
    if not translated:
        return False
    if is_replace_translation(translated):
        return False
    return True
