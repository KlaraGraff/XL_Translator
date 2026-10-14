"""Portable document-translation configuration import and export.

This is the second of the two bundles the settings UI offers.  Where
``core.model_config`` moves the whole model service (connections, model names,
throughput), this one moves how documents get translated: the per-surface
review marks, batching, PDF options, output behaviour, languages and the domain
prompts.  Both are deliberately whole-bundle: the user's complaint about the
previous design was that every model and every section had to be exported one
at a time.

Two things are left out on purpose:

* secrets — there are none in here, and none may ever be added;
* machine-local paths — ``custom_output_dir`` and the remembered source folders
  point at directories that do not exist on the machine importing the file.  A
  bundle that silently repoints someone's output at a missing path is worse
  than one that leaves their own choice alone.
"""

from __future__ import annotations

from typing import Any

from app_meta import APP_NAME, APP_VERSION
from settings import AppSettings, migrate_prompt_layers, migrate_legacy_rate_sections

DOCUMENT_CONFIG_EXPORT_TYPE = "translator_document_config"
# v2：领域 Prompt 覆盖曾升级为按目标语言分层；v3 收敛为每个预设一份 `_base`
# 主 Prompt。v4：提示词拆成内置规则 + 「自动识别场景」开关 + 补充要求（设置 v35），
# v3 及以前的包导入时按 settings.migrate_prompt_layers 转换。v5：速率与重试按文件类型
# 放在 rate 节（设置 v38），取代 word_batch / request_retry；v4 及以前的包导入时换算。
DOCUMENT_CONFIG_EXPORT_VERSION = 5
# 包里出现这些字段之一，就说明带了那个页面的提示词设置。
_PROMPT_PAGE_FIELDS = (
    "domain_preset",
    "custom_prompt",
    "domain_name_overrides",
    "domain_prompt_overrides",
    "domain_custom_prompts",
    "domain_disabled_presets",
)

# Nested setting objects that describe document translation behaviour.
DOCUMENT_CONFIG_SECTIONS = (
    "excel_review",
    "word_review",
    "word_conversion",
    "rate",
    "pdf",
    "tm",
    "output",
    "excel_output",
    "word_output",
    "pdf_output",
)
# Dropped from every exported section: a path from another machine.
DOCUMENT_CONFIG_LOCAL_PATH_FIELDS = frozenset(
    {"use_custom_output_dir", "custom_output_dir"}
)
# v4 及以前的包里的速率与重试，导入时换算成 rate 节。
_LEGACY_RATE_SECTIONS = ("word_batch", "request_retry")
# Top-level fields, kept flat because that is how they live in the settings
# model.  Languages and domain prompts are the part users actually want to hand
# to a colleague.
DOCUMENT_CONFIG_FIELDS = (
    "source_lang",
    "target_lang",
    "excel_source_lang",
    "word_source_lang",
    "excel_target_lang",
    "word_target_lang",
    "custom_target_langs",
    "domain_preset",
    "domain_smart_context",
    "custom_prompt",
    "domain_name_overrides",
    "domain_prompt_overrides",
    "excel_domain_preset",
    "excel_smart_context",
    "excel_custom_prompt",
    "excel_domain_name_overrides",
    "excel_domain_prompt_overrides",
    "excel_domain_custom_prompts",
    "excel_domain_disabled_presets",
    "word_domain_preset",
    "word_smart_context",
    "word_custom_prompt",
    "word_domain_name_overrides",
    "word_domain_prompt_overrides",
    "word_domain_custom_prompts",
    "word_domain_disabled_presets",
    "cad_domain_preset",
    "cad_smart_context",
    "cad_custom_prompt",
    "cad_domain_name_overrides",
    "cad_domain_prompt_overrides",
    "cad_domain_custom_prompts",
    "cad_domain_disabled_presets",
)

