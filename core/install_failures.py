"""组件一键安装失败的记录：失败原因写进诊断目录，反馈包和「导出诊断」带上它。

界面上只显示一句「组件下载失败，请检查网络连接。」之类的话，真正的原因（哪个下载源、
什么错误、试运行输出了什么）只写进了日志。这里在安装线程里收集这段时间的警告和错误，
安装失败时连同错误链一起存下最近几次，排查用户反馈时能看到。

只记组件名、阶段、进度、错误文字和下载地址；本机路径换成 [path]，密钥打码。
"""
from __future__ import annotations

import json
import platform
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from app_meta import APP_VERSION
from core import diagnostics
from core.task_logger import _API_SECRET_RE, redact_absolute_paths

FILENAME = "component_install_failures.json"
MAX_RECORDS = 10
_MAX_NOTES = 40
_MAX_TEXT = 600
_MAX_CAUSES = 6
_WRITE_LOCK = threading.Lock()


def failures_path() -> Path:
    # 现取：测试会把诊断目录换到临时目录。
    return Path(diagnostics.DIAGNOSTICS_DIR) / FILENAME


def _clean(text: Any) -> str:
    value = _API_SECRET_RE.sub("[redacted]", str(text or ""))
    value = redact_absolute_paths(value)
    value = diagnostics._redact(value)
    value = " ".join(value.split())
    return value[:_MAX_TEXT]


class InstallNotes:
    """安装线程里记下的警告和错误（按时间先后，最多 _MAX_NOTES 条）。"""

    def __init__(self) -> None:
        self.items: list[str] = []
        self.dropped = 0

    def add(self, level: str, message: str, exception: Any = None) -> None:
        text = f"{level}: {_clean(message)}"
        if exception is not None and getattr(exception, "value", None) is not None:
            text += f" | {_clean(_exception_text(exception.value))}"
        if len(self.items) < _MAX_NOTES:
            self.items.append(text[:_MAX_TEXT])
        else:
            self.dropped += 1


@contextmanager
def collect_notes() -> Iterator[InstallNotes]:
    """在当前线程里收集日志里的警告和错误，供安装失败时一起存下。"""
    notes = InstallNotes()
    thread_id = threading.get_ident()

    def sink(message: Any) -> None:
        record = message.record
        notes.add(record["level"].name, record["message"], record["exception"])

    handler = logger.add(sink, level="WARNING", format="{message}",
                         filter=lambda record: record["thread"].id == thread_id, catch=True)
    try:
        yield notes
    finally:
        logger.remove(handler)


def _exception_text(exc: BaseException) -> str:
    text = str(exc)
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _cause_chain(exc: BaseException | None) -> list[str]:
    chain: list[str] = []
    seen: set[int] = set()
    current = exc
    while current is not None and id(current) not in seen and len(chain) < _MAX_CAUSES:
        seen.add(id(current))
        chain.append(_clean(_exception_text(current)))
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    return chain


def _where(exc: BaseException | None) -> list[str]:
    """出错位置：最后几层调用的模块名、行号、函数名（不含本机路径）。"""
    frames: list[str] = []
    tb = exc.__traceback__ if exc is not None else None
    while tb is not None:
        code = tb.tb_frame.f_code
        frames.append(f"{Path(code.co_filename).name}:{tb.tb_lineno} {code.co_name}")
        tb = tb.tb_next
    return frames[-5:]


def record_failure(component: str, *, message: str, exc: BaseException | None, progress: dict[str, Any],
                   notes: InstallNotes | None = None) -> None:
    """存下一次安装失败；写不进去也不影响界面显示失败。"""
    entry: dict[str, Any] = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "component": _clean(component),
        "app_version": APP_VERSION,
        "os": f"{platform.system()} {platform.release()}",
        "machine": platform.machine(),
        "phase": _clean(progress.get("phase")),
        "done": int(progress.get("done") or 0),
        "total": int(progress.get("total") or 0),
        "message": _clean(message),
        "errors": _cause_chain(exc),
        "where": _where(exc),
        "log": list(notes.items) if notes else [],
    }
    if notes and notes.dropped:
        entry["log_dropped"] = notes.dropped
    try:
        with _WRITE_LOCK:
            records = [entry, *recent_failures()][:MAX_RECORDS]
            path = failures_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_name(f"{path.name}.{time.time_ns()}.tmp")
            temp.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(path)
    except OSError as error:
        logger.warning(f"[组件安装] 无法保存安装失败记录：{error!r}")


def recent_failures() -> list[dict[str, Any]]:
    """最近的安装失败记录，新的在前；没有或读不出时是空列表。"""
    try:
        data = json.loads(failures_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [item for item in data if isinstance(item, dict)][:MAX_RECORDS] if isinstance(data, list) else []


__all__ = ["FILENAME", "MAX_RECORDS", "InstallNotes", "collect_notes", "failures_path", "recent_failures",
           "record_failure"]
