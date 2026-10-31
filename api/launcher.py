"""Run the Translator FastAPI sidecar on a token-protected loopback port."""

from __future__ import annotations

import secrets
import socket
import os
import threading
import time

import psutil
import uvicorn
from loguru import logger

from api.app import create_app
from core import engine_file, text_transport

# How long uvicorn waits for open connections (mainly SSE streams) before it
# closes them itself and runs the app's shutdown hook.
GRACEFUL_SHUTDOWN_SECONDS = 10.0
# Last resort when the orderly shutdown itself wedges after the desktop shell
# is already gone.
WATCHDOG_FORCE_EXIT_SECONDS = 20.0
# How long programs this engine started (LibreOffice, the ODA converter,
# Tesseract, extension hosts) get to exit after a polite request before they
# are killed.
CHILD_STOP_SECONDS = 3.0


def end_child_processes(timeout: float = CHILD_STOP_SECONDS) -> list[int]:
    """End every program this engine started, and the programs those started.

    A runner waiting on ``subprocess.run`` cannot be interrupted by a flag, so
    without this a shutdown waits out the whole task budget while LibreOffice
    or the ODA converter keeps working, and the program then outlives the
    engine: measured on Windows, an extension host, its console host and its
    temporary folder were all still there after every kind of exit.  Ending
    the children lets the waiting runner return at once and unwind normally.

    Office applications driven over COM are started by Windows, not by this
    process, so they are not children and are left alone.  Returns the pids
    that were asked to stop.
    """
    try:
        children = psutil.Process().children(recursive=True)
    except psutil.Error:
        return []
    for child in children:
        try:
            child.terminate()
        except psutil.Error:
            pass
    _gone, alive = psutil.wait_procs(children, timeout=max(0.0, timeout))
    for child in alive:
        try:
            child.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(alive, timeout=1.0)
    return [child.pid for child in children]


# Once the engine is shutting down nothing it starts is wanted any more, so the
# sweep keeps running until the process exits: a runner whose converter was just
# ended often falls back to the next one (LibreOffice → textutil, a second ODA
# pass), and that one must not hold the shutdown for the whole task budget.
CHILD_SWEEP_INTERVAL_SECONDS = 0.5
_sweeper_started = threading.Event()


def _keep_ending_child_processes() -> None:
    while True:
        try:
            end_child_processes()
        except Exception:  # noqa: BLE001 - keep sweeping; one bad pid must not stop it
            pass
        time.sleep(CHILD_SWEEP_INTERVAL_SECONDS)


def _end_child_processes_soon() -> None:
    """Start ending this engine's child programs, now and until it exits.

    Runs off the calling thread: ``begin_shutdown`` runs inside a signal
    handler on Unix, where waiting for other processes is not allowed.  Calling
    it again (signal and shutdown request both arrive) starts nothing new.
    """
    if _sweeper_started.is_set():
        return
    _sweeper_started.set()
    threading.Thread(target=_keep_ending_child_processes, daemon=True, name="translator-end-children").start()


def _parent_process_is_alive(parent_pid: int) -> bool:
    """Return whether the desktop shell that spawned this sidecar still exists."""
    if parent_pid <= 1:
        return True
    # os.kill(pid, 0) is a POSIX liveness probe, but on Windows any signal
    # other than CTRL_C/CTRL_BREAK calls TerminateProcess and kills the shell.
    try:
        return psutil.pid_exists(parent_pid)
    except Exception:
        return True


class GracefulSidecarServer(uvicorn.Server):
    """Tell the task manager to wind down before the server stops serving.

    uvicorn only runs the app's shutdown hook after every connection is
    closed, and an SSE stream stays open for as long as its task runs.  Asking
    the runners to stop from ``handle_exit`` breaks that deadlock: the streams
    end, the connections close, and the shutdown hook then does the waiting.
    """

    def __init__(self, config: uvicorn.Config, *, on_exit) -> None:
        super().__init__(config)
        self._on_exit = on_exit

    def handle_exit(self, sig, frame) -> None:  # noqa: D102 - uvicorn hook
        # Runs inside a signal handler: flip flags only, never block here.
        try:
            self._on_exit()
        except Exception:
            pass
        super().handle_exit(sig, frame)