# What each key is called in the import preview, so the dialog can say what is
# about to change in the user's words rather than in field names.
DOCUMENT_CONFIG_LABELS = {
    "excel_review": "Excel 复核标记",
    "word_review": "Word 复核标记",
    "word_conversion": "Word 预处理",
    "rate": "速率与重试",
    "pdf": "PDF 参数",
    "tm": "翻译记忆库参数",
    "output": "输出选项",
    "excel_output": "Excel 输出选项",
    "word_output": "Word 输出选项",
    "pdf_output": "PDF 输出选项",
    "languages": "语言设置",
    "domain": "领域与提示词",
}
_LANGUAGE_FIELDS = frozenset(
    {
        "source_lang",
        "target_lang",
        "excel_source_lang",
        "word_source_lang",
        "excel_target_lang",
        "word_target_lang",
        "custom_target_langs",
    }
)

_PROMPT_MAP_FIELDS = frozenset(
    {
        "domain_prompt_overrides",
        "excel_domain_prompt_overrides",
        "word_domain_prompt_overrides",
        "cad_domain_prompt_overrides",
        "domain_custom_prompts",
        "excel_domain_custom_prompts",
        "word_domain_custom_prompts",
        "cad_domain_custom_prompts",
    }
)


def _canonicalize_prompt_map(value: object) -> dict[str, dict[str, str]]:
    """Collapse legacy language maps to one `_base` prompt per preset."""
    if not isinstance(value, dict):
        return {}
    result: dict[str, dict[str, str]] = {}
    for raw_name, raw_prompt in value.items():
        name = str(raw_name or "").strip()
        if not name:
            continue
        if isinstance(raw_prompt, str):
            prompt = raw_prompt.strip()
        elif isinstance(raw_prompt, dict):
            prompt = str(raw_prompt.get("_base") or "").strip()
            if not prompt:
                prompt = next(
                    (
                        str(item).strip()
                        for item in raw_prompt.values()
                        if isinstance(item, str) and item.strip()
                    ),
                    "",
                )
        else:
            prompt = ""
        if prompt:
            result[name] = {"_base": prompt}
    return result


def build_document_config_export_payload(settings: AppSettings) -> dict[str, Any]:
    """Build the full document-translation bundle for the current settings."""
    payload = settings.model_dump(mode="json")
    document: dict[str, Any] = {}
    for section in DOCUMENT_CONFIG_SECTIONS:
        values = payload.get(section)
        if not isinstance(values, dict):
            continue
        document[section] = {
            key: value
            for key, value in values.items()
            if key not in DOCUMENT_CONFIG_LOCAL_PATH_FIELDS
        }
    for field in DOCUMENT_CONFIG_FIELDS:
        if field in payload:
            document[field] = payload[field]
    return {
        "type": DOCUMENT_CONFIG_EXPORT_TYPE,
        "version": DOCUMENT_CONFIG_EXPORT_VERSION,
        "app": APP_NAME,
        "app_version": APP_VERSION,
        "document": document,
    }


