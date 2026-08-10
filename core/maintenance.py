"""Maintenance operations bounded to the current Translator data directory."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from config import APP_DATA_DIR, KEYS_PATH, LOG_PATH, SETTINGS_PATH
from core import diagnostics, tm_manager
from core.task_history import TaskHistoryStore
from core.task_logger import clear_log_files
import settings as settings_module
from settings import (
    SETTINGS_RECOVERY_SCOPE,
    TM_RECOVERY_SCOPE,
    AppSettings,
    clear_recovery_record,
    get_settings_schema_status,
    load_settings,
    read_recovery_record,
    recover_settings_file_if_needed,
    save_settings,
)


TASK_HISTORY_PATH = APP_DATA_DIR / "task_history.json"
WORKSPACES_DIR = APP_DATA_DIR / "workspaces"
API_HEALTH_STATE_PATH = APP_DATA_DIR / "api_health_state.json"
_TASK_HISTORY_LIMIT = 200
_LOG_FILE_LIMIT = 5
_LOG_FILE_SIZE_LIMIT = 5 * 1024 * 1024


class MaintenanceError(RuntimeError):
    """A requested destructive maintenance operation cannot safely proceed."""


@dataclass(frozen=True)
class MaintenanceResult:
    category: str
    removed_count: int
    outputs_affected: bool = False
    restart_required: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "category": self.category,
            "removed_count": self.removed_count,
            "outputs_affected": self.outputs_affected,
            "restart_required": self.restart_required,
        }


def data_overview(*, active_task_count: int = 0) -> dict[str, Any]:
    """Describe owned local data without exposing credentials or external paths."""
    categories = [
        _category("settings", "设置", [SETTINGS_PATH], clearable=True),
        _category("keys", "API Key", [KEYS_PATH], clearable=True),
        _category("tm", "翻译记忆库", _tm_paths(), clearable=True),
        _category("task_history", "任务摘要", [TASK_HISTORY_PATH], clearable=True),
        _category("logs", "结构化日志", _log_paths(), clearable=True),
        _category("diagnostics", "脱敏诊断", [diagnostics.DIAGNOSTICS_DIR], clearable=True),
        _category("workspaces", "临时工作区", [WORKSPACES_DIR], clearable=True),
    ]
    categories[0]["schema"] = get_settings_schema_status()
    categories[1]["key_count"] = _key_count()
    categories[2]["entry_count"] = _tm_entry_count()
    categories[2]["schema"] = tm_manager.get_schema_status()
    categories[3]["retention_limit"] = _TASK_HISTORY_LIMIT
    categories[4]["retention"] = {
        "max_files": _LOG_FILE_LIMIT,
        "max_file_bytes": _LOG_FILE_SIZE_LIMIT,
    }
    categories[5]["retention"] = diagnostics.diagnostic_overview()
    return {
        "app_data_dir": str(APP_DATA_DIR),
        "categories": categories,
        "active_task_count": max(0, int(active_task_count)),
        "activity_guarded_categories": [
            "keys",
            "tm",
            "task_history",
            "logs",
            "diagnostics",
            "reset_full",
        ],
        "outputs_protected": True,
    }


def data_health() -> dict[str, Any]:
    """Report how the local data files were opened, and repair them if needed.

    Reading this is what guarantees the three reported states are true rather
    than guessed: it inspects first, then runs the same idempotent open path
    the rest of the app uses, so a settings file or memory database that would
    otherwise block every write is fixed the moment the UI asks about it.

    Two states are worth a banner.  ``recreated`` is where the user lost the
    contents of a file and needs to be told where the backup is.
    ``unreadable`` is where the file is still there but the app cannot open
    it — nothing was destroyed, and nothing can be saved either, so saying
    "current" would leave the user watching every write fail with a healthy
    looking page in front of them.  ``adopted`` and ``upgraded`` kept
    everything and need no attention.
    """
    settings_before = get_settings_schema_status()
    tm_before = tm_manager.get_schema_status()

    # Inspect before repairing: the TM upgrade lands immediately, so asking
    # afterwards would only ever answer "current".
    try:
        recover_settings_file_if_needed()
    except Exception as exc:  # noqa: BLE001 - health must not fail on settings trouble
        logger.warning(f"设置文件自检失败：{exc}")
    try:
        tm_manager.init_db()
    except Exception as exc:  # noqa: BLE001 - health must not fail on TM trouble
        logger.warning(f"翻译记忆库自检失败：{exc}")

    record = read_recovery_record()
    return {
        "settings": _health_entry(
            settings_before,
            record.get(SETTINGS_RECOVERY_SCOPE),
            kept_state="adopted",
            after=get_settings_schema_status(),
        ),
        "tm": _health_entry(
            tm_before,
            record.get(TM_RECOVERY_SCOPE),
            kept_state="upgraded",
            after=tm_manager.get_schema_status(),
        ),
    }


def dismiss_recovery_notice() -> dict[str, Any]:
    """Clear the recovery notice after the user acknowledges it."""
    return {"cleared": clear_recovery_record()}


def _health_entry(
    status: dict[str, object],
    event: object,
    *,
    kept_state: str,
    after: dict[str, object],
) -> dict[str, Any]:
    current_version = status.get("current_version")
    if isinstance(event, dict):
        return {
            "state": "recreated",
            "stored_version": event.get("stored_version"),
            "current_version": current_version,
            "backup_path": str(event.get("backup_path") or ""),
        }
    # Read the state *after* the repair attempt for this one: a file that is
    # still unreadable is the case where the repair could not run, and it is
    # the only remaining state the user has to be told about.
    if str(after.get("state") or "") == "unreadable":
        return {
            "state": "unreadable",
            "stored_version": after.get("stored_version"),
            "current_version": current_version,
            "backup_path": "",
        }
    state = str(status.get("state") or "")
    return {
        "state": kept_state if state == kept_state else "current",
        "stored_version": status.get("stored_version"),
        "current_version": current_version,
        "backup_path": "",
    }


def reset_settings() -> MaintenanceResult:
    """Replace settings only, leaving keys and TM untouched."""
    save_settings(AppSettings(), replace_incompatible=True)
    return MaintenanceResult(category="settings", removed_count=1)


def reopen_quick_start() -> MaintenanceResult:
    settings = AppSettings.model_validate(load_current_settings_or_default())
    settings.onboarding.quick_start_completed = False
    save_settings(settings, replace_incompatible=True)
    return MaintenanceResult(category="quick_start", removed_count=0)


def clear_keys() -> MaintenanceResult:
    from settings import delete_all_keys

    return MaintenanceResult(category="keys", removed_count=delete_all_keys())


def clear_task_history(history: TaskHistoryStore | None = None) -> MaintenanceResult:
    removed = (history or TaskHistoryStore()).clear()
    return MaintenanceResult(category="task_history", removed_count=removed)


def clear_logs() -> MaintenanceResult:
    return MaintenanceResult(category="logs", removed_count=clear_log_files())


def clear_diagnostics() -> MaintenanceResult:
    return MaintenanceResult(category="diagnostics", removed_count=diagnostics.clear_diagnostic_records())


def delete_diagnostic(record_id: str) -> MaintenanceResult:
    if not diagnostics.delete_diagnostic_record(record_id):
        raise MaintenanceError("诊断记录不存在。")
    return MaintenanceResult(category="diagnostics", removed_count=1)


def clear_tm(*, lang_pair: str | None = None) -> MaintenanceResult:
    try:
        tm_manager.init_db()
    except tm_manager.TmSchemaError:
        # A database that cannot be opened is exactly what this button is the
        # way out of, so clearing all of it deletes the file instead of
        # failing on it.  One language pair still needs a readable database —
        # there is no way to keep the rest of it otherwise.
        if lang_pair:
            raise
        tm_manager.discard_database()
        tm_manager.init_db()
        return MaintenanceResult(category="tm", removed_count=0)
    removed = tm_manager.clear_entries(lang_pair=lang_pair)
    return MaintenanceResult(category="tm", removed_count=removed)


def clear_owned_workspaces() -> MaintenanceResult:
    """Remove only app-owned stale workspaces marked by Translator itself."""
    removed = 0
    if not WORKSPACES_DIR.exists():
        return MaintenanceResult(category="workspaces", removed_count=0)
    for candidate in WORKSPACES_DIR.iterdir():
        if candidate.is_symlink():
            continue
        marker = candidate / ".translator-workspace.json"
        if not candidate.is_dir() or not marker.is_file():
            continue
        if _remove_owned_path(candidate):
            removed += 1
    return MaintenanceResult(category="workspaces", removed_count=removed)


def reset_all_local_data() -> MaintenanceResult:
    """Remove known current-baseline data, never sources or user output folders."""
    removed = 0
    for path in _reset_paths():
        if _remove_owned_path(path):
            removed += 1
    return MaintenanceResult(
        category="reset_full",
        removed_count=removed,
        restart_required=True,
    )


def load_current_settings_or_default() -> dict[str, object]:
    """Use a normal current read, never copy old/incompatible settings into reset state."""
    return load_settings().model_dump(mode="json")


def _category(
    category_id: str,
    label: str,
    paths: list[Path],
    *,
    clearable: bool,
) -> dict[str, object]:
    return {
        "id": category_id,
        "label": label,
        "size_bytes": sum(_path_size(path) for path in paths),
        "count": sum(_path_count(path) for path in paths),
        "clearable": clearable,
        "contains_user_output": False,
    }


def _tm_paths() -> list[Path]:
    base = tm_manager.DB_PATH
    # Same sidecar list the TM itself backs up and removes: WAL mode leaves the
    # first two behind, rollback-journal mode (the fallback on a filesystem
    # that cannot do WAL) the third.  Missing one here would leave it behind
    # after a reset and under-report the size on the maintenance page.
    return [base, *tm_manager.db_sidecar_paths(base)]


def _log_paths() -> list[Path]:
    return [path for path in LOG_PATH.parent.glob(f"{LOG_PATH.name}*") if path.is_file()]


def _reset_paths() -> list[Path]:
    return [
        SETTINGS_PATH,
        SETTINGS_PATH.with_name(f".{SETTINGS_PATH.name}.lock"),
        KEYS_PATH,
        KEYS_PATH.with_name(f".{KEYS_PATH.name}.lock"),
        *_tm_paths(),
        TASK_HISTORY_PATH,
        TASK_HISTORY_PATH.with_suffix(".tmp"),
        *_log_paths(),
        diagnostics.DIAGNOSTICS_DIR,
        WORKSPACES_DIR,
        API_HEALTH_STATE_PATH,
        # Read late: tests point the settings module at a temporary directory.
        settings_module.RECOVERY_PATH,
        settings_module.RECOVERY_PATH.with_name(
            f".{settings_module.RECOVERY_PATH.name}.lock"
        ),
    ]


def _remove_owned_path(path: Path) -> bool:
    if not path.exists() and not path.is_symlink():
        return False
    try:
        path.resolve().relative_to(APP_DATA_DIR.resolve())
    except (OSError, ValueError) as exc:
        raise MaintenanceError("维护操作拒绝删除应用数据目录外的文件。") from exc
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)
    return True


def _path_size(path: Path) -> int:
    try:
        if path.is_file() or path.is_symlink():
            return path.stat().st_size
        if path.is_dir():
            return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
    except OSError:
        return 0
    return 0


def _path_count(path: Path) -> int:
    try:
        if path.is_file() or path.is_symlink():
            return 1
        if path.is_dir():
            return sum(1 for item in path.rglob("*") if item.is_file())
    except OSError:
        return 0
    return 0


def _key_count() -> int:
    try:
        from settings import load_keys

        return len(load_keys())
    except Exception:
        return 0


def _tm_entry_count() -> int:
    try:
        return tm_manager.count_entries()
    except Exception:
        return 0
