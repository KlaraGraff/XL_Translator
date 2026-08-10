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
from settings import AppSettings

DOCUMENT_CONFIG_EXPORT_TYPE = "translator_document_config"
DOCUMENT_CONFIG_EXPORT_VERSION = 1

# Nested setting objects that describe document translation behaviour.
DOCUMENT_CONFIG_SECTIONS = (
    "excel_review",
    "word_review",
    "word_batch",
    "word_conversion",
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
    "custom_prompt",
    "domain_name_overrides",
    "domain_prompt_overrides",
    "excel_domain_preset",
    "excel_custom_prompt",
    "excel_domain_name_overrides",
    "excel_domain_prompt_overrides",
    "word_domain_preset",
    "word_custom_prompt",
    "word_domain_name_overrides",
    "word_domain_prompt_overrides",
)

# What each key is called in the import preview, so the dialog can say what is
# about to change in the user's words rather than in field names.
DOCUMENT_CONFIG_LABELS = {
    "excel_review": "Excel 复核标记",
    "word_review": "Word 复核标记",
    "word_batch": "Word 批次与重试",
    "word_conversion": "Word 预处理",
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
            "这个文件不是文档翻译配置，无法导入。模型服务的配置请在「模型服务」页导入。"
        )
    version = raw.get("version")
    if not isinstance(version, int) or version > DOCUMENT_CONFIG_EXPORT_VERSION:
        raise ValueError("这个文档翻译配置来自更高版本的应用，当前版本读不了。")
    document = raw.get("document")
    if not isinstance(document, dict):
        raise ValueError("文件里没有 document 段，没有可导入的内容。")

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
    for field in DOCUMENT_CONFIG_FIELDS:
        if field in document:
            parsed[field] = document[field]
    if not parsed:
        raise ValueError("这个文件里没有可导入的文档翻译配置。")
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
        if key in DOCUMENT_CONFIG_SECTIONS and isinstance(value, dict):
            current = dict(payload.get(key) or {})
            current.update(value)
            payload[key] = current
            continue
        payload[key] = value
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