def parse_document_config_import(raw: object) -> dict[str, Any]:
    """Return only the recognised document fields carried by a bundle.

    Unknown keys are dropped rather than rejected: a file written by a future
    version must still import the parts this version understands.
    """
    if not isinstance(raw, dict):
        raise ValueError("导入的文件必须是一个 JSON 对象。")
    if raw.get("type") != DOCUMENT_CONFIG_EXPORT_TYPE:
        raise ValueError(
            "此文件不是文档翻译配置，无法导入。模型服务的配置请在「模型服务」页导入。"
        )
    version = raw.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        # 缺版本号说的是「这文件不对」，不是「你的应用太旧」。原来两种情况共用
        # 一句话，用户照着提示去升级应用，升完还是导不进来。
        raise ValueError(
            "此文件缺少版本号，可能不是本应用导出的文档翻译配置，请重新导出。"
        )
    if version > DOCUMENT_CONFIG_EXPORT_VERSION:
        raise ValueError(
            "此文档翻译配置来自更高版本的应用，当前版本无法读取。请先将应用升级到最新版本后再导入。"
        )
    document = raw.get("document")
    if not isinstance(document, dict):
        raise ValueError("文件中没有 document 段，无可导入的内容。")

    parsed: dict[str, Any] = {}
    for section in DOCUMENT_CONFIG_SECTIONS:
        values = document.get(section)
        if not isinstance(values, dict):
            continue
        kept = {
            key: value
            for key, value in values.items()
            if key not in DOCUMENT_CONFIG_LOCAL_PATH_FIELDS
        }
        if kept:
            parsed[section] = kept
    if version < 5 and "rate" not in parsed:
        legacy = {
            section: document[section]
            for section in _LEGACY_RATE_SECTIONS
            if isinstance(document.get(section), dict)
        }
        # 和设置文件升级同一套换算：等于旧默认值的项不带过来，按本机的新默认值走。
        rate = {
            surface: values
            for surface, values in migrate_legacy_rate_sections(legacy).items()
            if values
        }
        if rate:
            parsed["rate"] = rate
    for field in DOCUMENT_CONFIG_FIELDS:
        if field in document:
            parsed[field] = document[field]
    if not parsed:
        raise ValueError("此文件中没有可导入的文档翻译配置。")
    if version < 4:
        # 旧包里是场景预设：带了哪个页面的提示词，就把那个页面按 v35 的规则转换，
        # 转换结果（含新的开关字段）整页写入；没带的页面保持导入方自己的设置。
        prefixes = [
            prefix
            for prefix in ("", "excel_", "word_", "cad_")
            if any(f"{prefix}{field}" in parsed for field in _PROMPT_PAGE_FIELDS)
        ]
        if prefixes:
            migrated = migrate_prompt_layers(parsed, prefixes)
            for prefix in prefixes:
                for field in (*_PROMPT_PAGE_FIELDS, "smart_context" if prefix else "domain_smart_context"):
                    key = f"{prefix}{field}"
                    if key in migrated:
                        parsed[key] = migrated[key]
    return parsed


def apply_document_config_import(
    settings: AppSettings, imported: dict[str, Any]
) -> AppSettings:
    """Merge a parsed bundle onto a copy of ``settings``.

    Only fields the file actually carries are written; a section the file omits
    keeps the importing machine's own value.  The caller owns persistence.
    """
    payload = settings.model_dump(mode="json")
    for key, value in imported.items():
        if key == "rate" and isinstance(value, dict):
            # 按文件类型逐项合并：包里没带的文件类型、没带的项，保持本机原样。
            current = dict(payload.get(key) or {})
            for surface, values in value.items():
                if isinstance(values, dict):
                    current[surface] = {**dict(current.get(surface) or {}), **values}
            payload[key] = current
            continue
        if key in DOCUMENT_CONFIG_SECTIONS and isinstance(value, dict):
            current = dict(payload.get(key) or {})
            current.update(value)
            payload[key] = current
            continue
        if key == "custom_target_langs":
            # 自定义语言取并集，本机已有的一条都不删。翻译记忆库的词条是按语言存
            # 的，删掉一门自定义语言，那门语言下的全部译文当场从库里消失——而用户
            # 做的只是「导入同事的文档配置」。本机的排在前面：重名时校验器保留先
            # 出现的那条，也就保住了它原来的语言代码，TM 才对得上。
            payload[key] = [
                *(payload.get(key) or []),
                *(value if isinstance(value, list) else []),
            ]
            continue
        payload[key] = (
            _canonicalize_prompt_map(value)
            if key in _PROMPT_MAP_FIELDS
            else value
        )
    # Validation is what turns a bad colour string or an out-of-range batch size
    # into a 422 instead of an unusable settings file.
    return AppSettings.model_validate(payload)


def summarize_document_config_import(imported: dict[str, Any]) -> list[str]:
    """Name the areas a parsed bundle would change, for the preview dialog."""
    areas: list[str] = []
    for section in DOCUMENT_CONFIG_SECTIONS:
        if section in imported:
            areas.append(DOCUMENT_CONFIG_LABELS[section])
    if any(field in imported for field in _LANGUAGE_FIELDS):
        areas.append(DOCUMENT_CONFIG_LABELS["languages"])
    if any(
        field in imported
        for field in DOCUMENT_CONFIG_FIELDS
        if field not in _LANGUAGE_FIELDS
    ):
        areas.append(DOCUMENT_CONFIG_LABELS["domain"])
    return areas
