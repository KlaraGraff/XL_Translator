"""Per-surface domain (industry) prompt settings."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from loguru import logger

from core.model_roles import (
    ROLE_TRANSLATION,
)
from core.engine_dispatcher import build_role_engine
from core.language_registry import get_target_lang_display
from config import (
    DOMAIN_PRESETS,
)
from settings import (
    load_settings,
    save_settings,
)
from api.schemas import (
    DomainSettingsPayload,
    DomainPromptOptimizePayload,
)


def register(app: FastAPI) -> None:
    """Register the domains routes on ``app``."""

    @app.get("/api/domains/builtin-prompts")
    def get_domain_builtin_prompts() -> dict[str, Any]:
        """只读端点：内置领域 Prompt 的唯一事实来源。

        必须注册在 `/api/domains/{surface}` 之前——否则会被那条参数路由吃掉，
        变成 404「Unknown translation surface.」。

        前端曾经自己维护一份 DOMAIN_PRESETS 的硬拷贝用于「文本框默认显示什么、
        跟内置默认比对是否算改动」，那份拷贝会随 config.py 更新而过期。用户在
        陈旧文本上编辑保存，走的是「跟默认值不一样就存成覆盖」这条路（见
        put_domain_settings 调用方 settings.ts 的比对逻辑），于是把陈旧文本连
        同它缺的规则一起，永久固化成 prompt_overrides，此后再也追不上
        DOMAIN_PRESETS 的更新。改为前端每次都从这里取内置文本，config.py 改了
        当场生效，不需要跟着发前端版本。
        """
        return {"presets": DOMAIN_PRESETS}

    @app.get("/api/domains/{surface}")
    def get_domain_settings(surface: str) -> dict[str, Any]:
        normalized = str(surface or "").strip().lower()
        if normalized not in {"excel", "word", "cad"}:
            raise HTTPException(404, "找不到这个翻译页面，请刷新页面后重试。")
        settings = load_settings()
        prefix = f"{normalized}_"
        return {
            "surface": normalized,
            # 声明顺序就是展示顺序：「无」写在字典首位，排序会把它按拼音丢到中间去，
            # 和 config.py 里「排在第一项」的约定打架。
            "presets": list(DOMAIN_PRESETS),
            "preset": getattr(settings, f"{prefix}domain_preset"),
            "custom_prompt": getattr(settings, f"{prefix}custom_prompt"),
            "prompt_overrides": getattr(settings, f"{prefix}domain_prompt_overrides"),
            "name_overrides": getattr(settings, f"{prefix}domain_name_overrides"),
            "custom_prompts": getattr(settings, f"{prefix}domain_custom_prompts", {}),
            "disabled_presets": list(
                getattr(settings, f"{prefix}domain_disabled_presets", []) or []
            ),
        }

    @app.post("/api/domains/{surface}/optimize")
    def optimize_domain_prompt(
        surface: str, payload: DomainPromptOptimizePayload
    ) -> dict[str, Any]:
        """Suggest an improved prompt using the translation model role.

        The result is deliberately returned to the editor and never persisted
        here; the user still controls the subsequent save operation.
        """
        normalized = str(surface or "").strip().lower()
        if normalized not in {"excel", "word", "cad"}:
            raise HTTPException(404, "找不到这个翻译页面，请刷新页面后重试。")
        preset = str(payload.preset or "").strip()
        prompt = str(payload.prompt or "").strip()
        if not prompt:
            raise HTTPException(422, "Prompt 不能为空。")
        settings = load_settings()
        custom_names = {
            str(name).strip()
            for name in getattr(settings, f"{normalized}_domain_custom_prompts", {})
            if str(name).strip()
        }
        # The add form has not been persisted yet, so it uses a temporary
        # marker. Named saved prompts are accepted alongside built-ins.
        is_new_prompt = preset == "自定义提示词"
        if (
            not preset
            or preset in {"无", "自定义"}
            or (preset not in DOMAIN_PRESETS and preset not in custom_names and not is_new_prompt)
        ):
            raise HTTPException(422, "请选择一个有效的提示词后再整理。")
        target_code = str(payload.target_lang or "").strip() or str(
            getattr(settings, f"{normalized}_target_lang", "")
            or settings.target_lang
            or ""
        ).strip()
        try:
            target_name = get_target_lang_display(target_code, include_optional=True)
        except Exception:
            target_name = target_code or "目标语言"
        system = (
            "你是文档翻译产品的提示词编辑。请优化用户给出的领域翻译 Prompt，"
            "只返回优化后的 Prompt 正文，不要 Markdown 代码块、标题或解释。"
            "必须保留原有约束、数字和占位符，避免加入与领域无关的要求。"
        )
        user = (
            f"当前提示词名称：{preset}\n目标语言：{target_name}\n"
            "请让它更清晰、稳定、可执行，并适合直接作为翻译模型的 system prompt。\n\n"
            f"原 Prompt：\n{prompt}"
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
        normalized = str(surface or "").strip().lower()
        if normalized not in {"excel", "word", "cad"}:
            raise HTTPException(404, "找不到这个翻译页面，请刷新页面后重试。")
        preset = str(payload.preset or "").strip()
        custom_names = {
            str(name).strip()
            for name in payload.custom_prompts
            if str(name).strip()
        }
        if preset not in DOMAIN_PRESETS and preset not in custom_names:
            raise HTTPException(422, "未知专业领域预设。")
        # Deleting the active preset is a valid operation: fall back to the
        # neutral option in the same write instead of leaving settings stuck.
        if preset in set(payload.disabled_presets):
            preset = "无"
        if preset == "自定义" and not str(payload.custom_prompt or "").strip():
            raise HTTPException(422, "自定义领域必须填写完整 Prompt。")
        settings = load_settings()
        prefix = f"{normalized}_"
        setattr(settings, f"{prefix}domain_preset", preset)
        setattr(settings, f"{prefix}custom_prompt", str(payload.custom_prompt or ""))
        normalized_overrides: dict[str, dict[str, str]] = {}
        for name, value in payload.prompt_overrides.items():
            if isinstance(value, str):
                prompt_text = value.strip()
            elif isinstance(value, dict):
                # Canonical storage keeps one language-neutral value.  Prefer
                # an explicit _base entry, otherwise retain the first usable
                # legacy language value while collapsing the old map.
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
                normalized_overrides[str(name)] = {"_base": prompt_text}
        setattr(settings, f"{prefix}domain_prompt_overrides", normalized_overrides)
        setattr(settings, f"{prefix}domain_name_overrides", dict(payload.name_overrides))
        normalized_custom: dict[str, dict[str, str]] = {}
        for name, value in payload.custom_prompts.items():
            clean_name = str(name or "").strip()
            if not clean_name or clean_name in DOMAIN_PRESETS:
                raise HTTPException(422, "自定义提示词名称不能为空且不能与内置预设重名。")
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
        disabled = []
        for name in payload.disabled_presets:
            clean_name = str(name or "").strip()
            if clean_name in DOMAIN_PRESETS and clean_name != "无":
                disabled.append(clean_name)
        setattr(settings, f"{prefix}domain_custom_prompts", normalized_custom)
        setattr(settings, f"{prefix}domain_disabled_presets", list(dict.fromkeys(disabled)))
        save_settings(settings)
        return get_domain_settings(normalized)
