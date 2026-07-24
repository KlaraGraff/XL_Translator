"""Maintenance operations bounded to the current Translator data directory."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import APP_DATA_DIR, KEYS_PATH, LOG_PATH, SETTINGS_PATH
from core import diagnostics, tm_manager
from core.task_history import TaskHistoryStore
from core.task_logger import clear_log_files
from settings import AppSettings, get_settings_schema_status, save_settings


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
    tm_manager.init_db()
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
    from settings import load_settings

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
    return [base, base.with_name(f"{base.name}-wal"), base.with_name(f"{base.name}-shm")]


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
