"""First-run migration from the legacy local data directory."""

from __future__ import annotations

import json
import platform
import shutil
import sqlite3
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from config import APP_DATA_DIR
from core.app_paths import get_legacy_app_data_dir, get_legacy_launcher_data_dir

MIGRATION_MARKER_NAME = "migration.json"
MIGRATION_SCHEMA_VERSION = 1

PRIMARY_FILES = {
    "settings.json": "用户设置",
    "keys.json": "API Key",
    "tm.db": "翻译记忆库",
}

SUPPORT_FILE_NAMES = {
    "app.log": "应用日志",
    "desktop_launcher.log": "桌面启动日志",
    "desktop_instance.json": "桌面实例状态",
}

SUPPORT_DIR_NAMES = {
    "diagnostics": "诊断归档",
}

ProgressCallback = Callable[[int, int, str], None]


@dataclass(frozen=True)
class MigrationItem:
    label: str
    source: Path
    target: Path
    kind: str


@dataclass(frozen=True)
class DataMigrationPlan:
    status: str
    app_data_dir: Path
    legacy_data_dir: Path
    legacy_launcher_dir: Path
    marker_path: Path
    primary_items: tuple[MigrationItem, ...]
    support_items: tuple[MigrationItem, ...]
    conflicts: tuple[Path, ...] = ()

    @property
    def has_prompt(self) -> bool:
        return self.status in {"ready", "conflict"}

    @property
    def primary_size_bytes(self) -> int:
        return sum(_estimate_path_size(item.source) for item in self.primary_items)

    @property
    def support_size_bytes(self) -> int:
        return sum(_estimate_path_size(item.source) for item in self.support_items)


@dataclass(frozen=True)
class MigrationResult:
    migrated: tuple[MigrationItem, ...]
    skipped: tuple[MigrationItem, ...]
    marker_path: Path


def inspect_data_migration(
    *,
    app_data_dir: Path = APP_DATA_DIR,
    legacy_data_dir: Path | None = None,
    legacy_launcher_dir: Path | None = None,
) -> DataMigrationPlan:
    """Build a migration plan for the current process."""
    legacy_data_dir = legacy_data_dir or get_legacy_app_data_dir()
    legacy_launcher_dir = legacy_launcher_dir or get_legacy_launcher_data_dir()
    marker_path = app_data_dir / MIGRATION_MARKER_NAME

    primary_items = tuple(_iter_primary_items(legacy_data_dir, app_data_dir))
    support_items = tuple(
        _iter_support_items(legacy_data_dir, legacy_launcher_dir, app_data_dir)
    )

    if marker_path.exists() or not primary_items:
        return DataMigrationPlan(
            status="none",
            app_data_dir=app_data_dir,
            legacy_data_dir=legacy_data_dir,
            legacy_launcher_dir=legacy_launcher_dir,
            marker_path=marker_path,
            primary_items=primary_items,
            support_items=support_items,
        )

    conflicts = tuple(item.target for item in primary_items if item.target.exists())
    status = "conflict" if conflicts else "ready"
    return DataMigrationPlan(
        status=status,
        app_data_dir=app_data_dir,
        legacy_data_dir=legacy_data_dir,
        legacy_launcher_dir=legacy_launcher_dir,
        marker_path=marker_path,
        primary_items=primary_items,
        support_items=support_items,
        conflicts=conflicts,
    )


def migrate_legacy_data(
    plan: DataMigrationPlan,
    *,
    include_support_files: bool = False,
    progress: ProgressCallback | None = None,
) -> MigrationResult:
    """Migrate legacy primary data and optionally preserve support artifacts."""
    if plan.status != "ready":
        raise ValueError(f"当前迁移计划不可自动执行：status={plan.status}")

    items = list(plan.primary_items)
    if include_support_files:
        items.extend(plan.support_items)

    total = max(len(items) + 2, 1)
    migrated: list[MigrationItem] = []
    skipped: list[MigrationItem] = []

    _emit_progress(progress, 0, total, "准备新数据目录")
    plan.app_data_dir.mkdir(parents=True, exist_ok=True)

    step = 1
    for item in items:
        _emit_progress(progress, step, total, f"迁移{item.label}")
        if not item.source.exists():
            skipped.append(item)
        elif item.kind == "sqlite":
            _copy_sqlite_database(item.source, item.target)
            migrated.append(item)
        elif item.kind == "directory":
            _copy_directory(item.source, item.target)
            migrated.append(item)
        else:
            _copy_file(item.source, item.target)
            migrated.append(item)
        step += 1

    _emit_progress(progress, step, total, "写入迁移记录")
    _write_marker(
        plan.marker_path,
        status="migrated",
        legacy_data_dir=plan.legacy_data_dir,
        include_support_files=include_support_files,
        migrated_items=migrated,
        skipped_items=skipped,
    )
    _emit_progress(progress, total, total, "迁移完成")
    return MigrationResult(tuple(migrated), tuple(skipped), plan.marker_path)


