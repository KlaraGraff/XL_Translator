"""插件页「一键安装」的后台任务：一个组件同时只装一份，进度给界面轮询。"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from loguru import logger

InstallFn = Callable[..., Any]


class InstallJob:
    def __init__(self, name: str, install: Callable[[], InstallFn], error_type: Callable[[], type[Exception]],
                 on_success: Callable[[], None] | None = None) -> None:
        # install / error_type 取的是函数：测试会替换安装模块里的函数，这里每次现取。
        self._name = name
        self._install = install
        self._error_type = error_type
        self._on_success = on_success
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._state: dict[str, Any] = {"running": False, "phase": "", "done": 0, "total": 0, "error": "",
                                       "cancelled": False, "finished": 0.0}

    def progress(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def running(self) -> bool:
        return bool(self.progress().get("running"))

    def _set(self, **values: Any) -> None:
        with self._lock:
            self._state.update(values)

    def start(self, total: int) -> dict[str, Any]:
        """后台开始安装；已在安装时不重复开始。"""
        with self._lock:
            if self._state["running"]:
                return dict(self._state)
            self._cancel.clear()
            self._state.update(running=True, phase="download", done=0, total=total, error="", cancelled=False,
                               finished=0.0)
        threading.Thread(target=self._run, name=f"{self._name}-install", daemon=True).start()
        return self.progress()

    def cancel(self) -> dict[str, Any]:
        self._cancel.set()
        return self.progress()

    def _run(self) -> None:
        def progress(phase: str, done: int, total: int) -> None:
            self._set(phase=phase, done=done, total=total)

        try:
            self._install()(progress=progress, cancel=self._cancel)
            if self._on_success is not None:
                self._on_success()
            self._set(running=False, phase="done", error="", finished=time.time())
        except self._error_type() as exc:
            self._set(running=False, phase="failed", error=str(exc), cancelled=self._cancel.is_set(),
                      finished=time.time())
        except Exception:  # noqa: BLE001 - every failure becomes a message on the page
            logger.exception(f"[{self._name}] 一键安装失败")
            self._set(running=False, phase="failed", error="组件安装失败。", cancelled=False, finished=time.time())
