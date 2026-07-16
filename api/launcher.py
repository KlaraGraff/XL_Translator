"""Run the Translator FastAPI sidecar on a token-protected loopback port."""

from __future__ import annotations

import secrets
import socket

import uvicorn

from api.app import create_app


def main() -> None:
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
