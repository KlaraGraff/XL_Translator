"""What the command line and the MCP server share: type detection, options,
the start sequence with its confirmations, and the result summary.

Every type-specific fact comes from the descriptors (``GET /api/surfaces``)
or from the engine's answers; nothing here names a type.  A type registered
at run time is detected, configured and started exactly like a built-in one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.engine_client import EngineClient, EngineError, TaskEvent

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_INPUT = 2
EXIT_ENGINE = 3
EXIT_CONFIRM = 4

#: Request fields the command line sets itself; ``--option`` cannot name them.
RESERVED_FIELDS = frozenset(
    {
        "surface",
        "source_path",
        "selected_paths",
        "source_lang",
        "target_lang",
        "confirmation_token",
        "options",
        "apply_saved_defaults",
        "lang_pair",
    }
)
#: Kinds of stop a caller may step past: three ``accept`` stops plus a
#: shared model connection.  ``--yes`` accepts each as it comes, up to this many.
MAX_CONFIRMATIONS = 4
#: Folders the app writes its own output into; never counted as sources.
_SKIPPED_DIR_MARKERS = ("翻译输出", ".cad-work", ".cad-review")
_DETECTION_FILE_LIMIT = 50_000
_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n"}


class CliError(Exception):
    """A failure with its own exit code and a sentence for the user."""

    exit_code = EXIT_INPUT

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class InputError(CliError):
    exit_code = EXIT_INPUT


class NeedsConfirmation(CliError):
    """The engine stops here until the caller says "continue anyway".

    ``accept`` holds the request fields that say so; sending the request
    again with them (or with ``--yes``) continues.
    """

    exit_code = EXIT_CONFIRM

    def __init__(self, message: str, *, reason: str, accept: dict[str, Any]) -> None:
        super().__init__(message)
        self.reason = reason
        self.accept = dict(accept)

    def as_dict(self) -> dict[str, Any]:
        return {"needs_confirmation": True, "reason": self.reason, "message": self.message, "accept": self.accept}


# ── types ────────────────────────────────────────────────────────────────


def file_types(descriptors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The types that translate files, in sidebar order."""
    return [
        item
        for item in descriptors
        if (item.get("capabilities") or {}).get("requires_source", True) and item.get("extensions")
    ]


def label_of(descriptor: dict[str, Any], language: str = "zh") -> str:
    labels = descriptor.get("label") or {}
    return str(labels.get(language) or labels.get("en") or descriptor.get("key") or "")


def type_payload(descriptor: dict[str, Any]) -> dict[str, Any]:
    """A type as ``types`` / ``list_types`` report it."""
    return {
        "key": descriptor.get("key"),
        "label": descriptor.get("label"),
        "builtin": descriptor.get("builtin", True),
        "extensions": descriptor.get("extensions") or [],
        "options": descriptor.get("options") or [],
    }


def find_type(descriptors: list[dict[str, Any]], key: str) -> dict[str, Any]:
    wanted = str(key or "").strip().lower()
    types = file_types(descriptors)
    for item in types:
        if str(item.get("key") or "").lower() == wanted:
            return item
    known = "、".join(str(item.get("key")) for item in types)
    raise InputError(f"没有「{key}」这种类型。可用的类型：{known}。")


@dataclass
class Detection:
    """Which type will handle the given paths, and how to send them."""

    descriptor: dict[str, Any]
    source_path: str
    selected_paths: list[str] = field(default_factory=list)
    #: Files per type key among the inputs (for a folder: everything under it).
    counts: dict[str, int] = field(default_factory=dict)
    #: A sentence for the user when the choice was not the only possible one.
    note: str = ""

    @property
    def key(self) -> str:
        return str(self.descriptor.get("key") or "")


def resolve_paths(paths: list[str]) -> list[Path]:
    """Absolute paths (the engine runs elsewhere), checked before any engine is involved."""
    resolved = [Path(path).expanduser().resolve() for path in paths if str(path).strip()]
    if not resolved:
        raise InputError("请给出要翻译的文件或文件夹。")
    for path in resolved:
        if not path.exists():
            raise InputError(f"找不到「{path}」。")
    if len(resolved) > 1:
        if any(not path.is_file() for path in resolved):
            raise InputError("一次只能给一个文件夹；要翻译多个文件时，请给出同一文件夹里的文件。")
        if len({path.parent for path in resolved}) > 1:
            raise InputError("多个文件必须在同一个文件夹里。")
    return resolved


