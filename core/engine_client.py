"""Reach the translation engine from outside the app: attach, or start one.

The command line (``cli``) and the MCP server talk to the same loopback HTTP
API the app's window uses, so a task they start is an ordinary task: it shows
up in the task center with its progress, can be stopped there, and lands in
the history.  ``connect`` first tries the engine the running app published in
``engine.json`` (``core.engine_file``).  When there is none it starts one in
the background, tied to the caller's process through
``TRANSLATOR_SIDECAR_PARENT_PID`` (the engine's parent watchdog), so the
engine exits when the command or MCP session that needed it does.

Nothing here knows any translation type: a type registered at run time is
used exactly like a built-in one, through its descriptor (``surfaces()``).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import psutil

from core.app_paths import get_app_data_dir
from core.engine_file import RUN_DIR_NAME, read_engine_file

#: Same limits as the desktop shell (``SIDECAR_START_TIMEOUT`` and
#: ``SIDECAR_HEALTH_TIMEOUT`` in src-tauri/src/main.rs): a first start that a
#: virus scanner holds up can take a while, a started engine answers quickly.
START_TIMEOUT_SECONDS = 30.0
HEALTH_TIMEOUT_SECONDS = 8.0
#: An attach attempt only asks an engine that is already up, so a short wait.
ATTACH_TIMEOUT_SECONDS = 2.0
#: How long ``close`` lets an engine it started wind down before killing it.
#: Covers the engine's own worst case (drain 10 s + task unwind 12 s + 3 s).
STOP_TIMEOUT_SECONDS = 25.0
#: The event stream sends a keepalive every 15 s; anything much longer is a
#: dead connection.
EVENT_READ_TIMEOUT_SECONDS = 60.0
EVENT_RECONNECT_ATTEMPTS = 5
#: Engine output of a background start, kept for the error message when the
#: start fails (``<app_data>/run/cli-engine.log``, rewritten on every start).
SPAWN_LOG_NAME = "cli-engine.log"

TERMINAL_EVENT_TYPES = frozenset({"done", "completed_with_issues", "error", "stopped"})

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CREATE_NO_WINDOW = 0x0800_0000


class EngineUnavailable(RuntimeError):
    """No engine is running and none could be started."""


class EngineError(RuntimeError):
    """The engine answered, but refused the request.

    ``detail`` is the engine's own user-facing sentence; ``reason`` is the
    stable code next to it (``surface_busy``, ``confirmation_required`` …).
    """

    def __init__(self, status: int, detail: str, *, reason: str = "", payload: Any = None) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.reason = reason
        self.payload = payload


@dataclass(frozen=True)
class TaskEvent:
    """One event of a task's stream (``GET /api/tasks/{id}/events``)."""

    id: int
    type: str
    data: Any

    @property
    def terminal(self) -> bool:
        return self.type in TERMINAL_EVENT_TYPES


def parse_handshake(line: str) -> tuple[int, str]:
    """``PORT=… TOKEN=…`` as the engine prints it on stdout.

    Mirrors ``parse_handshake`` in src-tauri/src/main.rs: whitespace-separated
    segments in any order, both required, the token non-empty.
    """
    port: int | None = None
    token: str | None = None
    for segment in str(line or "").split():
        if segment.startswith("PORT="):
            try:
                value = int(segment[len("PORT="):])
            except ValueError:
                value = -1
            port = value if 0 < value < 65536 else None
        if segment.startswith("TOKEN="):
            token = segment[len("TOKEN="):]
    if port is None or not token:
        raise EngineUnavailable("翻译引擎返回的连接信息无效。")
    return port, token


