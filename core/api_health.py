"""Startup health checks and persisted state for translation backends."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

from config import APP_DATA_DIR
from core.connectivity_check import ConnectivityResult, check_connectivity
from settings import AppSettings, get_cloud_provider_config, get_key


API_HEALTH_STATE_PATH = APP_DATA_DIR / "api_health_state.json"
API_HEALTH_OK = "ok"
API_HEALTH_FAILED = "failed"
API_HEALTH_NOTICE_TTL_SECONDS = 8


@dataclass(frozen=True)
class ApiHealthRecord:
    signature: str
    last_status: str
    last_checked_date: str
    checked_at: str
    result_status: str = ""
    message: str = ""
    detail: str = ""
    provider: str = ""
    model: str = ""


def _hash_secret_for_signature(secret: str) -> str:
    if not secret:
        return ""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]


def _signature_base_url(provider: str, base_url: str) -> str:
    if provider == "openai":
        return ""
    return str(base_url or "").strip().rstrip("/")


def build_connectivity_signature(settings: AppSettings) -> str:
    """Return a stable, non-secret signature for the active backend config."""
    engine_settings = settings.engine
    if engine_settings.mode == "local":
        return "|".join(
            [
                "local",
                str(engine_settings.local_provider or ""),
                str(engine_settings.local_model or engine_settings.ollama_model or ""),
                str(engine_settings.local_base_url or "").strip().rstrip("/"),
            ]
        )

    provider = engine_settings.cloud_provider
    provider_config = get_cloud_provider_config(engine_settings, provider)
    base_url = provider_config.cloud_base_url
    return "|".join(
        [
            "cloud",
            provider,
            provider_config.cloud_model,
            _signature_base_url(provider, base_url),
            _hash_secret_for_signature(get_key(provider, base_url)),
        ]
    )


def load_api_health_record(path: Path = API_HEALTH_STATE_PATH) -> ApiHealthRecord | None:
    """Load the latest persisted backend health result."""
    if not path.exists():
        return None

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

    if not isinstance(payload, dict):
        return None

    signature = str(payload.get("signature") or "").strip()
    last_status = str(payload.get("last_status") or "").strip()
    if not signature or last_status not in {API_HEALTH_OK, API_HEALTH_FAILED}:
        return None

    return ApiHealthRecord(
        signature=signature,
        last_status=last_status,
        last_checked_date=str(payload.get("last_checked_date") or ""),
        checked_at=str(payload.get("checked_at") or ""),
        result_status=str(payload.get("result_status") or ""),
        message=str(payload.get("message") or ""),
        detail=str(payload.get("detail") or ""),
        provider=str(payload.get("provider") or ""),
        model=str(payload.get("model") or ""),
    )


def save_api_health_record(
    record: ApiHealthRecord,
    path: Path = API_HEALTH_STATE_PATH,
) -> None:
    """Persist the latest backend health result."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(record), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def should_check_api_health_on_startup(
    settings: AppSettings,
    *,
    record: ApiHealthRecord | None = None,
    today: date | None = None,
) -> bool:
    """Decide whether the current app session should run an automatic check."""
    today_value = today or date.today()
    current_signature = build_connectivity_signature(settings)

    if record is None:
        return True
    if record.signature != current_signature:
        return True
    if record.last_status != API_HEALTH_OK:
        return True
    return record.last_checked_date != today_value.isoformat()


def _trim_text(value: object, *, max_length: int = 500) -> str:
    text = str(value or "").strip()
    if len(text) <= max_length:
        return text
    return text[: max_length - 3] + "..."


def _record_from_connectivity_result(
    *,
    signature: str,
    result: ConnectivityResult,
    today: date,
) -> ApiHealthRecord:
    return ApiHealthRecord(
        signature=signature,
        last_status=API_HEALTH_OK if result.ok else API_HEALTH_FAILED,
        last_checked_date=today.isoformat(),
        checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        result_status=_trim_text(result.status, max_length=80),
        message=_trim_text(result.message),
        detail=_trim_text(result.detail),
        provider=_trim_text(result.provider, max_length=80),
        model=_trim_text(result.model, max_length=160),
    )


def run_api_health_check(
    settings: AppSettings,
    *,
    today: date | None = None,
    checker: Callable[..., ConnectivityResult] = check_connectivity,
    state_path: Path = API_HEALTH_STATE_PATH,
) -> ApiHealthRecord:
    """Run the configured backend check and persist its result."""
    today_value = today or date.today()
    signature = build_connectivity_signature(settings)

    try:
        result = checker(settings)
    except Exception as exc:  # noqa: BLE001 - health monitor must never crash startup.
        result = ConnectivityResult(
            ok=False,
            status="check_error",
            message=f"连接检测异常：{_trim_text(exc)}",
        )

    record = _record_from_connectivity_result(
        signature=signature,
        result=result,
        today=today_value,
    )
    save_api_health_record(record, state_path)
    return record
