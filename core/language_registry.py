"""Helpers for target-language registration, custom-language support and ordering."""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Iterable, Mapping

from pydantic import BaseModel, Field

from config import OPTIONAL_TARGET_LANGS, SUPPORTED_LANGS, SUPPORTED_SOURCE_LANGS


class CustomTargetLang(BaseModel):
    name: str = Field(default="")
    description: str = Field(default="")


CUSTOM_TARGET_LANG_PREFIX = "x-custom-"
CUSTOM_TARGET_LANG_ACTION = "__custom_target_lang_action__"
CUSTOM_TARGET_LANG_ACTION_LABEL = "＋ 自定义语言"
CUSTOM_TARGET_LANG_MAX_LENGTH = 32

_WHITESPACE_RE = re.compile(r"\s+")
_INLINE_WHITESPACE_RE = re.compile(r"[^\S\n]+")
_PAREN_WRAPPER_RE = re.compile(r"^(?P<outer>.+?)\s*[（(](?P<inner>.+?)[）)]$")

EXPLICIT_LANGUAGE_ALIAS_GROUPS: dict[str, set[str]] = {
    "中文": {"中文", "汉语"},
    "英文": {"英文", "英语"},
    "法文": {"法文", "法语"},
    "德文": {"德文", "德语"},
    "柬埔寨语（高棉语）": {"柬埔寨语（高棉语）", "柬埔寨语", "高棉语"},
    "菲律宾语（他加禄语）": {"菲律宾语（他加禄语）", "菲律宾语", "他加禄语"},
    "印度尼西亚语": {"印度尼西亚语", "印尼语"},
    "缅甸语": {"缅甸语", "缅语"},
}


def _extract_custom_target_lang_name(value: object) -> str:
    if isinstance(value, CustomTargetLang):
        return value.name
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        for key in ("name", "display", "label"):
            candidate = value.get(key)
            if candidate is not None:
                return str(candidate)
    if hasattr(value, "name"):
        candidate = getattr(value, "name")
        if candidate is not None:
            return str(candidate)
    return ""


def _extract_custom_target_lang_description(value: object) -> str:
    if isinstance(value, CustomTargetLang):
        return value.description
    if isinstance(value, Mapping):
        for key in ("description", "desc", "note"):
            candidate = value.get(key)
            if candidate is not None:
                return str(candidate)
    if hasattr(value, "description"):
        candidate = getattr(value, "description")
        if candidate is not None:
            return str(candidate)
    return ""


def normalize_custom_target_lang_display(display_name: str) -> str:
    return _WHITESPACE_RE.sub(" ", str(display_name or "")).strip()


def normalize_custom_target_lang_description(description: object) -> str:
    raw_text = str(description or "").replace("\r\n", "\n").replace("\r", "\n")
    normalized_lines = [
        _INLINE_WHITESPACE_RE.sub(" ", line).strip()
        for line in raw_text.split("\n")
    ]
    return "\n".join(line for line in normalized_lines if line).strip()


def _iter_base_language_name_aliases(display_name: str) -> set[str]:
    normalized = normalize_custom_target_lang_display(display_name)
    if not normalized:
        return set()

    aliases = {normalized}
    match = _PAREN_WRAPPER_RE.match(normalized)
    if match:
        outer = normalize_custom_target_lang_display(match.group("outer"))
        inner = normalize_custom_target_lang_display(match.group("inner"))
        if outer:
            aliases.add(outer)
        if inner:
            aliases.add(inner)

    expanded_aliases = set(aliases)
    for alias in list(aliases):
        if alias.endswith("文") and len(alias) > 1:
            expanded_aliases.add(f"{alias[:-1]}语")
        if alias.endswith("语") and len(alias) > 1:
            expanded_aliases.add(f"{alias[:-1]}文")

    return expanded_aliases


def _build_explicit_language_alias_maps() -> tuple[dict[str, str], dict[str, set[str]]]:
    alias_to_canonical: dict[str, str] = {}
    canonical_to_aliases: dict[str, set[str]] = {}
    for canonical_name, alias_group in EXPLICIT_LANGUAGE_ALIAS_GROUPS.items():
        expanded_aliases: set[str] = set()
        for alias in {canonical_name, *alias_group}:
            expanded_aliases.update(_iter_base_language_name_aliases(alias))
        canonical_to_aliases[canonical_name] = expanded_aliases
        for alias in expanded_aliases:
            alias_to_canonical[alias.casefold()] = canonical_name
    return alias_to_canonical, canonical_to_aliases