def mark_migration_skipped(
    plan: DataMigrationPlan,
    *,
    reason: str = "user_skipped",
) -> Path:
    """Record that the user chose not to migrate legacy data."""
    plan.app_data_dir.mkdir(parents=True, exist_ok=True)
    _write_marker(
        plan.marker_path,
        status="skipped",
        legacy_data_dir=plan.legacy_data_dir,
        include_support_files=False,
        migrated_items=[],
        skipped_items=[*plan.primary_items, *plan.support_items],
        reason=reason,
    )
    return plan.marker_path


def format_size(num_bytes: int) -> str:
    value = float(max(0, int(num_bytes or 0)))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _iter_primary_items(legacy_data_dir: Path, app_data_dir: Path) -> list[MigrationItem]:
    items: list[MigrationItem] = []
    for filename, label in PRIMARY_FILES.items():
        source = legacy_data_dir / filename
        if source.exists():
            kind = "sqlite" if filename == "tm.db" else "file"
            items.append(MigrationItem(label, source, app_data_dir / filename, kind))
    return items


def _iter_support_items(
    legacy_data_dir: Path,
    legacy_launcher_dir: Path,
    app_data_dir: Path,
) -> list[MigrationItem]:
    items: list[MigrationItem] = []
    support_root = app_data_dir / "legacy_support"

    for filename, label in SUPPORT_FILE_NAMES.items():
        source = legacy_data_dir / filename
        if source.exists():
            items.append(
                MigrationItem(
                    label,
                    source,
                    support_root / legacy_data_dir.name / filename,
                    "file",
                )
            )

        launcher_source = legacy_launcher_dir / filename
        if legacy_launcher_dir != legacy_data_dir and launcher_source.exists():
            items.append(
                MigrationItem(
                    label,
                    launcher_source,
                    support_root / legacy_launcher_dir.name / filename,
                    "file",
                )
            )

    for log_path in sorted(legacy_data_dir.glob("app.log.*")):
        if log_path.is_file():
            items.append(
                MigrationItem(
                    f"应用日志 {log_path.name}",
                    log_path,
                    support_root / legacy_data_dir.name / log_path.name,
                    "file",
                )
            )

    for dirname, label in SUPPORT_DIR_NAMES.items():
        source = legacy_data_dir / dirname
        if source.exists() and source.is_dir():
            items.append(
                MigrationItem(
                    label,
                    source,
                    support_root / legacy_data_dir.name / dirname,
                    "directory",
                )
            )

    for zip_path in sorted(legacy_data_dir.glob("*.zip")):
        if zip_path.is_file():
            items.append(
                MigrationItem(
                    f"诊断压缩包 {zip_path.name}",
                    zip_path,
                    support_root / legacy_data_dir.name / zip_path.name,
                    "file",
                )
            )

    return _dedupe_items(items)


def _dedupe_items(items: list[MigrationItem]) -> list[MigrationItem]:
    seen: set[tuple[Path, Path]] = set()
    deduped: list[MigrationItem] = []
    for item in items:
        key = (item.source, item.target)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _copy_sqlite_database(source: Path, target: Path) -> None:
    if target.exists():
        raise FileExistsError(f"目标数据库已存在：{target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with (
        sqlite3.connect(str(source)) as source_conn,
        sqlite3.connect(str(target)) as target_conn,
    ):
        source_conn.backup(target_conn)


def _copy_file(source: Path, target: Path) -> None:
    target = _unique_target(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    if source.name == "keys.json":
        _restrict_user_permissions(target)


def _copy_directory(source: Path, target: Path) -> None:
    target = _unique_target(target)
    shutil.copytree(source, target)


def _unique_target(target: Path) -> Path:
    if not target.exists():
        return target

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if target.suffix:
        candidate = target.with_name(f"{target.stem}_legacy_{timestamp}{target.suffix}")
    else:
        candidate = target.with_name(f"{target.name}_legacy_{timestamp}")
    if not candidate.exists():
        return candidate

    index = 2
    while True:
        if target.suffix:
            candidate = target.with_name(
                f"{target.stem}_legacy_{timestamp}_{index}{target.suffix}"
            )
        else:
            candidate = target.with_name(f"{target.name}_legacy_{timestamp}_{index}")
        if not candidate.exists():
            return candidate
        index += 1


def _restrict_user_permissions(path: Path) -> None:
    if platform.system() not in {"Darwin", "Linux"}:
        return
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        return


def _write_marker(
    marker_path: Path,
    *,
    status: str,
    legacy_data_dir: Path,
    include_support_files: bool,
    migrated_items: list[MigrationItem],
    skipped_items: list[MigrationItem],
    reason: str = "",
) -> None:
    payload = {
        "schema_version": MIGRATION_SCHEMA_VERSION,
        "status": status,
        "reason": reason,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "legacy_data_dir": str(legacy_data_dir),
        "include_support_files": include_support_files,
        "migrated_items": [_item_payload(item) for item in migrated_items],
        "skipped_items": [_item_payload(item) for item in skipped_items],
    }
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _item_payload(item: MigrationItem) -> dict[str, str]:
    return {
        "label": item.label,
        "source": str(item.source),
        "target": str(item.target),
        "kind": item.kind,
    }


def _estimate_path_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _emit_progress(
    progress: ProgressCallback | None,
    current: int,
    total: int,
    message: str,
) -> None:
    if progress is not None:
        progress(current, total, message)
