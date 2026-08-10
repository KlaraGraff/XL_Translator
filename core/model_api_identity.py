"""Helpers for comparing model API usage across translation tasks."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from config import normalize_cloud_base_url
from core.connection_pool import allocate_connection
from core.model_roles import (
    EffectiveModelConfig,
    ROLE_IMAGE,
    ROLE_CLEANER,
    ROLE_PDF_REVIEW,
    ROLE_TRANSLATION,
    list_effective_role_connections,
    resolve_effective_model_config,
)
from core.model_throughput import get_model_throughput
from settings import AppSettings, api_key_scope, connection_key_scope


ApiGroupSignature = tuple[str, str, str, str]


@dataclass(frozen=True)
class TaskApiContext:
    api_groups: frozenset[ApiGroupSignature]
    key_overrides: dict[str, str]
    # JSON-safe metadata frozen at task start.  Keys are represented only by
    # their scoped API identity and never by secret values.
    model_snapshot: dict[str, dict[str, object]] | None = None
    # The frozen role-to-connection mapping and the total request pressure for
    # each connection are required by the Phase 7 group scheduler.  They are
    # intentionally derived from the effective configuration, never from a
    # mutable settings object after a task has started.
    role_groups: dict[str, ApiGroupSignature] = field(default_factory=dict)
    group_concurrency: dict[ApiGroupSignature, int] = field(default_factory=dict)
    # The pool entry each role was allocated, plus the ordered fallback chain
    # frozen with it.  A task keeps its chain so a runtime switch stays inside
    # the set of connections that existed when it started.
    role_connection_ids: dict[str, str] = field(default_factory=dict)
    role_connection_chains: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # True when a role had to share a connection that another task already
    # occupies, which is what the concurrency warning is really about.
    shared_connection_roles: frozenset[str] = frozenset()


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
    if normalized_page == "tm_clean":
        return (ROLE_CLEANER,)
    if normalized_page == "pdf_translate":
        roles = [ROLE_IMAGE]
        if bool(settings.pdf.review_enabled):
            roles.append(ROLE_PDF_REVIEW)
        return tuple(roles)
    return ()


def task_api_context_for_page(
    settings: AppSettings,
    page_key: str,
    *,
    busy_connection_ids: frozenset[str] = frozenset(),
    spread: bool | None = None,
) -> TaskApiContext:
    """Resolve the API footprint and credential snapshot for one page.

    ``busy_connection_ids`` are the pool entries other active tasks already
    occupy.  Defaults keep the pre-pool behaviour: no spreading, primary
    connection, so callers that do not care are unaffected.
    """
    if spread is None:
        spread = bool(getattr(settings, "spread_tasks_across_connections", False))

    role_connection_ids: dict[str, str] = {}
    role_connection_chains: dict[str, tuple[str, ...]] = {}
    shared_roles: set[str] = set()
    configs: list[EffectiveModelConfig] = []
    # The entries a running task may switch to.  Kept apart from ``configs``
    # because only the allocated entry describes what the task is using now:
    # the API footprint, the model snapshot and the concurrency budget must not
    # count connections nothing has dialled.
    fallback_configs: list[EffectiveModelConfig] = []
    taken: set[str] = set(busy_connection_ids)
    for role in task_model_roles_for_page(settings, page_key):
        # A following role dials its source's pool, so allocate from that pool.
        # Allocating from its own idle pool recorded a connection id nothing
        # connected to, which both mislabelled the panel's "occupied" markers
        # and let a follower and its source double-book one endpoint while
        # spreading.
        allocation = allocate_connection(
            list_effective_role_connections(settings, role),
            busy_connection_ids=frozenset(taken),
            spread=spread,
        )
        # A task holding several roles must not hand the same connection to
        # two of them when the pool could have separated them.
        taken.add(allocation.connection.id)
        role_connection_ids[role] = allocation.connection.id
        role_connection_chains[role] = tuple(
            conn.id for conn in allocation.candidates
        )
        if allocation.shared:
            shared_roles.add(role)
        configs.append(
            resolve_effective_model_config(
                settings,
                role,
                connection_id=allocation.connection.id,
            )
        )
        for candidate in allocation.candidates:
            if candidate.id == allocation.connection.id:
                continue
            fallback_configs.append(
                resolve_effective_model_config(
                    settings,
                    role,
                    connection_id=candidate.id,
                )
            )

    api_groups = frozenset(api_group_signature_from_config(config) for config in configs)
    key_overrides: dict[str, str] = {}
    model_snapshot: dict[str, dict[str, object]] = {}
    role_groups: dict[str, ApiGroupSignature] = {}
    group_concurrency: dict[ApiGroupSignature, int] = {}
    for config in configs:
        throughput = get_model_throughput(settings, config)
        group = api_group_signature_from_config(config)
        role_groups[config.role] = group
        group_concurrency[group] = (
            group_concurrency.get(group, 0) + max(1, int(throughput.concurrency))
        )
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
            # This is a one-way identifier used solely to compare frozen
            # credential scopes.  It is not the API key or a recoverable form
            # of one.
            "connection_id": hashlib.sha256(
                repr(group).encode("utf-8")
            ).hexdigest()[:16],
            # The pool entry this role ran on, and the chain it may fall back
            # to.  Distinct from "connection_id" above, which identifies the
            # upstream API rather than the configured entry.
            "pool_connection_id": role_connection_ids.get(config.role, ""),
            "pool_connection_label": config.connection_label,
            "pool_connection_chain": list(
                role_connection_chains.get(config.role, ())
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
        # Freeze under the connection scope first: two pool entries can share a
        # provider and Base URL, so the provider scope alone would let one
        # entry's key overwrite the other's for the whole task.
        pool_scope = connection_key_scope(config.connection_id)
        if pool_scope:
            key_overrides[pool_scope] = api_key
        scope = api_key_scope(provider, base_url)
        if scope:
            key_overrides.setdefault(scope, api_key)

    # A task that switches connections mid-run builds the next engine from this
    # snapshot, on a worker thread where nothing else is visible.  Without the
    # candidates' own keys in it, that build resolved through the provider scope
    # — which the allocated entry just pinned to its own key — so failing over
    # to a second account on the same service replayed the credential that had
    # already failed.  Only the ``conn::`` scopes are added: the provider scope
    # still belongs to the entry the task started on.
    for config in fallback_configs:
        if config.mode != "cloud":
            continue
        api_key = str(config.api_key or "").strip()
        scope = connection_key_scope(config.connection_id)
        if api_key and scope:
            key_overrides.setdefault(scope, api_key)

    return TaskApiContext(
        api_groups=api_groups,
        key_overrides=key_overrides,
        model_snapshot=model_snapshot,
        role_groups=role_groups,
        group_concurrency=group_concurrency,
        role_connection_ids=role_connection_ids,
        role_connection_chains=role_connection_chains,
        shared_connection_roles=frozenset(shared_roles),
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
