"""Conservative, per-connection text-model upgrades at sidecar startup.

models.dev supplies only candidate metadata.  The configured endpoint must
list the exact model ID and answer a real text request before a setting moves.
No credentials, document content, or catalog responses are persisted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from threading import Event, RLock

import httpx
from loguru import logger

from core.model_catalog import (
    build_model_catalog_signature,
    fetch_openai_compatible_models,
)
from core.model_roles import (
    ROLE_CLEANER,
    ROLE_PDF_REVIEW,
    ROLE_TRANSLATION,
    list_effective_role_connections,
    resolve_effective_model_config,
    update_role_connection,
)
from core.text_transport import classify_http_error, request_text
from settings import (
    AppSettings,
    ModelUpgradeState,
    load_settings,
    update_settings_atomically,
)

CATALOG_URL = "https://models.dev/api.json"
CATALOG_TIMEOUT_SECONDS = 10.0
PROBE_TIMEOUT_SECONDS = 18.0
FAILURE_LIMIT = 3
_OPENAI_MODEL = re.compile(r"^gpt-(\d+)(?:\.(\d+))?-(luna|sol)$")
_ROLE_NAMES = (ROLE_TRANSLATION, ROLE_CLEANER, ROLE_PDF_REVIEW)
_DEEPSEEK_SUCCESSORS = {"deepseek-v4-flash": "deepseek-flash"}
_ROLLED_BACK: dict[tuple[str, str, str, str], str] = {}
_ROLLED_BACK_LOCK = RLock()


@dataclass(frozen=True)
class UpgradeTarget:
    role: str
    connection_id: str
    provider: str
    model: str
    base_url: str
    api_mode: str
    api_key: str
    signature: str

    @property
    def state_key(self) -> str:
        return f"{self.role}:{self.connection_id}"


def _version(model: str) -> tuple[int, int] | None:
    match = _OPENAI_MODEL.fullmatch(model)
    return (int(match[1]), int(match[2] or 0)) if match else None


def _family(model: str) -> str:
    match = _OPENAI_MODEL.fullmatch(model)
    return f"gpt-{match[3]}" if match else ""


def _cost_not_higher(old: dict, new: dict) -> bool:
    old_cost = old.get("cost")
    new_cost = new.get("cost")
    if not isinstance(old_cost, dict) or not isinstance(new_cost, dict):
        return False
    try:
        return all(float(new_cost[k]) <= float(old_cost[k]) for k in ("input", "output"))
    except (KeyError, TypeError, ValueError):
        return False


def select_candidate(
    catalog: dict, current: str, blocked: str = "", *,
    provider_name: str = "openai", require_image: bool = False,
) -> str:
    """Choose a newer exact ID with the same vendor family and no price rise."""
    provider = catalog.get(provider_name) if isinstance(catalog, dict) else None
    models = provider.get("models") if isinstance(provider, dict) else None
    if not isinstance(models, dict) or current not in models:
        return ""
    if provider_name == "deepseek":
        candidate = _DEEPSEEK_SUCCESSORS.get(current, "")
        old = models.get(current)
        new = models.get(candidate)
        if (
            candidate == blocked
            or not isinstance(old, dict)
            or not isinstance(new, dict)
            or old.get("family") != "deepseek-flash"
            or new.get("family") != old.get("family")
            or not _cost_not_higher(old, new)
            or "text" not in (new.get("modalities") or {}).get("output", [])
            or (require_image and "image" not in (new.get("modalities") or {}).get("input", []))
        ):
            return ""
        return candidate
    if provider_name != "openai":
        return ""
    old = models[current]
    if not isinstance(old, dict) or old.get("family") != _family(current):
        return ""
    if require_image and "image" not in (old.get("modalities") or {}).get("input", []):
        return ""
    old_version = _version(current)
    blocked_version = _version(blocked)
    if old_version is None:
        return ""
    floor = max(old_version, blocked_version or old_version)
    old_date = str(old.get("release_date") or "")
    eligible: list[tuple[tuple[int, int], str]] = []
    for model, metadata in models.items():
        version = _version(model)
        if (
            version is None
            or version <= floor
            or _family(model) != _family(current)
            or not isinstance(metadata, dict)
            or metadata.get("family") != old.get("family")
            or str(metadata.get("release_date") or "") <= old_date
            or "text" not in (metadata.get("modalities") or {}).get("input", [])
            or (require_image and "image" not in (metadata.get("modalities") or {}).get("input", []))
            or "text" not in (metadata.get("modalities") or {}).get("output", [])
            or not _cost_not_higher(old, metadata)
        ):
            continue
        eligible.append((version, model))
    return max(eligible)[1] if eligible else ""


def _catalog() -> dict:
    with httpx.Client(timeout=CATALOG_TIMEOUT_SECONDS) as client:
        response = client.get(CATALOG_URL)
        response.raise_for_status()
        payload = response.json()
    return payload if isinstance(payload, dict) else {}


def _targets(settings: AppSettings) -> list[UpgradeTarget]:
    targets: list[UpgradeTarget] = []
    for role in _ROLE_NAMES:
        try:
            effective_pool = list_effective_role_connections(settings, role)
        except Exception:  # noqa: BLE001 - one invalid idle role must not block others
            continue
        for connection in effective_pool:
            try:
                config = resolve_effective_model_config(
                    settings, role, connection_id=connection.id,
                )
            except Exception:  # noqa: BLE001 - skip an incompatible role
                continue
            if config.mode != "cloud" or config.provider not in {"openai", "deepseek"}:
                continue
            state_key = f"{role}:{connection.id}"
            state = settings.model_upgrade_states.get(state_key)
            if state is not None and state.status == "manual":
                continue
            # Fresh adoption is restricted to the two approved starting IDs.
            # Later generations are eligible only when this app promoted the
            # connection and recorded that provenance.
            if state is None and config.model not in {
                "gpt-5.6-luna", "gpt-5.6-sol", "deepseek-v4-flash",
            }:
                continue
            if state is not None and config.model not in {state.source_model, state.candidate_model}:
                continue
            key = config.api_key
            if not key:
                continue
            signature = build_model_catalog_signature(
                provider=config.provider,
                api_key=key,
                base_url=config.base_url,
            )
            targets.append(UpgradeTarget(
                role=role,
                connection_id=connection.id,
                provider=config.provider,
                model=config.model,
                base_url=config.base_url,
                api_mode=config.api_mode,
                api_key=key,
                signature=signature,
            ))
    return targets


def _model_specific_failure(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return classify_http_error(exc)[0] == "model"
    return getattr(exc, "kind", "") == "model"


def _probe(target: UpgradeTarget, model: str) -> tuple[bool, Exception | None]:
    try:
        text, _route = request_text(
            base_url=target.base_url,
            api_key=target.api_key,
            model=model,
            system="Translate the user's text into French. Reply with the translation only.",
            user="Hello",
            api_mode=target.api_mode,
            connection_id=target.connection_id,
            model_role=target.role,
            total_seconds=PROBE_TIMEOUT_SECONDS,
        )
        return bool(text.strip()), None
    except Exception as exc:  # noqa: BLE001 - caller classifies the failure
        return False, exc


def _fresh_connection(settings: AppSettings, target: UpgradeTarget):
    try:
        connection = next(
            conn for conn in list_effective_role_connections(settings, target.role)
            if conn.id == target.connection_id
        )
        config = resolve_effective_model_config(
            settings, target.role, connection_id=target.connection_id,
        )
    except Exception:  # noqa: BLE001 - connection may have been deleted meanwhile
        return None
    if (
        config.provider != target.provider
        or config.mode != "cloud"
        or config.model != target.model
        or config.base_url != target.base_url
        or config.api_mode != target.api_mode
    ):
        return None
    if build_model_catalog_signature(
        provider=config.provider,
        api_key=config.api_key,
        base_url=config.base_url,
    ) != target.signature:
        return None
    return connection


def _set_role_model(settings: AppSettings, role: str, connection_id: str, model: str) -> None:
    # The target id belongs to this role even when it borrows a source
    # endpoint.  Never rewrite the source primary or every row in this role.
    update_role_connection(settings, role, connection_id, model=model)


def _save_failed_candidate(target: UpgradeTarget, candidate: str) -> None:
    def update(settings: AppSettings) -> bool:
        if _fresh_connection(settings, target) is None:
            return False
        old = settings.model_upgrade_states.get(target.state_key)
        if old is not None and old.status == "manual":
            return False
        failures = (
            min(FAILURE_LIMIT, old.consecutive_failures + 1)
            if old is not None
            and old.candidate_model == candidate
            and old.connection_signature == target.signature
            else 1
        )
        settings.model_upgrade_states[target.state_key] = ModelUpgradeState(
            source_model=target.model,
            candidate_model=candidate,
            connection_signature=target.signature,
            consecutive_failures=failures,
            status="paused" if failures >= FAILURE_LIMIT else "failed",
        )
        return True

    update_settings_atomically(update)


def _save_upgrade(target: UpgradeTarget, candidate: str) -> bool:
    def update(settings: AppSettings) -> bool:
        if _fresh_connection(settings, target) is None:
            return False
        old = settings.model_upgrade_states.get(target.state_key)
        if old is not None and old.status == "manual":
            return False
        _set_role_model(settings, target.role, target.connection_id, candidate)
        settings.model_upgrade_states[target.state_key] = ModelUpgradeState(
            source_model=target.model,
            candidate_model=candidate,
            connection_signature=target.signature,
            consecutive_failures=0,
            status="active",
        )
        return True

    return update_settings_atomically(update)


def auto_upgrade_models_on_startup(stop: Event | None = None) -> None:
    """Check candidate metadata in the background; never block app startup."""
    try:
        targets = _targets(load_settings())
        if not targets or (stop is not None and stop.is_set()):
            return
        catalog = _catalog()
    except Exception as exc:  # noqa: BLE001 - optional startup check
        logger.info("模型升级目录暂不可用：{}", type(exc).__name__)
        return
    for target in targets:
        if stop is not None and stop.is_set():
            return
        state = load_settings().model_upgrade_states.get(target.state_key)
        if state is not None and state.status == "manual":
            continue
        if state is not None and state.connection_signature != target.signature:
            # Changed credential or endpoint has no inherited failure history.
            state = None
        listed = None
        if state is not None and state.status == "active" and target.model == state.candidate_model:
            listed = fetch_openai_compatible_models(
                provider=target.provider, api_key=target.api_key, base_url=target.base_url,
            )
            if not listed.ok:
                continue
            if target.model not in listed.models:
                # A catalog omission alone is not enough to strand a working
                # connection.  Confirm the recorded predecessor is available
                # on this same account before restoring it.
                if state.source_model in listed.models:
                    old_ok, _error = _probe(target, state.source_model)
                    if old_ok and (stop is None or not stop.is_set()):
                        rollback_upgraded_model(
                            target.connection_id, target.model, target.base_url,
                            api_key=target.api_key, model_role=target.role,
                        )
                continue
        blocked = state.candidate_model if state is not None and state.status == "paused" else ""
        candidate = select_candidate(
            catalog, target.model, blocked,
            provider_name=target.provider,
            require_image=target.role == ROLE_PDF_REVIEW,
        )
        if not candidate:
            continue
        if listed is None:
            listed = fetch_openai_compatible_models(
                provider=target.provider,
                api_key=target.api_key,
                base_url=target.base_url,
            )
        if not listed.ok:
            continue
        if candidate not in listed.models:
            if stop is None or not stop.is_set():
                _save_failed_candidate(target, candidate)
            continue
        probe_ok, probe_error = _probe(target, candidate)
        if not probe_ok:
            if probe_error is not None and _model_specific_failure(probe_error):
                if stop is None or not stop.is_set():
                    _save_failed_candidate(target, candidate)
            continue
        if stop is None or not stop.is_set():
            if _save_upgrade(target, candidate):
                logger.info("连接 {} 的模型已自动升级：{} -> {}", target.connection_id, target.model, candidate)


def rollback_upgraded_model(
    connection_id: str, model: str, base_url: str, *, api_key: str = "",
    model_role: str = ROLE_TRANSLATION,
) -> str:
    """Rollback only a recorded candidate; return its working predecessor."""
    restored = ""

    def update(settings: AppSettings) -> bool:
        nonlocal restored
        state = settings.model_upgrade_states.get(f"{model_role}:{connection_id}")
        if state is None or state.status != "active" or state.candidate_model != model:
            return False
        allowed_openai = (
            _version(state.source_model) is not None
            and _family(state.source_model) == _family(model)
        )
        allowed_deepseek = _DEEPSEEK_SUCCESSORS.get(state.source_model) == model
        if not (allowed_openai or allowed_deepseek) or state.source_model == model:
            return False
        try:
            config = resolve_effective_model_config(
                settings, model_role, connection_id=connection_id,
            )
        except Exception:
            return False
        if (
            config.connection_id != connection_id
            or config.model != model
            or config.base_url != base_url
            or config.provider not in {"openai", "deepseek"}
        ):
            return False
        if api_key and state.connection_signature != build_model_catalog_signature(
            provider=config.provider, api_key=api_key, base_url=base_url,
        ):
            return False
        _set_role_model(settings, model_role, connection_id, state.source_model)
        failures = min(FAILURE_LIMIT, state.consecutive_failures + 1)
        state.consecutive_failures = failures
        state.status = "paused" if failures >= FAILURE_LIMIT else "failed"
        restored = state.source_model
        return True

    update_settings_atomically(update)
    if restored:
        with _ROLLED_BACK_LOCK:
            _ROLLED_BACK[(model_role, connection_id, model, base_url)] = restored
    return restored


def effective_model_after_rollback(
    connection_id: str, model: str, base_url: str,
    model_role: str = ROLE_TRANSLATION,
) -> str:
    """Keep frozen running tasks on the restored model after a rollback."""
    identity = (model_role, connection_id, model, base_url)
    with _ROLLED_BACK_LOCK:
        predecessor = _ROLLED_BACK.get(identity)
    if not predecessor:
        return model
    try:
        settings = load_settings()
        state = settings.model_upgrade_states.get(f"{model_role}:{connection_id}")
        config = resolve_effective_model_config(
            settings, model_role, connection_id=connection_id,
        )
        valid = (
            state is not None
            and state.candidate_model == model
            and state.source_model == predecessor
            and state.status in {"failed", "paused"}
            and config.connection_id == connection_id
            and config.model == predecessor
            and config.base_url == base_url
        )
    except Exception:  # noqa: BLE001 - settings read must not break the request
        valid = False
    if valid:
        return predecessor
    with _ROLLED_BACK_LOCK:
        _ROLLED_BACK.pop(identity, None)
    return model
