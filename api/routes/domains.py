"""Per-surface translation prompt settings: 自动识别场景 switch and the user's 补充要求."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from loguru import logger

from core.model_roles import (
    ROLE_TRANSLATION,
)
from core.engine_dispatcher import build_role_engine
from core.language_registry import get_target_lang_display
from core.surface_settings import is_extension_key, set_surface_setting, surface_setting
from config import (
    CORE_TRANSLATION_PROMPT,
    EXTRA_PROMPT_HEADER,
    NO_EXTRA_PROMPT,
    RESERVED_EXTRA_PROMPT_NAMES,
)
from settings import (
    load_settings,
    save_settings,
)
from api.schemas import (
    DomainSettingsPayload,
    DomainPromptOptimizePayload,
)
from api.surfaces import all_surfaces

# 「整理」一条还没保存的新补充要求时，前端用这个名字占位。
_UNSAVED_PROMPT_MARKER = "自定义提示词"


def _surface_or_404(surface: str) -> str:
    normalized = str(surface or "").strip().lower()
    if normalized in {"excel", "word", "cad"}:
        return normalized
    # 运行时注册的类型（ext:<id>）只认已注册的：设置写在 extensions[<id>]，不能凭空建一份。
    if is_extension_key(normalized) and any(item.key == normalized for item in all_surfaces()):
        return normalized
    raise HTTPException(404, "找不到此翻译页面，请刷新后重试。")


def register(app: FastAPI) -> None:
    """Register the domains routes on ``app``."""

    @app.get("/api/domains/builtin-prompts")
    def get_domain_builtin_prompts() -> dict[str, Any]:
        """只读端点：内置翻译规则的唯一事实来源，设置页照这里显示。

        必须注册在 `/api/domains/{surface}` 之前——否则会被那条参数路由吃掉，
        变成 404「Unknown translation surface.」。前端不再自己存一份内置文本，
        config.py 改了当场生效，不需要跟着发前端版本。
        """
        return {"core_prompt": CORE_TRANSLATION_PROMPT, "extra_header": EXTRA_PROMPT_HEADER}

    @app.get("/api/domains/{surface}")
    def get_domain_settings(surface: str) -> dict[str, Any]:
        normalized = _surface_or_404(surface)
        settings = load_settings()
        return {
            "surface": normalized,
            "smart_context": bool(surface_setting(settings, normalized, "smart_context")),
            "preset": surface_setting(settings, normalized, "domain_preset"),
            "custom_prompts": surface_setting(settings, normalized, "domain_custom_prompts", {}),
        }

    @app.post("/api/domains/{surface}/optimize")
    def optimize_domain_prompt(
        surface: str, payload: DomainPromptOptimizePayload
    ) -> dict[str, Any]:
        """Suggest an improved 补充要求 using the translation model role.

        The result is deliberately returned to the editor and never persisted
        here; the user still controls the subsequent save operation.
        """
        normalized = _surface_or_404(surface)
        preset = str(payload.preset or "").strip()
        prompt = str(payload.prompt or "").strip()
        if not prompt:
            raise HTTPException(422, "提示词不能为空。")
        settings = load_settings()
        custom_names = {
            str(name).strip()
            for name in surface_setting(settings, normalized, "domain_custom_prompts", {})
            if str(name).strip()
        }
        if preset != _UNSAVED_PROMPT_MARKER and preset not in custom_names:
            raise HTTPException(422, "请选择一个有效的提示词后再整理。")
        target_code = str(payload.target_lang or "").strip() or str(
            surface_setting(settings, normalized, "target_lang", "")
            or settings.target_lang
            or ""
        ).strip()
        try:
            target_name = get_target_lang_display(target_code, include_optional=True)
        except Exception:
            target_name = target_code or "目标语言"
        system = (
            "你是文档翻译产品的提示词编辑。用户写了一段翻译「补充要求」，它会接在固定的内置翻译规则"
            "后面一起发给翻译模型。内置规则已经要求按专业表达翻译、术语一致、编号与规格原样保留、"
            "日期单位按目标语言习惯书写、只返回译文，所以补充要求里不需要重复这些。"
            "请把用户的补充要求改得更清晰、稳定、可执行，只返回改好的正文，"
            "不要 Markdown 代码块、标题或解释；保留原有约束、数字和占位符，不要加入无关要求。"
        )
        user = (
            f"补充要求名称：{preset}\n目标语言：{target_name}\n\n"
            f"原补充要求：\n{prompt}"
        )
        try:
            engine = build_role_engine(settings, ROLE_TRANSLATION)
            result = str(engine.chat(system, user) or "").strip()
        except Exception as exc:
            logger.warning("domain prompt optimization failed for {}: {}", normalized, exc)
            raise HTTPException(502, "模型优化失败，请检查文档翻译模型配置。") from exc
        if result.startswith("```") and result.endswith("```"):
            result = result.split("\n", 1)[1] if "\n" in result else result
            result = result.rsplit("```", 1)[0].strip()
        if not result:
            raise HTTPException(502, "模型未返回可用的优化结果。")
        return {"surface": normalized, "preset": preset, "target_lang": target_code, "prompt": result}

    @app.put("/api/domains/{surface}")
    def put_domain_settings(surface: str, payload: DomainSettingsPayload) -> dict[str, Any]:
        normalized = _surface_or_404(surface)
        normalized_custom: dict[str, dict[str, str]] = {}
        for name, value in payload.custom_prompts.items():
            clean_name = str(name or "").strip()
            if not clean_name or clean_name in RESERVED_EXTRA_PROMPT_NAMES:
                raise HTTPException(422, "补充要求的名称不能为空，也不能使用「无」「自定义」或原场景名称。")
            if isinstance(value, str):
                prompt_text = value.strip()
            elif isinstance(value, dict):
                prompt_text = str(value.get("_base") or "").strip()
                if not prompt_text:
                    prompt_text = next(
                        (str(text).strip() for text in value.values()
                         if isinstance(text, str) and text.strip()),
                        "",
                    )
            else:
                prompt_text = ""
            if prompt_text:
                normalized_custom[clean_name] = {"_base": prompt_text}
        preset = str(payload.preset or "").strip() or NO_EXTRA_PROMPT
        if preset != NO_EXTRA_PROMPT and preset not in normalized_custom:
            raise HTTPException(422, "找不到所选的补充要求，请刷新后重试。")
        settings = load_settings()
        set_surface_setting(settings, normalized, "smart_context", bool(payload.smart_context))
        set_surface_setting(settings, normalized, "domain_preset", preset)
        set_surface_setting(settings, normalized, "domain_custom_prompts", normalized_custom)
        save_settings(settings)
        return get_domain_settings(normalized)