def detect(
    descriptors: list[dict[str, Any]],
    paths: list[str],
    *,
    type_key: str | None = None,
) -> Detection:
    """Pick the type for ``paths`` from the descriptors' extensions.

    One file: the first type (in registration order, built-in pages first)
    that declares its extension.  A folder, or several files: the type with
    the most files; ties go to registration order.  ``type_key`` overrides
    the choice but still checks the paths.
    """
    resolved = resolve_paths(paths)
    types = file_types(descriptors)
    if not types:
        raise InputError("翻译引擎没有提供任何可翻译的文件类型。")

    if len(resolved) == 1 and resolved[0].is_dir():
        source_path, selected = str(resolved[0]), []
        suffixes = _suffixes_under(resolved[0])
    elif len(resolved) == 1:
        source_path, selected = str(resolved[0]), []
        suffixes = [_suffix(resolved[0])]
    else:
        source_path, selected = str(resolved[0].parent), [str(path) for path in resolved]
        suffixes = [_suffix(path) for path in resolved]

    counts = {
        str(item["key"]): sum(1 for suffix in suffixes if suffix in _extensions(item)) for item in types
    }
    if type_key:
        chosen = find_type(descriptors, type_key)
        if not counts.get(str(chosen["key"])):
            raise InputError(
                f"「{label_of(chosen)}」只处理 {_extension_list(chosen)} 文件，这里没有找到。"
            )
        return Detection(chosen, source_path, selected, counts)

    ranked = [item for item in types if counts[str(item["key"])] > 0]
    if not ranked:
        found = sorted({suffix for suffix in suffixes if suffix})
        found_text = "、".join(f".{suffix}" for suffix in found[:8]) or "无扩展名的文件"
        raise InputError(f"没有可翻译的文件（找到的是：{found_text}）。可用 types 查看支持的类型。")
    best = max(ranked, key=lambda item: counts[str(item["key"])])  # max keeps the first of equals
    others = [item for item in ranked if item is not best]
    note = ""
    if others:
        rest = "、".join(f"{label_of(item)} {counts[str(item['key'])]} 个" for item in others)
        note = f"已按「{label_of(best)}」处理（其他可译文件：{rest}），可用 --type 指定。"
    return Detection(best, source_path, selected, counts, note)


def _suffix(path: Path) -> str:
    return path.suffix.lower().lstrip(".")


def _extensions(descriptor: dict[str, Any]) -> set[str]:
    return {str(item).lower().lstrip(".") for item in descriptor.get("extensions") or ()}


def _extension_list(descriptor: dict[str, Any]) -> str:
    return " ".join(f".{item}" for item in sorted(_extensions(descriptor)))


def _suffixes_under(root: Path) -> list[str]:
    found: list[str] = []
    for current, directories, files in os.walk(root):
        directories[:] = [
            name
            for name in directories
            if not name.startswith(".") and not any(marker in name for marker in _SKIPPED_DIR_MARKERS)
        ]
        for name in files:
            if name.startswith((".", "~$")):
                continue
            found.append(_suffix(Path(name)))
            if len(found) >= _DETECTION_FILE_LIMIT:
                return found
    return found


# ── options ──────────────────────────────────────────────────────────────


def parse_option_pairs(pairs: list[str]) -> dict[str, str]:
    """``["k=v", …]`` from ``--option``; the last value of a key wins."""
    parsed: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = str(pair).partition("=")
        key = key.strip()
        if not sep or not key:
            raise InputError(f"--option 的格式是 名称=值，收到的是「{pair}」。")
        parsed[key] = value.strip()
    return parsed