_EXPLICIT_LANGUAGE_ALIAS_TO_CANONICAL, _EXPLICIT_CANONICAL_TO_ALIASES = (
    _build_explicit_language_alias_maps()
)


def _iter_language_name_aliases(display_name: str) -> set[str]:
    normalized = normalize_custom_target_lang_display(display_name)
    if not normalized:
        return set()

    aliases = set(_iter_base_language_name_aliases(normalized))
    explicit_canonical = _EXPLICIT_LANGUAGE_ALIAS_TO_CANONICAL.get(normalized.casefold())
    if explicit_canonical:
        aliases.update(_EXPLICIT_CANONICAL_TO_ALIASES.get(explicit_canonical, set()))
        aliases.add(explicit_canonical)
    return aliases


def _build_builtin_language_alias_to_canonical() -> dict[str, str]:
    alias_to_canonical: dict[str, str] = {}
    for display_name in SUPPORTED_LANGS:
        for alias in _iter_language_name_aliases(display_name):
            alias_to_canonical[alias.casefold()] = display_name
    return alias_to_canonical


_BUILTIN_LANGUAGE_ALIAS_TO_CANONICAL = _build_builtin_language_alias_to_canonical()
_RESERVED_LANGUAGE_ALIAS_TO_CANONICAL = {
    alias_key: canonical_name
    for alias_key, canonical_name in _EXPLICIT_LANGUAGE_ALIAS_TO_CANONICAL.items()
    if canonical_name not in SUPPORTED_LANGS
}
_SYSTEM_LANGUAGE_ALIAS_KEYS = (
    set(_BUILTIN_LANGUAGE_ALIAS_TO_CANONICAL)
    | set(_RESERVED_LANGUAGE_ALIAS_TO_CANONICAL)
)


def _get_builtin_supported_target_languages(
    *,
    include_optional: bool = False,
) -> dict[str, str]:
    supported = dict(SUPPORTED_LANGS)
    if include_optional:
        supported.update(OPTIONAL_TARGET_LANGS)
    return supported


def _split_lang_pair(lang_pair: str) -> tuple[str, str] | None:
    if not isinstance(lang_pair, str):
        return None
    source_lang, separator, target_lang = lang_pair.partition("-")
    if not separator or not source_lang or not target_lang:
        return None
    return source_lang, target_lang


def _find_builtin_or_reserved_language_match(display_name: str) -> tuple[str, str] | None:
    normalized = normalize_custom_target_lang_display(display_name)
    if not normalized:
        return None

    alias_key = normalized.casefold()
    builtin_match = _BUILTIN_LANGUAGE_ALIAS_TO_CANONICAL.get(alias_key)
    if builtin_match:
        return "builtin", builtin_match

    reserved_match = _RESERVED_LANGUAGE_ALIAS_TO_CANONICAL.get(alias_key)
    if reserved_match:
        return "reserved", reserved_match

    return None


def _find_custom_target_lang_match(
    display_name: str,
    custom_target_langs: Iterable[object] | None,
) -> str | None:
    alias_keys = {alias.casefold() for alias in _iter_language_name_aliases(display_name)}
    if not alias_keys:
        return None

    for existing_entry in normalize_custom_target_langs(custom_target_langs):
        existing_alias_keys = {
            alias.casefold()
            for alias in _iter_language_name_aliases(existing_entry.name)
        }
        if alias_keys & existing_alias_keys:
            return existing_entry.name
    return None


def _resolve_custom_target_lang_name(target_lang_or_display: str) -> str:
    decoded_name = decode_custom_target_lang_code(target_lang_or_display)
    if decoded_name:
        return decoded_name
    return normalize_custom_target_lang_display(target_lang_or_display)


def _find_custom_target_lang_entry(
    custom_target_langs: Iterable[object] | None,
    target_lang_or_display: str,
) -> CustomTargetLang | None:
    target_name = _resolve_custom_target_lang_name(target_lang_or_display)
    if not target_name:
        return None

    target_key = target_name.casefold()
    for entry in normalize_custom_target_langs(custom_target_langs):
        if entry.name.casefold() == target_key:
            return entry
    return None


def build_custom_target_lang_code(display_name: str) -> str:
    normalized = normalize_custom_target_lang_display(display_name)
    if not normalized:
        raise ValueError("自定义语言名称不能为空")
    encoded = base64.urlsafe_b64encode(normalized.encode("utf-8")).decode("ascii").rstrip("=")
    return f"{CUSTOM_TARGET_LANG_PREFIX}{encoded}"


