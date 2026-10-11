"""Settings, languages, API keys and the health probe."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Response

from core import tm_manager
from core.language_preflight import (
    build_language_preflight_prompt,
    extract_language_probe_texts,
    parse_preflight_languages,
)
from core.language_registry import (
    append_custom_target_lang,
    get_language_catalog,
    get_default_source_selection,
    get_default_target_lang,
    get_source_language_options,
    get_target_language_options,
    get_tm_language_pairs,
    remove_custom_target_lang,
    update_custom_target_lang_display,
)
from core.model_roles import (
    ROLE_CLEANER,
    ROLE_IMAGE,
    ROLE_PDF_REVIEW,
    ROLE_TRANSLATION,
    model_config_signature,
    resolve_effective_model_config,
)
from settings import (
    AppSettings,
    carry_settings_baseline,
    load_settings,
    save_settings,
    set_cloud_provider_config,
)
from api.schemas import (
    CustomTargetLanguagePayload,
    LanguagePreflightRequest,
)
from api.routes._model_helpers import (
    _reject_illegal_follow_payload,
)


def register(app: FastAPI) -> None:
    """Register the settings routes on ``app``."""

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "service": "translator-sidecar"}

    @app.get("/api/languages")
    def get_languages() -> dict[str, Any]:
        """Return the single language directory used by every selector."""
        settings = load_settings()
        custom = settings.custom_target_langs
        return {
            "languages": get_language_catalog(custom),
            "source_options": get_source_language_options(custom),
            "target_options": get_target_language_options(custom),
            "defaults": {
                "source_lang": get_default_source_selection(),
                "target_lang": get_default_target_lang(),
                "pdf_target_lang": settings.pdf.target_lang,
            },
            "recent_target_langs": list(settings.recent_target_langs),
        }

    @app.post("/api/languages/custom", status_code=201)
    def create_custom_language(payload: CustomTargetLanguagePayload) -> dict[str, Any]:
        settings = load_settings()
        try:
            custom, code = append_custom_target_lang(
                settings.custom_target_langs,
                payload.name,
                payload.description,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        settings.custom_target_langs = custom
        save_settings(settings)
        return {"code": code, "name": payload.name.strip(), "description": payload.description.strip()}

    @app.put("/api/languages/custom/{language_code}")
    def update_custom_language(
        language_code: str,
        payload: CustomTargetLanguagePayload,
    ) -> dict[str, Any]:
        settings = load_settings()
        existing = next(
            (entry for entry in settings.custom_target_langs if entry.code == language_code),
            None,
        )
        if existing is None:
            raise HTTPException(404, "自定义语言不存在。")
        try:
            settings.custom_target_langs = update_custom_target_lang_display(
                settings.custom_target_langs,
                language_code,
                payload.name,
                payload.description,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        save_settings(settings)
        updated = next(
            item for item in settings.custom_target_langs if item.code == language_code
        )
        return {
            "code": language_code,
            "name": updated.name,
            "description": updated.description,
        }

    @app.delete("/api/languages/custom/{language_code}", status_code=204)
    def delete_custom_language(language_code: str) -> Response:
        settings = load_settings()
        existing = next(
            (entry for entry in settings.custom_target_langs if entry.code == language_code),
            None,
        )
        if existing is None:
            raise HTTPException(404, "自定义语言不存在。")
        if hasattr(tm_manager, "count_entries_referencing_language") and tm_manager.count_entries_referencing_language(language_code):
            raise HTTPException(409, "该自定义语言仍被 TM 条目引用，请先导出或清空相关语言对。")
        settings.custom_target_langs = remove_custom_target_lang(
            settings.custom_target_langs,
            language_code,
        )
        if settings.target_lang == language_code:
            settings.target_lang = "en"
        if settings.excel_target_lang == language_code:
            settings.excel_target_lang = "en"
        if settings.word_target_lang == language_code:
            settings.word_target_lang = "en"
        if settings.pdf.target_lang == language_code:
            settings.pdf.target_lang = "zh"
        save_settings(settings)
        return Response(status_code=204)

    @app.post("/api/languages/preflight")
    def language_preflight(payload: LanguagePreflightRequest) -> dict[str, Any]:
        samples = extract_language_probe_texts(payload.texts)
        detected = parse_preflight_languages(payload.detected_languages or [])
        return {
            "file_id": payload.file_id,
            "candidate_count": len(samples),
            "requested": bool(samples),
            "source_langs": list(detected),
            "tm_pairs": get_tm_language_pairs(detected, payload.target_lang),
            "prompt": build_language_preflight_prompt(samples) if samples else "",
        }

    @app.get("/api/tm/language-pairs")
    def get_tm_language_pairs_catalog() -> dict[str, Any]:
        settings = load_settings()
        source_options = [
            option for option in get_source_language_options(settings.custom_target_langs)
            if option.get("code") != "auto"
        ]
        target_options = get_target_language_options(settings.custom_target_langs)
        return {
            "source_options": source_options,
            "target_options": target_options,
            "selected": {
                "source_lang": settings.tm_source_lang,
                "target_lang": settings.tm_target_lang,
            },
            "recent": list(settings.recent_tm_lang_pairs),
        }

    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        return load_settings().model_dump(mode="json")

    @app.put("/api/settings")
    def put_settings(payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise HTTPException(422, "Settings payload must be a JSON object.")
        # 只查这份 payload 自己点名的角色：这个端点是「当前设置 + 补丁」合并保存，
        # 拿合并结果去查的话，磁盘上早就存着的一条不合法跟随会让每一次保存都 422。
        _reject_illegal_follow_payload(payload)
        before_settings = load_settings()
        before_signatures: dict[str, str] = {}
        for role in (ROLE_TRANSLATION, ROLE_CLEANER, ROLE_IMAGE, ROLE_PDF_REVIEW):
            try:
                before_signatures[role] = model_config_signature(
                    resolve_effective_model_config(before_settings, role)
                )
            except Exception:
                continue
        current = before_settings.model_dump(mode="json")
        merged = _deep_merge(current, payload)
        try:
            settings = AppSettings.model_validate(merged)
        except Exception as exc:
            raise HTTPException(422, str(exc)) from exc
        # 校验器会在同一 provider 已经存过「服务商记忆」时，把这次刚合并进来的
        # cloud_model/cloud_base_url 悄悄换回记忆里的旧值——这是为切换 provider
        # 时找回上次用过的模型设计的，但代价是这个通用合并端点只改
        # engine.cloud_model 会返回 200 却被覆盖回旧值，跟没存一样。payload 里
        # 显式给出的字段必须真的落盘，所以在这里按用户原样写回服务商记忆，
        # 和 PUT /api/models/roles/{role} 走的是同一条路径。
        _persist_explicit_cloud_overrides(settings, payload)
        # Rebuilding through model_validate produces an object with no
        # load-time snapshot, which would make the save a full overwrite for
        # the one endpoint the UI hits on every switch flip.
        carry_settings_baseline(before_settings, settings)
        for role in (ROLE_TRANSLATION, ROLE_CLEANER, ROLE_IMAGE, ROLE_PDF_REVIEW):
            try:
                resolve_effective_model_config(settings, role)
            except Exception as exc:
                raise HTTPException(422, str(exc)) from exc
        for role in (ROLE_TRANSLATION, ROLE_CLEANER, ROLE_IMAGE, ROLE_PDF_REVIEW):
            try:
                changed = before_signatures.get(role) != model_config_signature(
                    resolve_effective_model_config(settings, role)
                )
            except Exception:
                changed = True
            if not changed:
                continue
            owner = settings.engine if role == ROLE_TRANSLATION else {
                ROLE_CLEANER: settings.cleaner_model_role,
                ROLE_IMAGE: settings.image_model_role,
                ROLE_PDF_REVIEW: settings.pdf_review_model_role,
            }[role]
            owner.availability_status = "unknown"
            owner.availability_message = "当前配置尚未测试。"
            owner.availability_signature = ""
            owner.availability_checked_at = ""
        save_settings(settings)
        return settings.model_dump(mode="json")


def _deep_merge(current: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    merged = dict(current)
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


# 四个「引擎归属」对象共享同一套 cloud_model/cloud_base_url + cloud_provider_configs
# 校验逻辑（见 settings.py 的 EngineSettings / ModelRoleSettings），因而共享同一个
# 「服务商记忆覆盖回」隐患。
_CLOUD_MEMORY_OWNER_FIELDS = (
    "engine",
    "cleaner_model_role",
    "image_model_role",
    "pdf_review_model_role",
)


def _persist_explicit_cloud_overrides(
    settings: AppSettings, payload: dict[str, Any]
) -> None:
    """让 payload 里显式给出的 cloud_model/cloud_base_url 真的落盘。

    ``AppSettings`` 校验之后，这几个字段可能已经被同 provider 下的服务商记忆
    悄悄换回旧值（见调用处的注释）。这里只认 payload 原始结构里显式出现的键——
    没提到的字段维持校验器的结果不动，只把用户这次明确改动的值写回去。
    """
    for field in _CLOUD_MEMORY_OWNER_FIELDS:
        raw = payload.get(field)
        if not isinstance(raw, dict):
            continue
        wants_model = "cloud_model" in raw
        wants_base_url = "cloud_base_url" in raw
        if not wants_model and not wants_base_url:
            continue
        owner = getattr(settings, field, None)
        if owner is None:
            continue
        set_cloud_provider_config(
            owner,
            owner.cloud_provider,
            cloud_model=str(raw.get("cloud_model", "") or "").strip() if wants_model else None,
            cloud_base_url=(
                str(raw.get("cloud_base_url", "") or "").strip() if wants_base_url else None
            ),
        )
