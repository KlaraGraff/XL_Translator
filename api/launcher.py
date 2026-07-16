"""Run the Translator FastAPI sidecar on a token-protected loopback port."""

from __future__ import annotations

import secrets
import socket
import os
import threading
import time

import uvicorn

from api.app import create_app


def _parent_process_is_alive(parent_pid: int) -> bool:
    """Return whether the desktop shell that spawned this sidecar still exists."""
    if parent_pid <= 1:
        return True
    try:
        os.kill(parent_pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _start_parent_watchdog() -> None:
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
        os._exit(0)

    threading.Thread(target=watch, daemon=True, name="translator-parent-watchdog").start()


def main() -> None:
    _start_parent_watchdog()
    token = secrets.token_urlsafe(32)
    app = create_app(auth_token=token)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        port = sock.getsockname()[1]
        print(f"PORT={port} TOKEN={token}", flush=True)
        config = uvicorn.Config(app, log_level="warning", access_log=False)
        uvicorn.Server(config).run(sockets=[sock])


if __name__ == "__main__":
    main()