def decode_custom_target_lang_code(target_lang: str) -> str | None:
    if not isinstance(target_lang, str) or not target_lang.startswith(CUSTOM_TARGET_LANG_PREFIX):
        return None

    encoded = target_lang[len(CUSTOM_TARGET_LANG_PREFIX) :]
    if not encoded:
        return None

    padding = "=" * (-len(encoded) % 4)
    try:
        decoded = base64.urlsafe_b64decode(encoded + padding).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None

    normalized = normalize_custom_target_lang_display(decoded)
    return normalized or None


def normalize_custom_target_langs(
    custom_target_langs: Iterable[object] | None,
) -> list[CustomTargetLang]:
    normalized: list[CustomTargetLang] = []
    seen_alias_keys: set[str] = set()
    alias_key_to_index: dict[str, int] = {}

    for item in custom_target_langs or []:
        display_name = normalize_custom_target_lang_display(
            _extract_custom_target_lang_name(item)
        )
        description = normalize_custom_target_lang_description(
            _extract_custom_target_lang_description(item)
        )

        if not display_name:
            continue
        if len(display_name) > CUSTOM_TARGET_LANG_MAX_LENGTH:
            continue

        alias_keys = {alias.casefold() for alias in _iter_language_name_aliases(display_name)}
        if not alias_keys:
            continue
        if alias_keys & _SYSTEM_LANGUAGE_ALIAS_KEYS:
            continue

        if alias_keys & seen_alias_keys:
            existing_indexes = {
                alias_key_to_index[alias_key]
                for alias_key in alias_keys
                if alias_key in alias_key_to_index
            }
            if description and existing_indexes:
                existing_index = min(existing_indexes)
                existing_entry = normalized[existing_index]
                if not existing_entry.description:
                    normalized[existing_index] = existing_entry.model_copy(
                        update={"description": description}
                    )
            continue

        entry = CustomTargetLang(name=display_name, description=description)
        normalized.append(entry)
        entry_index = len(normalized) - 1
        seen_alias_keys.update(alias_keys)
        for alias_key in alias_keys:
            alias_key_to_index[alias_key] = entry_index

    return normalized


def get_custom_target_lang_display_error(
    display_name: str,
    existing_custom_target_langs: Iterable[object] | None = None,
) -> str | None:
    raw_display = str(display_name or "")
    normalized = normalize_custom_target_lang_display(raw_display)

    if not normalized:
        return "请输入自定义语言名称。"
    if len(normalized) > CUSTOM_TARGET_LANG_MAX_LENGTH:
        return f"自定义语言名称不能超过 {CUSTOM_TARGET_LANG_MAX_LENGTH} 个字符。"
    if normalized == CUSTOM_TARGET_LANG_ACTION_LABEL:
        return "该名称与内置操作项冲突，请换一个名称。"

    builtin_or_reserved_match = _find_builtin_or_reserved_language_match(normalized)
    if builtin_or_reserved_match is not None:
        match_type, canonical_name = builtin_or_reserved_match
        if match_type == "builtin":
            return (
                f"该语言已包含在内置列表中：{canonical_name}。"
                f"你输入的是：{normalized}。请直接从下拉框选择。"
            )
        return (
            f"系统已识别该语种为：{canonical_name}。"
            f"你输入的是：{normalized}。该名称不需要再作为自定义语言添加。"
        )

    existing_custom_match = _find_custom_target_lang_match(
        normalized,
        existing_custom_target_langs,
    )
    if existing_custom_match is not None:
        return (
            f"该自定义语言已存在：{existing_custom_match}。"
            f"你输入的是：{normalized}。请勿重复添加。"
        )

    return None


def append_custom_target_lang(
    custom_target_langs: Iterable[object] | None,
    display_name: str,
    description: str = "",
) -> tuple[list[CustomTargetLang], str]:
    error = get_custom_target_lang_display_error(display_name, custom_target_langs)
    if error is not None:
        raise ValueError(error)

    normalized_langs = normalize_custom_target_langs(custom_target_langs)
    normalized_display = normalize_custom_target_lang_display(display_name)
    normalized_description = normalize_custom_target_lang_description(description)
    normalized_langs.append(
        CustomTargetLang(
            name=normalized_display,
            description=normalized_description,
        )
    )
    return normalized_langs, build_custom_target_lang_code(normalized_display)


