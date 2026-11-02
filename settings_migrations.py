"""旧版设置文件升级到新版时的迁移规则。settings.load_settings 读到老版本号时调用这里。新增一条迁移写在这里。"""
from __future__ import annotations

from collections.abc import Iterable

from config import LEGACY_DOMAIN_PRESET_NAMES, NO_EXTRA_PROMPT, RESERVED_EXTRA_PROMPT_NAMES


def _extract_settings_version(data: dict) -> int:
    """Read the persisted settings schema version; old files default to 0."""
    raw_version = data.get("settings_version", 0)
    try:
        version = int(raw_version)
    except (TypeError, ValueError):
        return 0
    return max(version, 0)


def _migrate_theme_default(payload: dict, stored_version: int) -> dict:
    """v30: the default theme became "light"; carry old files' default along.

    Up to v29 the default was "system", and nothing in a v29 file tells a
    default apart from a deliberate pick.  Almost every install never touched
    it, so an old "system" is read as the old default and becomes "light".
    Done at read time (not in a model validator) so a user who picks "system"
    again after the upgrade is never re-migrated by a later validation of the
    same in-memory payload.
    """
    if stored_version >= 30:
        return payload
    appearance = payload.get("appearance")
    if not isinstance(appearance, dict) or appearance.get("theme", "system") != "system":
        return payload
    migrated = dict(payload)
    migrated["appearance"] = {**appearance, "theme": "light"}
    return migrated


# v33 之前各页面的默认领域：Excel「无」，其余「同步工程场景」。
_PRE_V33_DEFAULT_DOMAINS = {
    "domain_preset": "同步工程场景",
    "excel_domain_preset": "无",
    "word_domain_preset": "同步工程场景",
    "cad_domain_preset": "同步工程场景",
}


def _migrate_smart_domain_default(payload: dict, stored_version: int) -> dict:
    """v33: every page's default domain became 「自动识别场景」; carry old defaults along.

    Same reasoning as the theme migration above: an old file cannot tell the
    default apart from a deliberate pick, and almost nobody changed it, so a
    page still on its pre-v33 default moves to 「自动识别场景」.  A page set to any
    other domain (or a named prompt) keeps it.
    """
    if stored_version >= 33:
        return payload
    migrated = dict(payload)
    for key, old_default in _PRE_V33_DEFAULT_DOMAINS.items():
        if str(migrated.get(key, old_default) or old_default).strip() == old_default:
            migrated[key] = "自动识别场景"
    return migrated


# 提示词各层所在的字段，按页面分：前缀 "" 是给 CLI 和任务快照用的全局兼容字段。
_PROMPT_LAYER_SURFACES = {
    "": "domain_smart_context",
    "excel_": "excel_smart_context",
    "word_": "word_smart_context",
    "cad_": "cad_smart_context",
}


# V9 旧版单条自定义提示词转成补充要求时用的名字。
LEGACY_CUSTOM_PROMPT_NAME = "旧版自定义提示词"


