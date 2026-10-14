"""Where a running engine says how to reach it: ``<app_data>/run/engine.json``.

The desktop shell learns the engine's port and token from the handshake line
the engine prints on stdout.  A command-line or MCP client started on its own
has no such pipe, so every engine also leaves ``{port, token, pid, version,
started_at}`` in this file, readable by the current user only, and takes it
away again when it shuts down.  ``core.engine_client`` reads it to attach to
the running app before it considers starting an engine of its own.

Two engines can be alive at once (the app's, and one a command started while
the app was closed).  Each writes its own file and the later one wins; an
engine only ever removes the file while it still names its own pid, so the
first one shutting down cannot take the second one's entry with it.  A file
left behind by a crash names a pid that no longer exists, or one that no
longer answers with this token, and readers treat it as absent.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from app_meta import APP_VERSION
from core.app_paths import get_app_data_dir

RUN_DIR_NAME = "run"
ENGINE_FILE_NAME = "engine.json"


@dataclass(frozen=True)
class EngineFileInfo:
    port: int
    token: str
    pid: int
    version: str
    started_at: str


def engine_file_path(app_data_dir: str | Path | None = None) -> Path:
    base = Path(app_data_dir) if app_data_dir is not None else get_app_data_dir()
    return base / RUN_DIR_NAME / ENGINE_FILE_NAME


def write_engine_file(
    *,
    port: int,
    token: str,
    pid: int | None = None,
    app_data_dir: str | Path | None = None,
) -> Path:
    """Publish this engine's address, replacing whatever entry is there."""
    path = engine_file_path(app_data_dir)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = EngineFileInfo(
        port=int(port),
        token=str(token),
        pid=int(pid if pid is not None else os.getpid()),
        version=APP_VERSION,
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    body = json.dumps(asdict(info), ensure_ascii=False, indent=2).encode("utf-8")
    # The token is a credential: create the file 0600 from the start (a chmod
    # afterwards leaves a window), and swap it in whole so a reader never sees
    # half of it.  A leftover temp file may carry looser permissions, which
    # O_TRUNC would keep, so it goes first.  On Windows the mode is ignored and
    # the file inherits the user profile's ACL.
    temporary = path.with_name(f".{ENGINE_FILE_NAME}.{info.pid}.tmp")
    temporary.unlink(missing_ok=True)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(body)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return path


def read_engine_file(app_data_dir: str | Path | None = None) -> EngineFileInfo | None:
    """The published entry, or ``None`` when there is none or it is unreadable."""
    try:
        raw = json.loads(engine_file_path(app_data_dir).read_text(encoding="utf-8"))
        info = EngineFileInfo(
            port=int(raw["port"]),
            token=str(raw["token"]),
            pid=int(raw["pid"]),
            version=str(raw.get("version") or ""),
            started_at=str(raw.get("started_at") or ""),
        )
    except (OSError, ValueError, TypeError, KeyError):
        return None
    if not (0 < info.port < 65536) or not info.token or info.pid <= 0:
        return None
    return info


def remove_engine_file(*, pid: int | None = None, app_data_dir: str | Path | None = None) -> bool:
    """Withdraw the entry if it still names ``pid`` (default: this process).

    Never raises: it runs on the way out, where a failure must not stop the
    shutdown, and a stale file is harmless to readers anyway.
    """
    owner = int(pid if pid is not None else os.getpid())
    current = read_engine_file(app_data_dir)
    if current is None or current.pid != owner:
        return False
    try:
        engine_file_path(app_data_dir).unlink(missing_ok=True)
    except OSError:
        return False
    return True