def update_custom_target_lang_description(
    custom_target_langs: Iterable[object] | None,
    target_lang_or_display: str,
    description: str,
) -> list[CustomTargetLang]:
    normalized_langs = normalize_custom_target_langs(custom_target_langs)
    target_name = _resolve_custom_target_lang_name(target_lang_or_display)
    if not target_name:
        return normalized_langs

    target_key = target_name.casefold()
    normalized_description = normalize_custom_target_lang_description(description)
    updated_langs: list[CustomTargetLang] = []
    for entry in normalized_langs:
        if entry.name.casefold() == target_key:
            updated_langs.append(
                entry.model_copy(update={"description": normalized_description})
            )
        else:
            updated_langs.append(entry)
    return updated_langs


def remove_custom_target_lang(
    custom_target_langs: Iterable[object] | None,
    target_lang_or_display: str,
) -> list[CustomTargetLang]:
    normalized_langs = normalize_custom_target_langs(custom_target_langs)
    display_name = _resolve_custom_target_lang_name(target_lang_or_display)
    if not display_name:
        return normalized_langs

    display_key = display_name.casefold()
    return [entry for entry in normalized_langs if entry.name.casefold() != display_key]


def get_saved_custom_target_lang_entries(
    custom_target_langs: Iterable[object] | None = None,
) -> list[tuple[str, str, str]]:
    return [
        (
            build_custom_target_lang_code(entry.name),
            entry.name,
            entry.description,
        )
        for entry in normalize_custom_target_langs(custom_target_langs)
    ]


def get_supported_languages(
    custom_target_langs: Iterable[object] | None = None,
    *,
    include_optional: bool = False,
) -> dict[str, str]:
    supported = _get_builtin_supported_target_languages(include_optional=include_optional)
    for code, display_name, _ in get_saved_custom_target_lang_entries(custom_target_langs):
        supported[display_name] = code
    return supported


def get_default_target_lang() -> str:
    return "en"


def get_default_source_lang() -> str:
    return "zh"


def get_supported_source_languages() -> dict[str, str]:
    return dict(SUPPORTED_SOURCE_LANGS)


def get_source_lang_codes() -> list[str]:
    return list(get_supported_source_languages().values())


def is_supported_source_lang(source_lang: str) -> bool:
    return source_lang in set(get_source_lang_codes())


def get_source_lang_display(source_lang: str) -> str:
    for display_name, lang_code in get_supported_source_languages().items():
        if lang_code == source_lang:
            return display_name
    return source_lang


def get_builtin_target_lang_codes(*, include_optional: bool = False) -> list[str]:
    return list(_get_builtin_supported_target_languages(include_optional=include_optional).values())


def get_custom_target_lang_codes(custom_target_langs: Iterable[object] | None = None) -> list[str]:
    return [code for code, _, _ in get_saved_custom_target_lang_entries(custom_target_langs)]


def get_target_lang_codes(
    custom_target_langs: Iterable[object] | None = None,
    *,
    include_optional: bool = False,
) -> list[str]:
    return get_builtin_target_lang_codes(include_optional=include_optional) + get_custom_target_lang_codes(custom_target_langs)


def is_custom_target_lang(target_lang: str) -> bool:
    return decode_custom_target_lang_code(target_lang) is not None


def is_supported_target_lang(
    target_lang: str,
    custom_target_langs: Iterable[object] | None = None,
    *,
    include_optional: bool = False,
) -> bool:
    return target_lang in set(get_target_lang_codes(custom_target_langs, include_optional=include_optional))


def get_target_lang_display(
    target_lang: str,
    custom_target_langs: Iterable[object] | None = None,
    *,
    include_optional: bool = False,
) -> str:
    for display_name, lang_code in get_supported_languages(
        custom_target_langs,
        include_optional=include_optional,
    ).items():
        if lang_code == target_lang:
            return display_name

    decoded_custom_name = decode_custom_target_lang_code(target_lang)
    if decoded_custom_name:
        return decoded_custom_name
    return target_lang


def get_target_lang_description(
    target_lang: str,
    custom_target_langs: Iterable[object] | None = None,
) -> str:
    entry = _find_custom_target_lang_entry(custom_target_langs, target_lang)
    if entry is None:
        return ""
    return entry.description


def get_target_lang_display_from_lang_pair(
    lang_pair: str,
    custom_target_langs: Iterable[object] | None = None,
) -> str:
    split_pair = _split_lang_pair(lang_pair)
    if split_pair is None:
        return "目标语言"
    _source_lang, target_lang = split_pair
    return get_target_lang_display(
        target_lang,
        custom_target_langs,
        include_optional=True,
    )


def get_target_lang_description_from_lang_pair(
    lang_pair: str,
    custom_target_langs: Iterable[object] | None = None,
) -> str:
    split_pair = _split_lang_pair(lang_pair)
    if split_pair is None:
        return ""
    _source_lang, target_lang = split_pair
    return get_target_lang_description(target_lang, custom_target_langs)