def _shutdown_requester(server: uvicorn.Server, on_exit):
    """Return the hook behind ``POST /api/system/shutdown``.

    It does what ``GracefulSidecarServer.handle_exit`` does for SIGTERM: ask
    the runners to stop first, then let uvicorn run its orderly shutdown
    (drain connections, lifespan, ``task_manager.shutdown``, history flush).
    Windows has no SIGTERM for the desktop shell to send, so this is the only
    way that platform gets the same unwinding instead of ``TerminateProcess``.
    Calling it twice is harmless; both steps only set flags.
    """

    def request() -> None:
        try:
            on_exit()
        except Exception:
            pass
        server.should_exit = True

    return request


def _start_parent_watchdog(server: uvicorn.Server, on_exit) -> None:
    """Exit when a Tauri parent dies so a loopback sidecar cannot linger."""
    raw_parent_pid = os.environ.get("TRANSLATOR_SIDECAR_PARENT_PID", "").strip()
    try:
        parent_pid = int(raw_parent_pid)
    except ValueError:
        return
    if parent_pid <= 1:
        return

    def watch() -> None:
        while _parent_process_is_alive(parent_pid):
            time.sleep(1)
        # A hard ``os._exit`` here skipped every runner's cleanup, so the
        # temporary docx directory, the LibreOffice profile and the PDF page
        # workspaces survived the shell.  Unwind first, force-exit only if
        # that does not finish.
        try:
            on_exit()
        except Exception:
            pass
        server.should_exit = True
        deadline = time.monotonic() + WATCHDOG_FORCE_EXIT_SECONDS
        while time.monotonic() < deadline:
            time.sleep(0.25)
        # Leaving now would orphan whatever the engine started.
        end_child_processes(timeout=1.0)
        os._exit(0)

    threading.Thread(target=watch, daemon=True, name="translator-parent-watchdog").start()


def _publish_engine_file(port: int, token: str) -> None:
    """Leave this engine's address for the command line and MCP clients.

    The desktop shell reads the handshake line instead, so a data directory
    that cannot be written to only costs those clients their attach, never
    the app its engine.
    """
    try:
        engine_file.write_engine_file(port=port, token=token)
    except OSError as exc:
        logger.warning(f"无法写入引擎连接信息（命令行将改为自行启动引擎）：{exc}")


def main() -> None:
    token = secrets.token_urlsafe(32)
    app = create_app(auth_token=token)

    def begin_shutdown() -> None:
        # Stop advertising first, so a command line reading engine.json from
        # now on starts its own engine instead of attaching to one on its way
        # out.  Only a small read and an unlink: this runs in a signal handler.
        engine_file.remove_engine_file()
        manager = getattr(app.state, "task_manager", None)
        if manager is not None:
            manager.begin_shutdown()
        # A runner waiting on a model reply (up to minutes) cannot see the stop
        # flag; cancel the in-flight requests so it unwinds at once.
        text_transport.cancel_all_requests()
        # After the runners know they are stopping, so a runner whose program
        # just died reads it as the stop, not as a conversion failure.
        _end_child_processes_soon()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        port = sock.getsockname()[1]
        print(f"PORT={port} TOKEN={token}", flush=True)
        _publish_engine_file(port, token)
        config = uvicorn.Config(
            app,
            log_level="warning",
            access_log=False,
            timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
        )
        server = GracefulSidecarServer(config, on_exit=begin_shutdown)
        app.state.request_shutdown = _shutdown_requester(server, begin_shutdown)
        _start_parent_watchdog(server, begin_shutdown)
        try:
            server.run(sockets=[sock])
        finally:
            engine_file.remove_engine_file()
            # Anything a runner started while it was unwinding.
            end_child_processes(timeout=1.0)


if __name__ == "__main__":
    main()
