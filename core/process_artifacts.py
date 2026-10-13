"""Owned, stable task work directories outside user deliverable folders.

The per-run metadata is the index: classification changes never move the work
directory, so checkpoint and diagnostic links remain valid. Unknown directories
and interrupted/resumable runs are deliberately excluded from automatic pruning.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from config import (
    APP_DATA_DIR,
    PROCESS_RETENTION_FAILED_RUNS_DEFAULT,
    PROCESS_RETENTION_MB_DEFAULT,
    PROCESS_RETENTION_SUCCEEDED_RUNS_DEFAULT,
)

# 两类的默认上限；用户在「设置 → 数据与维护」里改过就以 settings.process_retention 为准
# （见 _retention_rules）。normal = 正常完成，errors = 需复核或出错。
NORMAL_RETENTION = PROCESS_RETENTION_SUCCEEDED_RUNS_DEFAULT
ERROR_RETENTION = PROCESS_RETENTION_FAILED_RUNS_DEFAULT
RETENTION_BYTES = PROCESS_RETENTION_MB_DEFAULT * 1024 * 1024
METADATA_FILENAME = ".process-run.json"
_OWNER = "xl-translator-process-artifacts"
_SCHEMA_VERSION = 1
_THREAD_LOCK = threading.RLock()
_FINISHED_STATUSES = {"succeeded", "failed", "needs_review", "cancelled", "paused", "interrupted"}
_INTERRUPTED_STATUSES = {"cancelled", "paused", "interrupted"}


def _process_root(*, create: bool) -> Path:
    # Resolve the application data base first (macOS temporary directories may
    # have a system symlink ancestor), but never follow links inside our store.
    base = Path(APP_DATA_DIR).expanduser().resolve()
    root = base / "logs" / "process"
    for directory in (base / "logs", root):
        if directory.is_symlink():
            raise OSError(f"Process artifact directory cannot be a symlink: {directory}")
        if create:
            directory.mkdir(parents=True, exist_ok=True)
    return root


@contextmanager
def _store_lock(root: Path) -> Iterator[None]:
    """Serialize writers in threads and processes; OS releases locks on crash."""
    with _THREAD_LOCK:
        lock_path = root / ".store.lock"
        if lock_path.is_symlink():
            raise OSError("Process artifact lock cannot be a symlink")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(lock_path, flags, 0o600)
        try:
            if os.name == "nt":
                import msvcrt

                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _output_key(output_dir: Path) -> str:
    return os.path.normcase(str(Path(output_dir).expanduser().resolve()))


def _read_metadata(directory: Path) -> dict | None:
    marker = directory / METADATA_FILENAME
    try:
        if directory.is_symlink() or not directory.is_dir() or marker.is_symlink():
            return None
        data = json.loads(marker.read_text(encoding="utf-8"))
        if (
            not isinstance(data, dict)
            or data.get("owner") != _OWNER
            or data.get("schema_version") != _SCHEMA_VERSION
            or data.get("directory") != directory.name
            or not isinstance(data.get("output_dir"), str)
            or not isinstance(data.get("kind"), str)
            or not isinstance(data.get("created_ns"), int)
            or data.get("category") not in {"active", "normal", "errors"}
        ):
            return None
        return data
    except (OSError, ValueError, TypeError):
        return None


def _write_metadata(directory: Path, data: dict) -> None:
    temporary = directory / f".process-run-{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / METADATA_FILENAME)
    finally:
        # Use the OS primitive here rather than Path.unlink().  Translation
        # tests and integrations may intentionally inject cleanup failures on
        # user-facing paths; metadata publication must remain atomic and its
        # best-effort temporary cleanup must not turn that into a task failure.
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _indexed_runs(root: Path) -> list[tuple[Path, dict]]:
    result = []
    for directory in root.iterdir():
        metadata = _read_metadata(directory)
        if metadata is not None:
            result.append((directory, metadata))
    return sorted(result, key=lambda item: (item[1]["created_ns"], item[0].name), reverse=True)


def _has_resume_data(directory: Path) -> bool:
    """Conservatively preserve checkpoints, including formats added later."""
    for parent, directories, files in os.walk(directory, followlinks=False):
        directories[:] = [name for name in directories if not (Path(parent) / name).is_symlink()]
        for name in files:
            lower = name.lower()
            if "checkpoint" in lower or lower in {"pdf_translation_manifest.json", ".resumable"}:
                return True
    return False


def _safe_to_prune(directory: Path, metadata: dict, root: Path) -> bool:
    if directory.parent != root or directory.is_symlink() or _read_metadata(directory) != metadata:
        return False
    if metadata.get("status") not in _FINISHED_STATUSES or metadata.get("category") == "active":
        return False
    if metadata.get("resumable") or metadata.get("status") in _INTERRUPTED_STATUSES:
        return False
    if metadata["category"] == "errors" and _has_resume_data(directory):
        return False
    # Do not recurse into or unlink user-inserted links, even within owned runs.
    for parent, directories, files in os.walk(directory, followlinks=False):
        if any((Path(parent) / name).is_symlink() for name in directories + files):
            return False
    return True


def _retention_limit(value: int, default: int) -> int:
    try:
        return max(1, min(1000, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


def _retention_bytes(value: int, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _configured_retention() -> dict[str, tuple[int, int]]:
    """用户在设置里定的上限；读不到设置时返回空表，调用方退回模块默认值。"""
    try:
        # 延迟导入：settings 依赖链很长，这个模块被各个 runner 在导入期引用。
        from settings import load_settings

        retention = load_settings().process_retention
    except Exception:  # noqa: BLE001 - fall back to defaults, never block a task
        return {}
    return {
        category: (rule.max_runs, rule.max_mb * 1024 * 1024)
        for category, rule in (("normal", retention.succeeded), ("errors", retention.failed))
    }


def _retention_rules() -> dict[str, tuple[int, int]]:
    """每类的 (最多几次, 最多多少字节)。"""
    configured = _configured_retention()
    rules = {}
    for category, default_runs in (("normal", NORMAL_RETENTION), ("errors", ERROR_RETENTION)):
        runs, size = configured.get(category, (default_runs, RETENTION_BYTES))
        rules[category] = (
            _retention_limit(runs, 5 if category == "normal" else 30),
            _retention_bytes(size, PROCESS_RETENTION_MB_DEFAULT * 1024 * 1024),
        )
    return rules


def _directory_size(directory: Path) -> int:
    total = 0
    for parent, directories, files in os.walk(directory, followlinks=False):
        directories[:] = [name for name in directories if not (Path(parent) / name).is_symlink()]
        for name in files:
            try:
                total += (Path(parent) / name).lstat().st_size
            except OSError:
                continue
    return total


def _prunable_runs(root: Path, category: str) -> list[tuple[Path, dict, int]]:
    """可自动清理的已结束任务，最近结束的在前，附带各自的体积。

    暂停、停止、可续译和含断点的出错任务不在其中：它们既不清理，也不占这里的上限，
    否则几份留着续译的大 PDF 就能把其余记录全部挤掉。
    """
    completed = [
        (directory, metadata, _directory_size(directory))
        for directory, metadata in _indexed_runs(root)
        if metadata["category"] == category and _safe_to_prune(directory, metadata, root)
    ]
    completed.sort(key=lambda item: (item[1].get("finished_ns", 0), item[0].name), reverse=True)
    return completed


def _prune(root: Path) -> int:
    """按条数和体积两道上限清理；最近一次始终保留。返回删掉的目录数。"""
    removed = 0
    for category, (max_runs, max_bytes) in _retention_rules().items():
        kept_bytes = 0
        over = False
        for index, (directory, metadata, size) in enumerate(_prunable_runs(root, category)):
            # 一旦某一次超出上限，比它更早的全部清掉：「从最早的开始清理」，
            # 不会跳过一份大的、反而留下更早的几份小的。
            if not over and (index == 0 or (index < max_runs and kept_bytes + size <= max_bytes)):
                kept_bytes += size
                continue
            over = True
            # Recheck ownership/status immediately before deletion. Failed
            # cleanup leaves data in place and never fails a translation.
            try:
                if _safe_to_prune(directory, metadata, root):
                    shutil.rmtree(directory)
                    removed += 1
            except OSError:
                continue
    return removed


def prune_now() -> int:
    """保留上限改小后立即按新上限清理一次（设置保存时调用）。"""
    try:
        root = _process_root(create=False)
        if not root.is_dir():
            return 0
        with _store_lock(root):
            return _prune(root)
    except OSError:
        return 0


def retention_overview() -> dict[str, dict[str, int]]:
    """维护页「自动保留上限」用：每类当前可清理的次数、体积和生效中的上限。"""
    rules = _retention_rules()
    usage = {category: {"count": 0, "size_bytes": 0} for category in rules}
    try:
        root = _process_root(create=False)
        if root.is_dir():
            for category in rules:
                runs = _prunable_runs(root, category)
                usage[category] = {"count": len(runs), "size_bytes": sum(size for *_, size in runs)}
    except OSError:
        pass
    return {
        category: {**usage[category], "max_runs": rules[category][0], "max_bytes": rules[category][1]}
        for category in rules
    }


@dataclass(frozen=True)
class ProcessArtifacts:
    work_dir: Path
    _root: Path

    @classmethod
    def create(cls, kind: str, output_dir: Path, run_id: str | None = None) -> ProcessArtifacts:
        """Create a new active run without creating/changing the output folder."""
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", kind):
            raise ValueError("Process artifact kind must be a short path-safe identifier")
        root = _process_root(create=True)
        with _store_lock(root):
            created_ns = time.time_ns()
            directory = root / f"{created_ns}-{kind}-{uuid.uuid4().hex}"
            directory.mkdir(mode=0o700)
            _write_metadata(directory, {
                "owner": _OWNER,
                "schema_version": _SCHEMA_VERSION,
                "directory": directory.name,
                "run_id": str(run_id) if run_id is not None else uuid.uuid4().hex,
                "kind": kind,
                "output_dir": _output_key(output_dir),
                "created_ns": created_ns,
                "status": "active",
                "category": "active",
                "resumable": False,
            })
        return cls(directory, root)

    def finish(self, status: str) -> None:
        """Classify a completed run in place, then prune safe old completed runs.

        Paused/cancelled/interrupted runs and failed runs holding checkpoints
        remain available indefinitely until a successful task supersedes them.
        """
        if status not in _FINISHED_STATUSES:
            raise ValueError(f"Unsupported process artifact status: {status}")
        if _process_root(create=False) != self._root:
            raise OSError("Application data directory changed while a task was running")
        with _store_lock(self._root):
            metadata = _read_metadata(self.work_dir)
            if metadata is None:
                raise OSError("Process artifact ownership metadata is missing or invalid")
            if metadata["category"] != "active":
                if metadata["status"] == status:
                    return
                raise ValueError("Process artifact run has already finished")
            metadata.update(
                status=status,
                category="normal" if status == "succeeded" else "errors",
                finished_ns=time.time_ns(),
                resumable=status in _INTERRUPTED_STATUSES or (
                    status != "succeeded" and _has_resume_data(self.work_dir)
                ),
            )
            _write_metadata(self.work_dir, metadata)
            _prune(self._root)


def find_process_dirs(output_dir: Path, kind: str | None = None) -> list[Path]:
    """Find owned runs for this exact output folder, newest first (read only)."""
    try:
        root = _process_root(create=False)
        if not root.is_dir():
            return []
        key = _output_key(output_dir)
        # Atomic metadata replaces make reads consistent without writing a lock
        # file during a read-only scan. Concurrent deletion is an ordinary miss.
        return [
            directory for directory, metadata in _indexed_runs(root)
            if metadata["output_dir"] == key and (kind is None or metadata["kind"] == kind)
        ]
    except OSError:
        return []