def build_target_lang_note_block(
    target_lang: str,
    custom_target_langs: Iterable[object] | None = None,
) -> str:
    target_lang_name = get_target_lang_display(target_lang, custom_target_langs)
    target_lang_description = get_target_lang_description(target_lang, custom_target_langs)
    if not target_lang_description:
        return ""

    return (
        "[目标语言识别补充]\n"
        f"目标语言名称：{target_lang_name}\n"
        "语言说明：\n"
        f"{target_lang_description}\n"
        "以上说明仅用于帮助你准确识别目标语言，不改变既有输出格式、领域要求和术语规则。"
    )


def build_target_lang_note_block_from_lang_pair(
    lang_pair: str,
    custom_target_langs: Iterable[object] | None = None,
) -> str:
    split_pair = _split_lang_pair(lang_pair)
    if split_pair is None:
        return ""
    _source_lang, target_lang = split_pair
    return build_target_lang_note_block(target_lang, custom_target_langs)


def append_prompt_block(prompt: str, extra_block: str) -> str:
    prompt_text = str(prompt or "").strip()
    extra_text = str(extra_block or "").strip()
    if prompt_text and extra_text:
        return f"{prompt_text}\n\n{extra_text}"
    return prompt_text or extra_text


def normalize_recent_target_langs(
    target_langs: Iterable[str] | None,
    custom_target_langs: Iterable[object] | None = None,
    *,
    include_optional: bool = False,
) -> list[str]:
    supported_codes = set(
        get_target_lang_codes(
            custom_target_langs,
            include_optional=include_optional,
        )
    )
    normalized: list[str] = []
    seen: set[str] = set()

    for target_lang in target_langs or []:
        if target_lang not in supported_codes or target_lang in seen:
            continue
        normalized.append(target_lang)
        seen.add(target_lang)

    return normalized


def remember_recent_target_lang(
    recent_target_langs: Iterable[str] | None,
    selected_target_lang: str,
    custom_target_langs: Iterable[object] | None = None,
    *,
    include_optional: bool = False,
) -> list[str]:
    if not is_supported_target_lang(
        selected_target_lang,
        custom_target_langs,
        include_optional=include_optional,
    ):
        return normalize_recent_target_langs(
            recent_target_langs,
            custom_target_langs,
            include_optional=include_optional,
        )

    ordered = [selected_target_lang]
    for target_lang in normalize_recent_target_langs(
        recent_target_langs,
        custom_target_langs,
        include_optional=include_optional,
    ):
        if target_lang != selected_target_lang:
            ordered.append(target_lang)
    return ordered


def remove_recent_target_lang(
    recent_target_langs: Iterable[str] | None,
    target_lang: str,
    custom_target_langs: Iterable[object] | None = None,
    *,
    include_optional: bool = False,
) -> list[str]:
    return [
        code
        for code in normalize_recent_target_langs(
            recent_target_langs,
            custom_target_langs,
            include_optional=include_optional,
        )
        if code != target_lang
    ]


def get_ordered_target_lang_codes(
    recent_target_langs: Iterable[str] | None = None,
    custom_target_langs: Iterable[object] | None = None,
    *,
    include_optional: bool = False,
) -> list[str]:
    supported_codes = get_target_lang_codes(
        custom_target_langs,
        include_optional=include_optional,
    )
    if not supported_codes:
        return []

    ordered_codes = normalize_recent_target_langs(
        recent_target_langs,
        custom_target_langs,
        include_optional=include_optional,
    )
    if not ordered_codes:
        ordered_codes = [get_default_target_lang()]

    remaining_codes = [
        target_lang
        for target_lang in supported_codes
        if target_lang not in ordered_codes
    ]
    return ordered_codes + remaining_codes


def get_first_available_target_lang(
    recent_target_langs: Iterable[str] | None = None,
    custom_target_langs: Iterable[object] | None = None,
    *,
    include_optional: bool = False,
) -> str:
    ordered_codes = get_ordered_target_lang_codes(
        recent_target_langs,
        custom_target_langs,
        include_optional=include_optional,
    )
    if ordered_codes:
        return ordered_codes[0]
    return get_default_target_lang()


def build_lang_pair(target_lang: str, source_lang: str = "zh") -> str:
    normalized_source_lang = str(source_lang or "zh").strip() or "zh"
    normalized_target_lang = str(target_lang or "").strip()
    return f"{normalized_source_lang}-{normalized_target_lang}"