class EngineClient:
    """A connection to one engine.  Use ``connect`` to get one."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        process: subprocess.Popen[Any] | None = None,
        pid: int | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._process = process
        self.pid = process.pid if process is not None else pid
        self._http = httpx.Client(
            base_url=self.base_url,
            headers={"X-Translator-Token": token},
            timeout=httpx.Timeout(30.0, connect=5.0),
            # Loopback only; an HTTP(S)_PROXY from the environment must not
            # route these requests (or the token) anywhere else.
            trust_env=False,
        )

    @property
    def started_here(self) -> bool:
        """This client started the engine, so the engine ends with this process."""
        return self._process is not None

    # ── requests ─────────────────────────────────────────────────────────

    def _request(self, method: str, path: str, *, json_body: Any = None, timeout: float | None = None) -> Any:
        try:
            response = self._http.request(
                method,
                path,
                json=json_body,
                timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
            )
        except httpx.TransportError as exc:
            raise EngineUnavailable(f"与翻译引擎的连接中断：{exc}") from exc
        if response.status_code >= 400:
            raise _engine_error(response)
        if not response.content:
            return None
        return response.json()

    def health(self, *, timeout: float = ATTACH_TIMEOUT_SECONDS) -> bool:
        try:
            response = self._http.get("/health", timeout=timeout)
        except httpx.TransportError:
            return False
        if response.status_code != 200:
            return False
        try:
            return bool(response.json().get("ok"))
        except ValueError:
            return False

    def surfaces(self) -> list[dict[str, Any]]:
        """Type descriptors, in sidebar order (``GET /api/surfaces``)."""
        return list((self._request("GET", "/api/surfaces") or {}).get("surfaces") or [])

    def scan(self, surface: str, paths: list[str], **fields: Any) -> dict[str, Any]:
        """What a type would translate under ``paths`` (``POST /api/sources/scan``)."""
        clean = [str(path) for path in paths if str(path).strip()]
        if not clean:
            raise ValueError("scan needs at least one path")
        body = {"surface": surface, "path": clean[0], "paths": clean, **fields}
        return self._request("POST", "/api/sources/scan", json_body=body, timeout=300.0)

    def preflight(self, request: dict[str, Any]) -> dict[str, Any]:
        """Check a start request; may ask for confirmation (``POST /api/tasks/preflight``)."""
        return self._request("POST", "/api/tasks/preflight", json_body=request, timeout=120.0)

    def start(self, request: dict[str, Any]) -> dict[str, Any]:
        """Start a task; returns its status with ``task_id`` (``POST /api/tasks``)."""
        return self._request("POST", "/api/tasks", json_body=request, timeout=120.0)

    def tasks(self) -> dict[str, Any]:
        """Running tasks and recent history, without logs (``GET /api/tasks``)."""
        return self._request("GET", "/api/tasks")

    def task(self, task_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/tasks/{_task_path(task_id)}")

    def results(self, task_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/tasks/{_task_path(task_id)}/results")

    def stop(self, task_id: str) -> dict[str, Any]:
        return self._request("POST", f"/api/tasks/{_task_path(task_id)}/stop")

    def events(self, task_id: str, *, after: int = 0) -> Iterator[TaskEvent]:
        """Yield the task's events from ``after`` on, until it has finished.

        The stream replays what the task already emitted, so a reader that
        joins late still sees the terminal event.  A dropped connection is
        resumed from the last event received (``Last-Event-ID``).
        """
        last_id = max(0, int(after or 0))
        failures = 0
        path = f"/api/tasks/{_task_path(task_id)}/events"
        while True:
            saw_terminal = False
            try:
                with self._http.stream(
                    "GET",
                    path,
                    headers={"Last-Event-ID": str(last_id)},
                    timeout=httpx.Timeout(EVENT_READ_TIMEOUT_SECONDS, connect=5.0),
                ) as response:
                    if response.status_code >= 400:
                        response.read()
                        raise _engine_error(response)
                    for event in iter_sse(response.iter_lines()):
                        failures = 0
                        last_id = max(last_id, event.id)
                        saw_terminal = saw_terminal or event.terminal
                        yield event
            except httpx.TransportError as exc:
                failures += 1
                if failures > EVENT_RECONNECT_ATTEMPTS:
                    raise EngineUnavailable(f"与翻译引擎的连接中断：{exc}") from exc
                time.sleep(min(4.0, 0.5 * 2 ** (failures - 1)))
                continue
            if saw_terminal:
                return
            # The engine ends a stream at a task's end, and also when it is
            # shutting down.  Only the first is a finished task.
            if self.task(task_id).get("terminal"):
                return
            failures += 1
            if failures > EVENT_RECONNECT_ATTEMPTS:
                raise EngineUnavailable("翻译引擎停止了事件推送，任务尚未结束。")
            time.sleep(min(4.0, 0.5 * 2 ** (failures - 1)))

    # ── lifetime ─────────────────────────────────────────────────────────

    def close(self, *, timeout: float = STOP_TIMEOUT_SECONDS) -> None:
        """Release the connection; an engine started here is shut down too.

        Its parent watchdog would end it anyway once this process exits.
        Asking first lets running tasks unwind (temporary folders, history)
        the same way they do when the app quits.
        """
        process = self._process
        self._process = None
        try:
            if process is not None and process.poll() is None:
                try:
                    self._http.post("/api/system/shutdown", timeout=2.0)
                except httpx.TransportError:
                    pass
                try:
                    process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        finally:
            self._http.close()

    def __enter__(self) -> EngineClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def iter_sse(lines: Iterator[str]) -> Iterator[TaskEvent]:
    """Parse ``text/event-stream`` lines into events; comments are skipped."""
    event_id: int | None = None
    event_type = "message"
    data_lines: list[str] = []
    for raw in lines:
        line = raw.rstrip("\r")
        if not line:
            if data_lines and event_id is not None:
                text = "\n".join(data_lines)
                try:
                    data: Any = json.loads(text)
                except ValueError:
                    data = text
                yield TaskEvent(id=event_id, type=event_type, data=data)
            event_id, event_type, data_lines = None, "message", []
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if name == "id":
            try:
                event_id = int(value)
            except ValueError:
                event_id = None
        elif name == "event":
            event_type = value or "message"
        elif name == "data":
            data_lines.append(value)


def connect(
    *,
    spawn_if_needed: bool = True,
    parent_pid: int | None = None,
    app_data_dir: str | Path | None = None,
) -> EngineClient:
    """Attach to the running engine, or start one tied to ``parent_pid``.

    ``parent_pid`` defaults to this process: the engine started here exits
    when it does.  Raises ``EngineUnavailable`` when neither works.
    """
    attached = attach(app_data_dir=app_data_dir)
    if attached is not None:
        return attached
    if not spawn_if_needed:
        raise EngineUnavailable("翻译引擎未在运行。请先打开 Translator，或允许命令行自行启动引擎。")
    return spawn(parent_pid=parent_pid, app_data_dir=app_data_dir)


def attach(*, app_data_dir: str | Path | None = None) -> EngineClient | None:
    """The engine published in ``engine.json``, if it is alive and answers."""
    info = read_engine_file(app_data_dir)
    if info is None or not _pid_alive(info.pid):
        return None
    client = EngineClient(f"http://127.0.0.1:{info.port}", info.token, pid=info.pid)
    if client.health():
        return client
    client.close()
    return None


def spawn(
    *,
    parent_pid: int | None = None,
    app_data_dir: str | Path | None = None,
    start_timeout: float = START_TIMEOUT_SECONDS,
) -> EngineClient:
    """Start an engine in the background and wait until it answers."""
    data_dir = Path(app_data_dir) if app_data_dir is not None else get_app_data_dir()
    run_dir = data_dir / RUN_DIR_NAME
    run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    log_path = run_dir / SPAWN_LOG_NAME
    env = {
        **os.environ,
        "TRANSLATOR_SIDECAR_PARENT_PID": str(parent_pid if parent_pid is not None else os.getpid()),
        "PYTHONUNBUFFERED": "1",
    }
    if app_data_dir is not None:
        env["TRANSLATOR_APP_DATA_DIR"] = str(data_dir)
    # Only the app's dev shell may widen the CORS allowlist.
    env.pop("TRANSLATOR_DEV_ORIGIN", None)
    command, cwd = engine_command()
    with open(log_path, "wb") as log:
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=log,
                creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        except OSError as exc:
            raise EngineUnavailable(f"无法启动翻译引擎：{exc}") from exc
    try:
        port, token = _read_handshake(process, start_timeout)
    except EngineUnavailable as exc:
        _abort(process)
        raise EngineUnavailable(_with_log_tail(str(exc), log_path)) from None
    # Keep draining stdout: nothing should print there after the handshake,
    # but a full pipe would block the engine on its next write if anything did.
    threading.Thread(target=_drain, args=(process.stdout,), daemon=True, name="engine-stdout").start()
    client = EngineClient(f"http://127.0.0.1:{port}", token, process=process)
    deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if client.health(timeout=0.5):
            return client
        if process.poll() is not None:
            break
        time.sleep(0.1)
    client.close(timeout=2.0)
    raise EngineUnavailable(_with_log_tail("翻译引擎已启动，但没有通过健康检查。", log_path))


def engine_command() -> tuple[list[str], str | None]:
    """How to start an engine from this process.

    Frozen, this process *is* the sidecar executable, which runs the engine
    when given no subcommand.  From source it is ``python -m api.launcher``
    in the repository root.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable], str(Path(sys.executable).parent)
    return [sys.executable, "-m", "api.launcher"], str(_REPO_ROOT)