def _prompt_text(value: object) -> str:
    """One prompt out of a stored value: ``{"_base": ...}``, an old language map, or a string."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        text = str(value.get("_base") or "").strip()
        if text:
            return text
        return next(
            (str(item).strip() for item in value.values() if isinstance(item, str) and item.strip()),
            "",
        )
    return ""


def _unique_extra_name(name: str, taken: set[str]) -> str:
    """A name for a converted prompt that clashes with neither reserved names nor existing ones."""
    base = name.strip() or LEGACY_CUSTOM_PROMPT_NAME
    if base in RESERVED_EXTRA_PROMPT_NAMES:
        base = f"{base}（旧版修改）"
    candidate = base
    index = 2
    while candidate in taken or candidate in RESERVED_EXTRA_PROMPT_NAMES:
        candidate = f"{base}{index}"
        index += 1
    return candidate


def migrate_prompt_layers(payload: dict, prefixes: Iterable[str] | None = None) -> dict:
    """v35: turn the old per-page 「场景预设」 into 内置规则 + 自动识别开关 + 补充要求.

    Up to v34 each page picked one whole prompt: 「无」, one of four built-in
    scenes (any of them editable), a named prompt of the user's, or the V9
    single custom prompt.  From v35 the built-in rules are fixed in code, so:

    * a page on 「自动识别场景」 or one of the three retired scenes keeps
      auto-detect on; 「无」, a named prompt or the V9 custom prompt turns it off,
      which is what those pages were doing before;
    * every edited built-in scene and the V9 custom prompt becomes a named
      补充要求 so nothing the user wrote is lost, and a page that was on an
      edited scene keeps that text selected;
    * the old override / rename / disable fields are emptied.

    Pure: works on a copy of a settings (or export-bundle) dict.  Only the
    page prefixes passed in are touched; a key the payload does not carry reads
    as that version's default.
    """
    migrated = dict(payload)
    if prefixes is None:
        # 页面拆分前的文件只有全局字段：先照 AppSettings 的校验器那样分给各页面，
        # 再逐页转换，否则各页面会按缺省值处理，丢掉用户当时的选择。
        for field in ("domain_preset", "custom_prompt", "domain_name_overrides", "domain_prompt_overrides"):
            if field in migrated:
                for page in ("excel_", "word_", "cad_"):
                    migrated.setdefault(f"{page}{field}", migrated[field])
    for prefix in (prefixes if prefixes is not None else _PROMPT_LAYER_SURFACES):
        smart_key = _PROMPT_LAYER_SURFACES[prefix]
        preset_key = f"{prefix}domain_preset"
        custom_key = f"{prefix}custom_prompt"
        names_key = f"{prefix}domain_name_overrides"
        overrides_key = f"{prefix}domain_prompt_overrides"
        named_key = f"{prefix}domain_custom_prompts"
        disabled_key = f"{prefix}domain_disabled_presets"

        preset = str(migrated.get(preset_key, "自动识别场景") or "").strip() or "自动识别场景"
        if preset == "智能":
            preset = "自动识别场景"
        raw_named = migrated.get(named_key)
        extras: dict[str, dict[str, str]] = {}
        for raw_name, raw_value in (raw_named.items() if isinstance(raw_named, dict) else ()):
            name = str(raw_name or "").strip()
            text = _prompt_text(raw_value)
            if name and text:
                if name in RESERVED_EXTRA_PROMPT_NAMES:
                    name = _unique_extra_name(name, set(extras))
                extras[name] = {"_base": text}
        name_overrides = migrated.get(names_key)
        name_overrides = name_overrides if isinstance(name_overrides, dict) else {}
        overrides = migrated.get(overrides_key)
        converted: dict[str, str] = {}
        for scene in LEGACY_DOMAIN_PRESET_NAMES:
            text = _prompt_text(overrides.get(scene)) if isinstance(overrides, dict) else ""
            if not text:
                continue
            shown = str(name_overrides.get(scene) or "").strip() or scene
            name = _unique_extra_name(shown, set(extras))
            extras[name] = {"_base": text}
            converted[scene] = name
        legacy_text = str(migrated.get(custom_key) or "").strip()
        if legacy_text:
            legacy_name = _unique_extra_name(LEGACY_CUSTOM_PROMPT_NAME, set(extras))
            extras[legacy_name] = {"_base": legacy_text}
            converted["自定义"] = legacy_name

        if preset in LEGACY_DOMAIN_PRESET_NAMES:
            smart, selected = True, converted.get(preset, NO_EXTRA_PROMPT)
        elif preset == "自定义":
            smart, selected = False, converted.get("自定义", NO_EXTRA_PROMPT)
        elif preset in extras:
            smart, selected = False, preset
        elif preset == NO_EXTRA_PROMPT:
            smart, selected = False, NO_EXTRA_PROMPT
        else:
            # 指向一个已经不存在的提示词：按新默认来。
            smart, selected = True, NO_EXTRA_PROMPT

        migrated[smart_key] = smart
        migrated[preset_key] = selected
        migrated[named_key] = extras
        migrated[custom_key] = ""
        migrated[names_key] = {}
        migrated[overrides_key] = {}
        migrated[disabled_key] = []
    return migrated


def _has_legacy_prompt_data(payload: dict | None) -> bool:
    """Whether the v35 prompt migration rewrites something the user chose or wrote.

    A file still on the old defaults carries nothing worth a backup; one with an
    edited scene, a renamed or disabled scene, the V9 custom prompt, or a page on
    one of the retired scenes does.
    """
    if not isinstance(payload, dict):
        return False
    for prefix in _PROMPT_LAYER_SURFACES:
        for field in ("custom_prompt", "domain_name_overrides", "domain_prompt_overrides", "domain_disabled_presets"):
            if payload.get(f"{prefix}{field}"):
                return True
        preset = str(payload.get(f"{prefix}domain_preset") or "").strip()
        if preset in LEGACY_DOMAIN_PRESET_NAMES[1:] or preset == "自定义":
            return True
    return False


def _migrate_prompt_layers(payload: dict, stored_version: int) -> dict:
    """Read-time hook for :func:`migrate_prompt_layers` (files older than v35)."""
    if stored_version >= 35:
        return payload
    return migrate_prompt_layers(payload)


# v37 及以前的默认值。只用来判断用户改没改过速率：改过的话升级前先备份旧文件，
# 因为升级后这些值一律改用 v38 的新默认值（maple 2026-10-14 定）。默认值本身在
# v38 变了（并发 20 → 30、Word 字符 800 → 1000 等），所以这里写死旧值，不引用
# config 里的现行常量。
_PRE_V38_TEXT_THROUGHPUT_DEFAULTS = {
    "cloud": {"batch_size": 20, "concurrency": 20},
    "local": {"batch_size": 12, "concurrency": 3},
}


_PRE_V38_PAGE_CONCURRENCY_DEFAULTS = {"cloud": 2, "local": 3}


_PRE_V38_REVIEW_CONCURRENCY_DEFAULTS = {"cloud": 1, "local": 3}


_PRE_V38_PAGE_RETRY_DEFAULT = 1


_PRE_V38_REQUEST_RETRY_DEFAULT = 2


_PRE_V38_WORD_BATCH_DEFAULTS = {
    "max_chars_per_batch": ("batch_chars", 800),
    "max_paragraphs_per_batch": ("batch_items", 8),
    "strict_retry_attempts": ("item_retry", 3),
}


RATE_SURFACE_KEYS = ("excel", "word", "pdf_text", "cad")


# PDF 整页图像翻译的三项，v38 起归「翻译参数 · 速率与重试」，升级时同样回到默认值。
_PDF_RATE_KEYS = ("page_generation_concurrency", "page_retry_attempts", "review_concurrency")


def _changed_int(value: object, old_default: int) -> int | None:
    """The stored value if the user changed it, else None."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return None if value == old_default else value


