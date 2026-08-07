"""Run the Translator FastAPI sidecar on a token-protected loopback port."""

from __future__ import annotations

import secrets
import socket
import os
import threading
import time

import psutil
import uvicorn

from api.app import create_app

# How long uvicorn waits for open connections (mainly SSE streams) before it
# closes them itself and runs the app's shutdown hook.
GRACEFUL_SHUTDOWN_SECONDS = 10.0
# Last resort when the orderly shutdown itself wedges after the desktop shell
# is already gone.
WATCHDOG_FORCE_EXIT_SECONDS = 20.0


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
        os._exit(0)

    threading.Thread(target=watch, daemon=True, name="translator-parent-watchdog").start()


def main() -> None:
    token = secrets.token_urlsafe(32)
    app = create_app(auth_token=token)

    def begin_shutdown() -> None:
        manager = getattr(app.state, "task_manager", None)
        if manager is not None:
            manager.begin_shutdown()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        port = sock.getsockname()[1]
        print(f"PORT={port} TOKEN={token}", flush=True)
        config = uvicorn.Config(
            app,
            log_level="warning",
            access_log=False,
            timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
        )
        server = GracefulSidecarServer(config, on_exit=begin_shutdown)
        _start_parent_watchdog(server, begin_shutdown)
        server.run(sockets=[sock])


if __name__ == "__main__":
    main()
