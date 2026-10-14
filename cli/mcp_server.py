"""``translator-sidecar mcp``: the command line's actions as MCP tools (stdio).

Each tool does what a subcommand does, through ``cli/flow.py`` and
``core/engine_client.py``: with Translator open the tasks go into its task
center; otherwise the engine starts on the first call that needs it and
ends with this process.  stdout carries only the protocol: while serving,
the SDK points file descriptor 1 at stderr, so stray output cannot reach it.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from typing import Annotated, Any, TypeVar

import anyio
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from cli import flow
from core.engine_client import EngineClient, EngineError, EngineUnavailable, connect

SERVER_NAME = "translator"
TOOL_NAMES = (
    "list_types",
    "scan_sources",
    "start_translation",
    "get_task",
    "wait_task",
    "stop_task",
    "get_task_result",
)
#: Below the 60-second tool timeout common MCP clients apply.
WAIT_DEFAULT_SECONDS = 50.0
WAIT_MAX_SECONDS = 600.0
_POLL_SECONDS = 1.0
#: A scan of a large folder lists at most this many files.
_SCAN_FILE_LIMIT = 1000
_TASK_LOG_LINES = 10
_REVIEW_ROW_LIMIT = 50

INSTRUCTIONS = (
    "Translator translates Excel, Word, PDF/image and CAD files on this computer with the model "
    "configured in the Translator app. Usual flow: start_translation with absolute paths and a target "
    "language, then wait_task until done is true, then get_task_result for the output files. Results "
    "are written to a new folder next to the sources; source files are never changed. Use scan_sources "
    "first to see which files a folder would contribute."
)

T = TypeVar("T")


class EngineSession:
    """One engine connection per MCP session, made on first use.

    Listing the tools does not start an engine.  If the app the session was
    attached to closes, the next read-only call reconnects (starting an
    engine of its own); a start is never sent twice.
    """

    def __init__(self, connector: Callable[..., EngineClient] = connect) -> None:
        self._connector = connector
        self._client: EngineClient | None = None
        self._lock = threading.Lock()

    def call(self, action: Callable[[EngineClient], T], *, retry: bool = True) -> T:
        client = self._get()
        try:
            return action(client)
        except EngineUnavailable:
            self._drop(client)
            if not retry:
                raise
        return action(self._get())

    def close(self) -> None:
        with self._lock:
            client, self._client = self._client, None
        if client is not None:
            client.close()

    def _get(self) -> EngineClient:
        with self._lock:
            if self._client is None:
                self._client = self._connector(spawn_if_needed=True, parent_pid=os.getpid())
            return self._client

    def _drop(self, client: EngineClient) -> None:
        with self._lock:
            if self._client is client:
                self._client = None
        try:
            client.close(timeout=2)
        except Exception:  # noqa: BLE001 - it is already gone
            pass


def build_server(session: EngineSession) -> MCPServer:
    from app_meta import APP_VERSION

    server = MCPServer(name=SERVER_NAME, title="Translator", version=APP_VERSION, instructions=INSTRUCTIONS)

    def run(action: Callable[[EngineClient], T], *, retry: bool = True) -> T:
        try:
            return session.call(action, retry=retry)
        except flow.CliError as exc:
            raise ToolError(exc.message) from None
        except EngineError as exc:
            raise ToolError(exc.detail) from None
        except EngineUnavailable as exc:
            raise ToolError(str(exc)) from None

    @server.tool(
        description=(
            "List the file types Translator can translate: each type's key, names, file extensions "
            "and its switches (usable as start_translation options)."
        )
    )
    def list_types() -> dict[str, Any]:
        types = run(lambda client: flow.file_types(client.surfaces()))
        return {"types": [flow.type_payload(item) for item in types]}

    @server.tool(
        description=(
            "Show which files would be translated, without translating: the detected type, the files "
            "found and the files skipped with the reason. Paths are absolute: one file, one folder, or "
            "several files in the same folder."
        )
    )
    def scan_sources(
        paths: Annotated[list[str], Field(description="Absolute paths: a file, a folder, or files in one folder.")],
        file_type: Annotated[
            str | None, Field(description="Type key from list_types; by default chosen from the file extensions.")
        ] = None,
    ) -> dict[str, Any]:
        def scan(client: EngineClient) -> dict[str, Any]:
            detection = flow.detect(client.surfaces(), paths, type_key=file_type)
            surface, scan_paths = flow.scan_request(detection)
            scanned = client.scan(surface, scan_paths, apply_saved_defaults=True)
            names = [
                str(item.get("relative_path") or item.get("name") or item.get("path") or "")
                for item in scanned.get("items") or ()
                if isinstance(item, dict)
            ]
            report: dict[str, Any] = {
                "type": detection.key,
                "note": detection.note,
                "file_count": len(names),
                "files": names[:_SCAN_FILE_LIMIT],
                "skipped": [item for item in scanned.get("skipped") or () if isinstance(item, dict)],
            }
            if len(names) > _SCAN_FILE_LIMIT:
                report["files_truncated"] = True
            return report

        _check_paths(paths)
        return run(scan)

    @server.tool(
        description=(
            "Start translating files and return the task id at once; the task runs in the background. "
            "Then call wait_task (repeatedly, until done) and get_task_result. Options not given use the "
            "values saved in the Translator app. If the result has needs_confirmation, nothing was "
            "started: tell the user the message and, if they agree, call again with the same arguments "
            "plus accept set to the returned accept object."
        )
    )
    def start_translation(
        paths: Annotated[list[str], Field(description="Absolute paths: a file, a folder, or files in one folder.")],
        target_lang: Annotated[
            str | None, Field(description="Target language code such as fr, en, zh; default: the app's saved one.")
        ] = None,
        source_lang: Annotated[
            str | None, Field(description="Source language code or auto; default: the app's saved one.")
        ] = None,
        file_type: Annotated[
            str | None, Field(description="Type key from list_types; by default chosen from the file extensions.")
        ] = None,
        options: Annotated[
            dict[str, Any] | None,
            Field(description="Task options by name (see list_types), e.g. {\"uppercase\": true}."),
        ] = None,
        accept: Annotated[
            dict[str, Any] | None,
            Field(description="The accept object from an earlier needs_confirmation answer, after the user agreed."),
        ] = None,
        accept_all: Annotated[
            bool, Field(description="Accept every confirmation (only when the user said so in advance).")
        ] = False,
    ) -> dict[str, Any]:
        _check_paths(paths)
        option_values = {str(key): value for key, value in (options or {}).items()}

        def start(client: EngineClient) -> dict[str, Any]:
            detection = flow.detect(client.surfaces(), paths, type_key=file_type)
            request = flow.build_request(
                detection, target_lang=target_lang, source_lang=source_lang, options=option_values
            )
            flow.apply_accept(request, accept)
            try:
                started = flow.start_task(client, request, accept_all=accept_all)
            except flow.NeedsConfirmation as exc:
                return exc.as_dict()
            return {
                "task_id": started.task_id,
                "type": detection.key,
                "note": detection.note,
                "in_app_task_center": not client.started_here,
                "options": started.preflight.get("options") or {},
                "accepted": started.accepted,
            }

        return run(start, retry=False)

    @server.tool(
        description=(
            "Get a task's state now: state, percent, current phase, file counts and the last log lines. "
            "To wait for the end, use wait_task instead of calling this in a loop."
        )
    )
    def get_task(task_id: Annotated[str, Field(description="Task id from start_translation.")]) -> dict[str, Any]:
        return run(lambda client: _brief_status(client.task(task_id)))

    @server.tool(
        description=(
            "Wait until a task ends or the timeout passes, then return its state. done is false when "
            "the timeout passed first: call wait_task again. When done, call get_task_result."
        )
    )
    async def wait_task(
        task_id: Annotated[str, Field(description="Task id from start_translation.")],
        ctx: Context,
        timeout_seconds: Annotated[
            float,
            Field(description=f"Longest wait in seconds (default {WAIT_DEFAULT_SECONDS:g}, at most {WAIT_MAX_SECONDS:g})."),
        ] = WAIT_DEFAULT_SECONDS,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.0, min(float(timeout_seconds), WAIT_MAX_SECONDS))
        while True:
            status = await anyio.to_thread.run_sync(lambda: run(lambda client: client.task(task_id)))
            brief = _brief_status(status)
            if brief["terminal"] or time.monotonic() >= deadline:
                return {"done": bool(brief["terminal"]), **brief}
            percent = brief["percent"]
            if percent is not None:
                await ctx.report_progress(float(percent), 100.0, brief["phase"] or None)
            await anyio.sleep(min(_POLL_SECONDS, max(0.0, deadline - time.monotonic())))

    @server.tool(description="Stop a running task. Files already finished stay in the output folder.")
    def stop_task(task_id: Annotated[str, Field(description="Task id from start_translation.")]) -> dict[str, Any]:
        status = run(lambda client: client.stop(task_id))
        return {"task_id": task_id, "state": status.get("state")}

    @server.tool(
        description=(
            "Get a finished task's result: the output folder, each file's output path and state, how "
            "many places need review and where (review_items, at most 50), and the report path. "
            "Call after wait_task says done."
        )
    )
    def get_task_result(
        task_id: Annotated[str, Field(description="Task id from start_translation.")],
    ) -> dict[str, Any]:
        def result(client: EngineClient) -> dict[str, Any]:
            status = client.results(task_id)
            details = status.get("result") if isinstance(status.get("result"), dict) else {}
            return {**flow.summarize(status), "review_items": flow.review_rows(details, limit=_REVIEW_ROW_LIMIT)}

        return run(result)

    return server


def _check_paths(paths: list[str]) -> None:
    try:
        flow.resolve_paths(paths)
    except flow.CliError as exc:
        raise ToolError(exc.message) from None


def _brief_status(status: dict[str, Any]) -> dict[str, Any]:
    full = flow.task_status(status, logs=_TASK_LOG_LINES)
    progress = full.pop("progress")
    full.pop("files")
    full["percent"] = progress.get("overall_percent")
    full["phase"] = str(progress.get("phase_name") or "")
    full["file_count"] = len(((status.get("file_progress") or {}).get("files")) or [])
    return full


def serve() -> int:
    """Serve MCP on stdin/stdout until the client closes the stream."""
    session = EngineSession()
    server = build_server(session)
    # One INFO line per engine request would bury the server's own log in the client.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        anyio.run(server.run_stdio_async)
    except KeyboardInterrupt:
        pass
    finally:
        session.close()
    return 0