def _read_handshake(process: subprocess.Popen[Any], timeout: float) -> tuple[int, str]:
    result: dict[str, Any] = {}

    def read() -> None:
        try:
            result["line"] = process.stdout.readline().decode("utf-8", "replace") if process.stdout else ""
        except Exception as exc:  # noqa: BLE001 - reported below
            result["error"] = exc

    reader = threading.Thread(target=read, daemon=True, name="engine-handshake")
    reader.start()
    reader.join(timeout)
    if reader.is_alive():
        raise EngineUnavailable("翻译引擎启动超时。")
    line = str(result.get("line") or "")
    if not line:
        raise EngineUnavailable("翻译引擎在完成启动前退出了。")
    return parse_handshake(line)


def _abort(process: subprocess.Popen[Any]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def _drain(stream: Any) -> None:
    try:
        while stream.read(65536):
            pass
    except (OSError, ValueError):
        pass


def _with_log_tail(message: str, log_path: Path, *, lines: int = 12) -> str:
    try:
        tail = log_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-lines:]
    except OSError:
        tail = []
    if not tail:
        return message
    return message + "\n引擎输出（最后几行）：\n" + "\n".join(tail)


def _pid_alive(pid: int) -> bool:
    try:
        return psutil.pid_exists(pid)
    except Exception:  # noqa: BLE001 - a failed probe falls through to the health check
        return True


def _task_path(task_id: str) -> str:
    value = str(task_id or "").strip()
    if not value or "/" in value or value in {".", ".."}:
        raise ValueError(f"invalid task id: {task_id!r}")
    return value


def _engine_error(response: httpx.Response) -> EngineError:
    payload: Any = None
    detail = ""
    reason = str(response.headers.get("X-Translator-Reason") or "")
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        raw = payload.get("detail")
        if isinstance(raw, str):
            detail = raw
        elif isinstance(raw, list):
            # Request validation (FastAPI 422): one line per rejected field.
            detail = "；".join(_validation_line(item) for item in raw if isinstance(item, dict))
        reason = str(payload.get("reason") or reason)
    if not detail:
        detail = "翻译引擎拒绝了这个请求。" if response.status_code != 401 else "翻译引擎拒绝了连接令牌。"
    return EngineError(response.status_code, detail, reason=reason, payload=payload)


def _validation_line(item: dict[str, Any]) -> str:
    location = ".".join(str(part) for part in item.get("loc") or () if part != "body")
    message = str(item.get("msg") or "")
    return f"{location}: {message}" if location else message