def _migrate_rate_settings(payload: dict, stored_version: int) -> dict:
    """v38: 速率与重试挪到「翻译参数」，按文件类型保存，升级时一律改用新默认值。

    v37 的值是按旧默认值调的（同时请求数上限 20、Word 每批 800 字等），带到新位置
    会让新默认值对调过的人不生效，所以不带（maple 2026-10-14 定）：

    * word_batch、request_retry 两节移除，rate 节为空（各项按云端/本地取默认值）；
    * pdf 节的同时生成页数、单页重试次数、同时审核页数回到默认值；
    * 按模型存的 model_throughput_profiles 保留（清洗记忆库仍按它取速率），文档翻译、
      PDF 与图片翻译、审核三个模型不再读它。

    用户改过的值只留在备份里：第一次写回前 save_settings 按 _has_legacy_rate_data
    先备份旧文件。
    """
    if stored_version >= 38:
        return payload
    migrated = dict(payload)
    migrated.pop("request_retry", None)
    migrated.pop("word_batch", None)
    migrated["rate"] = {}
    if isinstance(migrated.get("pdf"), dict):
        migrated["pdf"] = {
            key: value for key, value in migrated["pdf"].items() if key not in _PDF_RATE_KEYS
        }
    return migrated


def _migrate_translation_types(payload: dict, stored_version: int) -> dict:
    """v40: 侧栏里各翻译类型的顺序和显示改记在 ``translation_types`` 一处（内置四种也能改）。

    v39 只有运行时注册的类型有 ``extensions.<id>.pinned`` / ``order``。调过顺序（有非 0 的
    order）的，按内置四种在前、扩展类型按 order 排在后面写成 ``order``，与 v39 侧栏里看到的
    顺序一致。``pinned`` 不再沿用：v40 起扩展类型默认不显示，要用的在「扩展」页打开
    （v39 没有发布过，「默认显示」只在开发版里出现过）。旧字段原样留着，不再写。
    """
    if stored_version >= 40 or "translation_types" in payload:
        return payload
    extensions = payload.get("extensions")
    entries = [
        (ext_id, entry) for ext_id, entry in (extensions.items() if isinstance(extensions, dict) else ())
        if isinstance(entry, dict)
    ]

    def position(entry: dict) -> int:
        value = entry.get("order")
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    order: list[str] = []
    if any(position(entry) for _ext_id, entry in entries):
        ranked = sorted(enumerate(entries), key=lambda item: (position(item[1][1]), item[0]))
        order = ["excel", "word", "pdf", "cad", *(f"ext:{ext_id}" for _index, (ext_id, _entry) in ranked)]
    migrated = dict(payload)
    migrated["translation_types"] = {"order": order, "visible": {}}
    return migrated
