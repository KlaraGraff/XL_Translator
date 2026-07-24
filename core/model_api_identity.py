"""Helpers for comparing model API usage across translation tasks."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from config import normalize_cloud_base_url
from core.model_roles import (
    EffectiveModelConfig,
    ROLE_IMAGE,
    ROLE_PDF_REVIEW,
    ROLE_TRANSLATION,
    resolve_effective_model_config,
)
from core.model_throughput import get_model_throughput
from settings import AppSettings, api_key_scope


ApiGroupSignature = tuple[str, str, str, str]


@dataclass(frozen=True)
class TaskApiContext:
    api_groups: frozenset[ApiGroupSignature]
    key_overrides: dict[str, str]
    # JSON-safe metadata frozen at task start.  Keys are represented only by
    # their scoped API identity and never by secret values.
    model_snapshot: dict[str, dict[str, object]] | None = None


def api_group_signature_from_config(config: EffectiveModelConfig) -> ApiGroupSignature:
    """Return a stable, non-secret identity for the upstream API used by a model."""
    mode = str(config.mode or "").strip() or "cloud"
    provider = str(config.provider or "").strip()
    if mode == "cloud":
        base_url = normalize_cloud_base_url(provider, config.base_url).rstrip("/")
        key_hash = _hash_secret(config.api_key)
        return ("cloud", provider, base_url, key_hash)

    base_url = str(config.base_url or "").strip().rstrip("/")
    return ("local", provider, base_url, "")


def task_model_roles_for_page(settings: AppSettings, page_key: str) -> tuple[str, ...]:
    """Map one translation page to the model roles it will occupy."""
    normalized_page = str(page_key or "").strip()
    if normalized_page in {"excel_translate", "word_translate"}:
        return (ROLE_TRANSLATION,)
    if normalized_page == "pdf_translate":
        roles = [ROLE_IMAGE]
        if bool(settings.pdf.review_enabled):
            roles.append(ROLE_PDF_REVIEW)
        return tuple(roles)
    return ()


def task_api_context_for_page(
    settings: AppSettings,
    page_key: str,
) -> TaskApiContext:
    """Resolve the API footprint and credential snapshot for one page."""
    configs = [
        resolve_effective_model_config(settings, role)
        for role in task_model_roles_for_page(settings, page_key)
    ]
    api_groups = frozenset(api_group_signature_from_config(config) for config in configs)
    key_overrides: dict[str, str] = {}
    model_snapshot: dict[str, dict[str, object]] = {}
    for config in configs:
        throughput = get_model_throughput(settings, config)
        if config.mode == "cloud":
            base_url = normalize_cloud_base_url(
                config.provider,
                config.base_url,
            ).rstrip("/")
        else:
            base_url = str(config.base_url or "").strip().rstrip("/")
        model_snapshot[config.role] = {
            "role": config.role,
            "label": config.label,
            "capability": config.capability,
            "mode": config.mode,
            "provider": config.provider,
            "model": config.model,
            "base_url": base_url,
            "source_role": config.source_role,
            "follows": config.follows,
            "api_scope": (
                api_key_scope(config.provider, base_url)
                if config.mode == "cloud"
                else ""
            ),
            "throughput": {
                "profile_key": throughput.profile_key,
                "batch_size": throughput.batch_size,
                "concurrency": throughput.concurrency,
            },
        }
        if config.mode != "cloud":
            continue
        api_key = str(config.api_key or "").strip()
        if not api_key:
            continue
        provider = str(config.provider or "").strip()
        base_url = normalize_cloud_base_url(provider, config.base_url).rstrip("/")
        scope = api_key_scope(provider, base_url)
        if scope:
            key_overrides[scope] = api_key
    return TaskApiContext(
        api_groups=api_groups,
        key_overrides=key_overrides,
        model_snapshot=model_snapshot,
    )


def task_api_groups_for_page(
    settings: AppSettings,
    page_key: str,
) -> frozenset[ApiGroupSignature]:
    """Resolve only the API footprint for lock conflict checks."""
    return task_api_context_for_page(settings, page_key).api_groups


def _hash_secret(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