def request_fields(descriptor: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    """Turn option values into start-request fields for this type.

    A built-in type's options are the request's own named fields, converted
    with the request schema's annotations.  A type registered at run time
    keeps its switches in ``options``, typed by its descriptor.
    """
    if not values:
        return {}
    if descriptor.get("builtin", True):
        return _builtin_fields(values)
    return {"options": _descriptor_options(descriptor, values)}


def _builtin_fields(values: dict[str, Any]) -> dict[str, Any]:
    from pydantic import TypeAdapter, ValidationError

    from api.schemas import TaskStartRequest

    known = {name: info for name, info in TaskStartRequest.model_fields.items() if name not in RESERVED_FIELDS}
    fields: dict[str, Any] = {}
    for key, raw in values.items():
        info = known.get(key)
        if info is None:
            raise InputError(f"没有「{key}」这个选项。可用的选项：{', '.join(sorted(known))}。")
        try:
            fields[key] = TypeAdapter(info.annotation).validate_python(raw)
        except ValidationError:
            raise InputError(f"选项「{key}」的值「{raw}」无效。") from None
    return fields


def _descriptor_options(descriptor: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    declared = {str(item.get("key")): item for item in descriptor.get("options") or ()}
    options: dict[str, Any] = {}
    for key, raw in values.items():
        spec = declared.get(key)
        if spec is None:
            known = "、".join(sorted(declared)) or "（无）"
            raise InputError(f"「{label_of(descriptor)}」没有「{key}」这个选项。可用的选项：{known}。")
        options[key] = _typed_value(key, raw, spec)
    return options


def _typed_value(key: str, raw: Any, spec: dict[str, Any]) -> Any:
    default = spec.get("default")
    if not isinstance(raw, str):
        return raw
    text = raw.strip()
    if spec.get("kind") == "toggle" or isinstance(default, bool):
        lowered = text.lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
        raise InputError(f"选项「{key}」是开关，值应为 true 或 false，收到的是「{raw}」。")
    try:
        if isinstance(default, int):
            return int(text)
        if isinstance(default, float):
            return float(text)
    except ValueError:
        raise InputError(f"选项「{key}」的值应为数字，收到的是「{raw}」。") from None
    return text


def build_request(
    detection: Detection,
    *,
    target_lang: str | None = None,
    source_lang: str | None = None,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The start request: unset fields get the app's saved values in the engine."""
    request: dict[str, Any] = {
        "surface": detection.key,
        "source_path": detection.source_path,
        "selected_paths": list(detection.selected_paths),
        "apply_saved_defaults": True,
    }
    if target_lang:
        request["target_lang"] = target_lang
    if source_lang:
        request["source_lang"] = source_lang
    request.update(request_fields(detection.descriptor, options or {}))
    return request


def apply_accept(request: dict[str, Any], accept: dict[str, Any] | None) -> dict[str, Any]:
    """Add the fields a ``NeedsConfirmation`` named, as a caller hands them back.

    Only fields that step past a stop are taken: ``confirmation_token`` and
    the request's ``allow_*`` switches.
    """
    if not accept:
        return request
    from api.schemas import TaskStartRequest

    allowed = {name for name in TaskStartRequest.model_fields if name.startswith("allow_")} | {"confirmation_token"}
    for key, value in accept.items():
        if key not in allowed:
            raise InputError(f"「{key}」不是可确认的项目。可确认的项目：{', '.join(sorted(allowed))}。")
        request[key] = str(value) if key == "confirmation_token" else bool(value)
    return request


def scan_request(detection: Detection) -> tuple[str, list[str]]:
    paths = list(detection.selected_paths) or [detection.source_path]
    return detection.key, paths


# ── start ────────────────────────────────────────────────────────────────


@dataclass
class Started:
    task: dict[str, Any]
    request: dict[str, Any]
    preflight: dict[str, Any]
    accepted: dict[str, Any]

    @property
    def task_id(self) -> str:
        return str(self.task.get("task_id") or "")


def start_task(
    client: EngineClient,
    request: dict[str, Any],
    *,
    accept_all: bool = False,
) -> Started:
    """Preflight, step past confirmations as allowed, then start.

    Without ``accept_all`` the first stop raises ``NeedsConfirmation`` with
    the fields that continue past it; calling again with those fields in
    ``request`` continues.  With ``accept_all`` each stop is accepted as it
    comes (``--yes``), at most ``MAX_CONFIRMATIONS`` of them.
    """
    current = dict(request)
    accepted: dict[str, Any] = {}
    for _round in range(MAX_CONFIRMATIONS + 1):
        try:
            preflight = client.preflight(current)
        except EngineError as exc:
            accept = exc.payload.get("accept") if isinstance(exc.payload, dict) else None
            if exc.status != 422 or not isinstance(accept, dict) or not accept:
                raise
            already = all(current.get(key) == value for key, value in accept.items())
            if not accept_all or already:
                raise NeedsConfirmation(exc.detail, reason=exc.reason, accept=accept) from None
            current.update(accept)
            accepted.update(accept)
            continue
        if preflight.get("requires_confirmation"):
            token = str(preflight.get("confirmation_token") or "")
            # A token from an earlier answer has expired by now; holding one
            # means the caller already said yes to sharing the connection.
            if not (accept_all or current.get("confirmation_token")):
                warnings = (preflight.get("risk") or {}).get("warnings") or []
                message = str(warnings[0]) if warnings else "此任务将与正在运行的任务共用同一条模型连接。"
                raise NeedsConfirmation(
                    message, reason="confirmation_required", accept={"confirmation_token": token}
                )
            current["confirmation_token"] = token
            accepted["confirmation_token"] = True
        else:
            current.pop("confirmation_token", None)
        task = client.start(current)
        sent = {key: value for key, value in current.items() if key != "confirmation_token"}
        return Started(task=task, request=sent, preflight=preflight, accepted=accepted)
    raise InputError("确认次数过多，任务没有开始。")


def dry_run(client: EngineClient, request: dict[str, Any]) -> dict[str, Any]:
    """Scan and preflight without starting anything (``translate --dry-run``).

    A stop that needs a confirmation is reported, not raised: a dry run's
    job is to say what a real run would run into.
    """
    surface = str(request["surface"])
    paths = list(request.get("selected_paths") or []) or [str(request["source_path"])]
    scanned = client.scan(surface, paths, apply_saved_defaults=True)
    report: dict[str, Any] = {
        "request": request,
        "files": [
            str(item.get("relative_path") or item.get("name") or item.get("path") or "")
            for item in scanned.get("items") or ()
            if isinstance(item, dict)
        ],
        "skipped": [item for item in scanned.get("skipped") or () if isinstance(item, dict)],
        "ready": False,
    }
    try:
        preflight = client.preflight(request)
    except EngineError as exc:
        accept = exc.payload.get("accept") if isinstance(exc.payload, dict) else None
        if exc.status != 422 or not isinstance(accept, dict) or not accept:
            report["problem"] = {"message": exc.detail, "reason": exc.reason}
            return report
        report["confirmation"] = NeedsConfirmation(exc.detail, reason=exc.reason, accept=accept).as_dict()
        return report
    report["options"] = preflight.get("options") or {}
    report["snapshot"] = preflight.get("candidate_snapshot") or {}
    if preflight.get("requires_confirmation"):
        warnings = (preflight.get("risk") or {}).get("warnings") or []
        report["confirmation"] = {
            "needs_confirmation": True,
            "reason": "confirmation_required",
            "message": str(warnings[0]) if warnings else "此任务将与正在运行的任务共用同一条模型连接。",
            "accept": {"confirmation_token": "<--yes>"},
        }
        return report
    report["ready"] = True
    return report


# ── progress and results ─────────────────────────────────────────────────


def progress_line(event: TaskEvent, last: dict[str, Any]) -> str | None:
    """A short human line for an event, or ``None`` when it adds nothing.

    ``last`` carries what was printed before, so a long task prints a line
    per phase and per five percent instead of one per batch.
    """
    data = event.data if isinstance(event.data, dict) else {}
    if event.type == "progress":
        percent = float(data.get("overall_percent") or 0.0)
        phase = str(data.get("phase_name") or "")
        if phase == last.get("phase") and percent - float(last.get("percent", -100)) < 5 and percent < 100:
            return None
        last.update(phase=phase, percent=percent)
        # Step counts are per-runner units (files, batches, permille), so only
        # the overall percentage is comparable across types.
        return f"[{percent:5.1f}%] {phase}".rstrip()
    if event.type == "file_progress":
        state = str(data.get("state") or "")
        name = str(data.get("name") or data.get("relative_path") or "")
        words = {"generated": "已生成", "failed": "未生成", "stopped": "已停止", "interrupted": "已中断"}
        if state not in words or last.setdefault("files", {}).get(name) == state:
            return None
        last["files"][name] = state
        return f"  {name}：{words[state]}"
    if event.type == "log":
        level = str(data.get("level") or "").upper()
        if level in {"WARN", "WARNING", "ERROR"}:
            return f"  {'警告' if level.startswith('WARN') else '错误'}：{data.get('message') or ''}"
    return None


def summarize(status: dict[str, Any]) -> dict[str, Any]:
    """The finished task as a caller needs it: where the files are, what to check."""
    result = status.get("result") if isinstance(status.get("result"), dict) else {}
    files = []
    for item in ((status.get("file_progress") or {}).get("files") or []):
        if not isinstance(item, dict):
            continue
        detail = item.get("result") if isinstance(item.get("result"), dict) else {}
        output = next(
            (str(detail[key]) for key in ("output", "output_path", "translated_image_path") if detail.get(key)),
            "",
        )
        files.append(
            {
                "name": str(item.get("name") or ""),
                "relative_path": str(item.get("relative_path") or ""),
                "state": str(item.get("state") or ""),
                "output": output,
            }
        )
    generated = sum(1 for item in files if item["state"] == "generated")
    state = str(status.get("state") or "")
    summary: dict[str, Any] = {
        "task_id": str(status.get("task_id") or ""),
        "surface": str(status.get("surface") or ""),
        "state": state,
        "terminal": bool(status.get("terminal")),
        "output_dir": str(result.get("output_dir") or ""),
        "files": files,
        "generated_count": generated,
        "failed_count": sum(1 for item in files if item["state"] in {"failed", "stopped", "interrupted", "unstarted"}),
        "review_count": review_count(result),
        "report_path": str(result.get("report_path") or result.get("summary_path") or ""),
        "review_dir": str(result.get("review_dir") or ""),
    }
    message = str(result.get("message") or "")
    if message:
        summary["message"] = message
    return summary


def task_status(status: dict[str, Any], *, logs: int = 5) -> dict[str, Any]:
    """``summarize`` plus where a running task is and its last log lines."""
    progress = status.get("progress") if isinstance(status.get("progress"), dict) else {}
    entries = status.get("logs") if isinstance(status.get("logs"), list) else []
    return {
        **summarize(status),
        "progress": progress,
        "logs": [str(entry.get("message") or "") for entry in entries[-max(1, logs):] if isinstance(entry, dict)],
    }


def exit_code_for(summary: dict[str, Any]) -> int:
    """0 when the task finished (some files may have failed), 1 when nothing came out."""
    if summary["state"] in {"done", "completed_with_issues"} and (
        summary["generated_count"] > 0 or not summary["files"]
    ):
        return EXIT_OK
    return EXIT_FAILED


_REVIEW_ROW_KEYS = ("review_items", "review_locations", "review_details", "issues")
_REVIEW_FILE_KEYS = ("file", "source_relative_path", "relative_path", "path")
_REVIEW_SECTION_KEYS = ("worksheet", "sheet", "sheet_name", "section", "chapter", "section_path", "heading")
_REVIEW_LOCATION_KEYS = (
    "cell", "cell_reference", "location_label", "location", "paragraph", "paragraph_index", "table_cell"
)
_REVIEW_TEXT_KEYS = ("excerpt", "snippet", "source_excerpt", "text", "source_text")


def review_count(result: dict[str, Any]) -> int | None:
    """Places still to check, counted the way the task center counts them
    (``reviewCount`` in ui/src/views/tasks.ts): review rows when the result
    lists them, else the totals the runner reported."""
    rows = _first_entries(result, _REVIEW_ROW_KEYS)
    review = result.get("review") if isinstance(result.get("review"), dict) else {}
    rows += _first_entries(review, ("items", "locations", "details", "issues"))
    if rows:
        merged: dict[tuple[str, ...], bool] = {}
        for row in rows:
            key = (
                _first_text(row, _REVIEW_FILE_KEYS),
                _first_text(row, _REVIEW_SECTION_KEYS),
                _first_text(row, _REVIEW_LOCATION_KEYS),
                _first_text(row, _REVIEW_TEXT_KEYS),
                _first_text(row, ("severity",)),
            )
            severity = key[-1]
            merged[key] = merged.get(key, False) or severity not in {"resolved", "info"}
        return sum(1 for pending in merged.values() if pending)
    counts = review.get("counts") if isinstance(review.get("counts"), dict) else {}
    if any(key in counts for key in ("needs_review", "resolved", "info")):
        return _int(counts.get("needs_review")) or 0
    merged_fields = {**result, **(result.get("summary") or {}), **(result.get("kpi") or {})}
    for source, keys in (
        (review, ("total_count",)),
        (merged_fields, ("review_count", "review_items_count", "review_total", "review_text_count")),
    ):
        for key in keys:
            value = _int(source.get(key))
            if value is not None:
                return value
    return None


def review_rows(result: dict[str, Any], *, limit: int = 50) -> list[dict[str, str]]:
    """The places to check, one row each (the rows ``review_count`` counts)."""
    rows = _first_entries(result, _REVIEW_ROW_KEYS)
    review = result.get("review") if isinstance(result.get("review"), dict) else {}
    rows += _first_entries(review, ("items", "locations", "details", "issues"))
    found: dict[tuple[str, ...], dict[str, str]] = {}
    for row in rows:
        if _first_text(row, ("severity",)) in {"resolved", "info"}:
            continue
        item = {
            "file": _first_text(row, _REVIEW_FILE_KEYS),
            "section": _first_text(row, _REVIEW_SECTION_KEYS),
            "location": _first_text(row, _REVIEW_LOCATION_KEYS),
            "text": _first_text(row, _REVIEW_TEXT_KEYS),
            "reason": _first_text(row, ("message", "reason", "category", "type")),
        }
        found.setdefault((item["file"], item["section"], item["location"], item["text"]), item)
    return [{key: value for key, value in item.items() if value} for item in found.values()][:limit]


def _first_entries(source: dict[str, Any], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    for key in keys:
        value = source.get(key)
        if isinstance(value, list) and value:
            return [item for item in value if isinstance(item, dict)]
    return []


def _first_text(source: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = source.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None
