"""Per-model throughput profile helpers."""

from __future__ import annotations

from dataclasses import dataclass

from config import (
    CHUNK_CLOUD_MAX,
    CHUNK_CLOUD_DEFAULT,
    CHUNK_CLOUD_MIN,
    CHUNK_LOCAL_MAX,
    CHUNK_LOCAL_DEFAULT,
    CHUNK_LOCAL_MIN,
    CONCURRENCY_CLOUD_DEFAULT,
    CONCURRENCY_LOCAL_DEFAULT,
    PDF_PAGE_CONCURRENCY_DEFAULT,
    PDF_PAGE_CONCURRENCY_SAFETY_CAP,
    get_cloud_concurrency_bounds,
    get_local_concurrency_bounds,
    normalize_cloud_base_url,
)
from core.model_roles import (
    EffectiveModelConfig,
    ROLE_CLEANER,
    ROLE_IMAGE,
    ROLE_PDF_REVIEW,
    ROLE_TRANSLATION,
)
from settings import AppSettings, ModelThroughputSettings


TEXT_THROUGHPUT_ROLES = {ROLE_TRANSLATION, ROLE_CLEANER}


@dataclass(frozen=True)
class EffectiveModelThroughput:
    profile_key: str
    batch_size: int | None
    concurrency: int


def model_throughput_key(config: EffectiveModelConfig) -> str:
    """Build a stable profile key for one effective model configuration."""
    base_url = str(config.base_url or "").strip()
    if config.mode == "cloud":
        base_url = normalize_cloud_base_url(config.provider, base_url).rstrip("/")
    return "|".join(
        [
            str(config.role or "").strip(),
            str(config.mode or "").strip(),
            str(config.provider or "").strip(),
            base_url,
            str(config.model or "").strip(),
        ]
    )


def batch_size_bounds(config: EffectiveModelConfig) -> tuple[int, int] | None:
    if config.role not in TEXT_THROUGHPUT_ROLES:
        return None
    if config.mode == "local":
        return CHUNK_LOCAL_MIN, CHUNK_LOCAL_MAX
    return CHUNK_CLOUD_MIN, CHUNK_CLOUD_MAX


def concurrency_bounds(config: EffectiveModelConfig) -> tuple[int, int]:
    if config.mode == "local":
        return get_local_concurrency_bounds(False)
    if config.role in {ROLE_IMAGE, ROLE_PDF_REVIEW}:
        return 1, PDF_PAGE_CONCURRENCY_SAFETY_CAP
    return get_cloud_concurrency_bounds(False)


def _throughput_bounds(settings: AppSettings, config: EffectiveModelConfig):
    unlocked = bool(getattr(settings.engine, "concurrency_unlocked", False))
    batch = None if unlocked else batch_size_bounds(config)
    concurrency = None if unlocked else concurrency_bounds(config)
    return batch, concurrency


def supports_batch_size(config: EffectiveModelConfig) -> bool:
    return batch_size_bounds(config) is not None


def _clamp_int(value, *, minimum: int, maximum: int, fallback: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = fallback
    return max(minimum, min(maximum, number))


def _default_batch_size(
    settings: AppSettings,
    config: EffectiveModelConfig,
) -> int | None:
    if not supports_batch_size(config):
        return None
    bounds, _ = _throughput_bounds(settings, config)
    bounds = bounds or (1, 2**31 - 1)
    minimum, maximum = bounds
    return _clamp_int(
        CHUNK_LOCAL_DEFAULT if config.mode == "local" else CHUNK_CLOUD_DEFAULT,
        minimum=minimum,
        maximum=maximum,
        fallback=minimum,
    )


def _default_concurrency(settings: AppSettings, config: EffectiveModelConfig) -> int:
    if config.mode == "local":
        raw = CONCURRENCY_LOCAL_DEFAULT
    elif config.role == ROLE_IMAGE:
        raw = PDF_PAGE_CONCURRENCY_DEFAULT
    elif config.role == ROLE_PDF_REVIEW:
        raw = 1
    else:
        raw = CONCURRENCY_CLOUD_DEFAULT
    _, bounds = _throughput_bounds(settings, config)
    minimum, maximum = bounds or (1, 2**31 - 1)
    return _clamp_int(
        raw,
        minimum=minimum,
        maximum=maximum,
        fallback=CONCURRENCY_CLOUD_DEFAULT,
    )


def get_model_throughput(
    settings: AppSettings,
    config: EffectiveModelConfig,
) -> EffectiveModelThroughput:
    key = model_throughput_key(config)
    profiles = getattr(settings, "model_throughput_profiles", {}) or {}
    profile = profiles.get(key)
    if not isinstance(profile, ModelThroughputSettings):
        try:
            profile = ModelThroughputSettings.model_validate(profile or {})
        except Exception:
            profile = ModelThroughputSettings()

    default_batch = _default_batch_size(settings, config)
    if default_batch is None:
        batch_size = None
    else:
        bounds, _ = _throughput_bounds(settings, config)
        minimum, maximum = bounds or (1, 2**31 - 1)
        batch_size = _clamp_int(
            profile.batch_size,
            minimum=minimum,
            maximum=maximum,
            fallback=default_batch,
        )

    _, bounds = _throughput_bounds(settings, config)
    minimum, maximum = bounds or (1, 2**31 - 1)
    concurrency = _clamp_int(
        profile.concurrency,
        minimum=minimum,
        maximum=maximum,
        fallback=_default_concurrency(settings, config),
    )
    return EffectiveModelThroughput(
        profile_key=key,
        batch_size=batch_size,
        concurrency=concurrency,
    )


def set_model_throughput(
    settings: AppSettings,
    config: EffectiveModelConfig,
    *,
    batch_size: int | None = None,
    concurrency: int | None = None,
) -> EffectiveModelThroughput:
    key = model_throughput_key(config)
    current = get_model_throughput(settings, config)
    profile = ModelThroughputSettings(
        batch_size=current.batch_size,
        concurrency=current.concurrency,
    )

    if batch_size is not None and supports_batch_size(config):
        if (
            not isinstance(batch_size, int)
            or isinstance(batch_size, bool)
            or not 1 <= batch_size <= 2**31 - 1
        ):
            raise ValueError("batch_size must be a positive integer within the protocol limit")
        bounds, _ = _throughput_bounds(settings, config)
        minimum, maximum = bounds or (1, int(batch_size))
        profile.batch_size = _clamp_int(
            batch_size,
            minimum=minimum,
            maximum=maximum,
            fallback=current.batch_size or minimum,
        )
    if concurrency is not None:
        if (
            not isinstance(concurrency, int)
            or isinstance(concurrency, bool)
            or not 1 <= concurrency <= 2**31 - 1
        ):
            raise ValueError("concurrency must be a positive integer within the protocol limit")
        _, bounds = _throughput_bounds(settings, config)
        minimum, maximum = bounds or (1, int(concurrency))
        profile.concurrency = _clamp_int(
            concurrency,
            minimum=minimum,
            maximum=maximum,
            fallback=current.concurrency,
        )

    settings.model_throughput_profiles[key] = profile
    return get_model_throughput(settings, config)


def reset_model_throughput(
    settings: AppSettings,
    config: EffectiveModelConfig,
) -> EffectiveModelThroughput:
    """Discard one saved profile and return deterministic recommended values.

    Runtime changes are always profile-scoped.  Removing a profile must not
    inherit a tuning choice from another model through legacy global fields,
    so the fallback is the role/mode recommendation computed above.
    """
    settings.model_throughput_profiles.pop(model_throughput_key(config), None)
    return get_model_throughput(settings, config)
