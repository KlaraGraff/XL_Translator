"""User feedback: build the package a user chose to send, preview it, send it.

Nothing here runs on its own. A package is built only when the user opens the
feedback page and asks for a preview, a ZIP, or a send, and it holds exactly
what they ticked:

* their own text (category, description, optional contact) and any
  screenshots they pasted into the description;
* optionally the anonymous app/system summary the diagnostics module already
  produces (version, OS, model connection type, never keys or endpoints);
* optionally the activity log of chosen tasks. Those logs are already stripped
  of credentials, absolute paths and quoted content when they are written;
  here every file name is additionally replaced with 文件1, 文件2 … because
  names often carry a project or client name;
* optionally the source files and/or generated files of the chosen tasks
  (see ``task_files``), and files they picked themselves. None by default.

``start_send`` posts the ZIP to the feedback relay (``feedback-relay/`` in the repo).
When no relay is configured, or it cannot be reached, the UI offers the same
ZIP to save locally, so a user is never left with nothing to do.
"""

from __future__ import annotations

import io
import json
import logging
import os
import platform
import re
import threading
import time
import traceback
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from app_meta import APP_UPDATE_USER_AGENT, APP_VERSION, FEEDBACK_ENDPOINT
from core import diagnostics, system_proxy
from core.task_logger import app_event_logger, redact_absolute_paths
import settings as settings_module

FEEDBACK_CATEGORIES = ("problem", "suggestion", "other")
MAX_DESCRIPTION_CHARS = 5_000
MAX_CONTACT_CHARS = 200
# 只附任务、没写描述时 issue 里的描述（中转站要求描述非空）。
NO_DESCRIPTION = "（用户未填写描述，请查看所附任务的日志。）"
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
MAX_TASKS = 5
# 用户自己另外挑的文件最多 10 个；加上所选任务的原文件和译文，整包最多 50 个。
MAX_PICKED_FILES = 10
# 描述框里粘贴的截图：不限张数，单张和合计都不超过 10 MB。截图的额度和文件分开算，
# 文件再多也挤不掉截图。
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGES_TOTAL_BYTES = 10 * 1024 * 1024
MAX_FILES = 50
# 附件文件（所选任务的原文件、译文和用户另挑的文件）原始大小上限，截图另算。中转站
# 把包分段存进 Cloudflare KV（单个值上限 25 MiB，免费额度共 1 GB、最长保留 90 天）；
# 文件 40 MB + 截图 10 MB，整包上限再给 ZIP 头和日志留出余量，须与
# feedback-relay/src/index.js 的 MAX_PACKAGE_BYTES 一致。
MAX_ATTACHMENT_BYTES = 40 * 1024 * 1024
MAX_PACKAGE_BYTES = 54 * 1024 * 1024
# 任务中心最多保留 200 条记录；界面上再按搜索和条数筛选。
RECENT_TASK_LIMIT = 200

_ENDPOINT_ENV = "TRANSLATOR_FEEDBACK_ENDPOINT"
_SEND_TIMEOUT = httpx.Timeout(120.0, connect=15.0)

# 日志里剩下的、看起来像文档文件名的片段（不在本次任务文件清单里的，比如输出文件
# 改过名、旧版本写下的行）。宁可多遮一点，也不让客户或项目名漏出去。
_NAME_STOP = r"\s\\/:*?\"<>|，。；：、「」『』（）()\[\]"
_FILE_NAME_RE = re.compile(
    rf"(?<![^{_NAME_STOP}])[^{_NAME_STOP}\x00]+"
    r"\.(?:xlsx|xlsm|xls|docx|docm|doc|pdf|dwg|dxf|png|jpe?g|tiff?|bmp|webp|zip|csv|txt)\b",
    re.IGNORECASE,
)
_PATH_LIKE_KEYS = ("path", "paths", "dir", "dirs")
_DROPPED_TASK_KEYS = {"logs", "source_label", "local_operations", "model_snapshot"}


class FeedbackError(ValueError):
    """A request the user can fix (or route around by saving a ZIP).

    ``log_detail`` is what app.log gets about a failed send (stage, HTTP
    status, exception); it never carries the user's text, address or files.
    """

    def __init__(self, message: str, *, reason: str, log_detail: str = "") -> None:
        super().__init__(message)
        self.reason = reason
        self.log_detail = log_detail


@dataclass
class FeedbackDraft:
    category: str
    description: str
    contact: str = ""
    include_system: bool = True
    ui_language: str = "zh"
    # Task-center records (as returned by the task manager, logs included).
    task_records: list[dict[str, Any]] = field(default_factory=list)
    file_paths: list[str] = field(default_factory=list)
    # Screenshots pasted into the description, raw image bytes in paste order.
    images: list[bytes] = field(default_factory=list)


_IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
)


def image_names(images: list[bytes]) -> list[str]:
    """Package names for pasted screenshots (截图1.png …); rejects anything that is not an image."""
    names: list[str] = []
    for index, data in enumerate(images, start=1):
        if len(data) > MAX_IMAGE_BYTES:
            raise FeedbackError("单张图片不能超过 10 MB。", reason="image_too_large")
        suffix = next((ext for magic, ext in _IMAGE_SIGNATURES if data.startswith(magic)), "")
        if not suffix and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            suffix = ".webp"
        if not suffix:
            raise FeedbackError("只能粘贴 PNG、JPEG、GIF 或 WebP 图片。", reason="invalid_image")
        names.append(f"截图{index}{suffix}")
    return names


def feedback_endpoint() -> str:
    """Relay base URL; empty means online sending is not available in this build."""
    value = os.environ.get(_ENDPOINT_ENV, "").strip() or FEEDBACK_ENDPOINT
    return value.rstrip("/")


def recent_tasks(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The finished tasks a user may attach, newest first, with local labels only."""
    result: list[dict[str, Any]] = []
    for record in records:
        if not record.get("terminal"):
            continue
        result.append(
            {
                "task_id": str(record.get("task_id") or ""),
                "surface": str(record.get("surface") or ""),
                "state": str(record.get("state") or ""),
                # 只在本机界面上帮用户认出是哪一次；不会进反馈包。
                "source_label": str(record.get("source_label") or ""),
                "updated_at": record.get("updated_at") or record.get("created_at") or 0,
            }
        )
        if len(result) >= RECENT_TASK_LIMIT:
            break
    return result


def validate_draft(draft: FeedbackDraft) -> list[Path]:
    """Check the draft and return the attached files as resolved paths."""
    if draft.category not in FEEDBACK_CATEGORIES:
        raise FeedbackError("反馈类型无效。", reason="invalid_category")
    # 「反馈此任务」附上了出问题的任务时，日志本身就说明了问题，描述可以不写。
    if not draft.description.strip() and not draft.task_records:
        raise FeedbackError("请填写反馈内容。", reason="empty_description")
    if len(draft.description) > MAX_DESCRIPTION_CHARS:
        raise FeedbackError("反馈内容过长，请精简到 5000 字以内。", reason="description_too_long")
    if len(draft.contact) > MAX_CONTACT_CHARS:
        raise FeedbackError("邮箱过长。", reason="contact_too_long")
    if len(draft.task_records) > MAX_TASKS:
        raise FeedbackError("一次最多附上 5 次任务的日志。", reason="too_many_tasks")
    if len(draft.file_paths) > MAX_FILES:
        raise FeedbackError("一次最多附上 50 个文件，请少选几次任务或不附原文件。", reason="too_many_files")
    files: list[Path] = []
    total = 0
    for raw in draft.file_paths:
        path = Path(str(raw)).expanduser()
        if not path.is_file():
            raise FeedbackError(f"找不到文件：{path.name}", reason="file_missing")
        total += path.stat().st_size
        files.append(path)
    if total > MAX_ATTACHMENT_BYTES:
        raise FeedbackError("附件文件合计超过 40 MB，请只附上必要的文件。", reason="too_large")
    image_names(draft.images)
    if sum(len(data) for data in draft.images) > MAX_IMAGES_TOTAL_BYTES:
        raise FeedbackError("截图合计超过 10 MB，请删掉几张。", reason="images_too_large")
    return files


def file_info(raw: str) -> dict[str, Any]:
    path = Path(str(raw)).expanduser()
    exists = path.is_file()
    return {
        "path": str(raw),
        "name": path.name,
        "bytes": path.stat().st_size if exists else 0,
        "exists": exists,
    }


_OUTPUT_KEYS = ("output_path", "result_path", "output", "translated_image_path", "translated_path")
_COMPRESSED_KEYS = ("compressed_output", "compressed_output_path", "compressed_pdf_path")


def task_files(record: dict[str, Any], sources: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Files one task touched that the user may tick: its source and generated files.

    ``record`` is the task-center status (with result); ``sources`` maps file ids
    to source paths and only exists while the task is still held in memory
    (history never keeps source paths). Nothing is read here, only listed.
    """
    sources = sources or {}
    result = record.get("result") if isinstance(record.get("result"), dict) else {}
    terminal: dict[str, dict[str, Any]] = {}
    loose: list[dict[str, Any]] = []
    for key in ("file_results", "files", "file_records"):
        for entry in (result.get(key) if isinstance(result, dict) else None) or []:
            if not isinstance(entry, dict):
                continue
            file_id = str(entry.get("file_id") or "")
            if file_id:
                terminal[file_id] = {**terminal.get(file_id, {}), **entry}
            else:
                loose.append(entry)
    progress = record.get("file_progress")
    progress_files = progress.get("files") if isinstance(progress, dict) else None
    entries: list[tuple[str, dict[str, Any]]] = []
    for item in progress_files or []:
        if isinstance(item, dict):
            file_id = str(item.get("file_id") or "")
            merged = {**(item.get("result") if isinstance(item.get("result"), dict) else {}), **terminal.pop(file_id, {})}
            entries.append((file_id, merged))
    entries.extend((file_id, entry) for file_id, entry in terminal.items())
    entries.extend(("", entry) for entry in loose)

    found: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(raw: Any, kind: str) -> None:
        text = str(raw or "").strip()
        if not text or text in seen or text.startswith("["):
            return
        seen.add(text)
        info = file_info(text)
        if info["exists"]:
            found.append({**info, "kind": kind})

    for file_id, entry in entries:
        add(sources.get(file_id), "source")
        add(next((entry.get(key) for key in _OUTPUT_KEYS if entry.get(key)), ""), "output")
        add(next((entry.get(key) for key in _COMPRESSED_KEYS if entry.get(key)), ""), "compressed")
    return found


class _NameMasker:
    """Replace one task's file names with stable placeholders (文件1, 文件2 …)."""

    def __init__(self, names: list[str]) -> None:
        self._replacements: list[tuple[str, str]] = []
        seen: dict[str, str] = {}
        for name in names:
            clean = str(name or "").strip()
            if not clean or clean in seen:
                continue
            label = f"文件{len(seen) + 1}"
            seen[clean] = label
            suffix = Path(clean).suffix
            self._replacements.append((clean, f"{label}{suffix}"))
            stem = Path(clean).stem
            # 输出文件名一般是「原名_译文.xlsx」之类，单遮完整文件名会漏掉它们。
            if len(stem) >= 2 and stem != clean:
                self._replacements.append((stem, label))
        # 长的先换：「报价单_2026」要在「报价单」之前处理。
        self._replacements.sort(key=lambda item: len(item[0]), reverse=True)

    def __call__(self, text: str) -> str:
        result = redact_absolute_paths(str(text or ""))
        # 先换成不会被下面的正则认成文件名的记号，兜底遮完再换回「文件1.xlsx」。
        for index, (original, _label) in enumerate(self._replacements):
            result = result.replace(original, f"\x00{index}\x00")
        result = _FILE_NAME_RE.sub("[文件名]", result)
        for index, (_original, label) in enumerate(self._replacements):
            result = result.replace(f"\x00{index}\x00", label)
        return result


def _task_file_names(record: dict[str, Any]) -> list[str]:
    names: list[str] = []
    progress = record.get("file_progress")
    files = progress.get("files") if isinstance(progress, dict) else None
    for item in files or []:
        if isinstance(item, dict):
            names.append(str(item.get("name") or ""))
    result = record.get("result")
    for item in (result.get("file_results") if isinstance(result, dict) else None) or []:
        if isinstance(item, dict):
            for key in ("name", "file_name", "source_name"):
                if item.get(key):
                    names.append(str(item[key]))
    return names


def _scrub(value: Any, masker: _NameMasker, *, key: str = "") -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for child_key, child in value.items():
            name = str(child_key)
            lowered = name.lower()
            if lowered in _DROPPED_TASK_KEYS or lowered.endswith(_PATH_LIKE_KEYS):
                continue
            result[name] = _scrub(child, masker, key=lowered)
        return result
    if isinstance(value, (list, tuple)):
        return [_scrub(item, masker, key=key) for item in value]
    if isinstance(value, str):
        return masker(value)
    return value


def task_log_text(record: dict[str, Any]) -> str:
    """The activity log exactly as it goes into the package."""
    masker = _NameMasker(_task_file_names(record))
    lines = []
    for entry in record.get("logs") or []:
        if not isinstance(entry, dict):
            continue
        level = str(entry.get("level") or "INFO").upper()
        lines.append(f"[{level}] {masker(str(entry.get('message') or ''))}")
    return "\n".join(lines)


def _task_summary(record: dict[str, Any]) -> dict[str, Any]:
    masker = _NameMasker(_task_file_names(record))
    return diagnostics._redact(_scrub(record, masker))


def _hardware_summary() -> dict[str, Any]:
    """Processor architecture, core count and memory: enough to spot an underpowered machine."""
    summary: dict[str, Any] = {"machine": platform.machine(), "cpu_count": os.cpu_count() or 0}
    try:
        import psutil

        memory = psutil.virtual_memory()
        summary["memory_gb"] = round(memory.total / 1024**3, 1)
        summary["memory_available_gb"] = round(memory.available / 1024**3, 1)
    except Exception:  # noqa: BLE001 - hardware details are a nice-to-have
        pass
    return summary


def _system_summary(settings: Any) -> dict[str, Any]:
    return {
        **diagnostics._build_runtime_payload(),
        "hardware": _hardware_summary(),
        "connection_summary": diagnostics._connection_summary(settings),
    }


def _task_diagnostic_files(task_id: str) -> dict[str, bytes]:
    """The task's anonymous diagnostic record, if one was archived."""
    locator = diagnostics._anonymous_locator(task_id)
    for item in diagnostics.list_diagnostic_records():
        if str(item.get("anonymous_locator") or "") != locator:
            continue
        root = Path(str(item.get("record_dir") or ""))
        files: dict[str, bytes] = {}
        for path in sorted(root.rglob("*")) if root.is_dir() else []:
            if path.is_file():
                files[path.relative_to(root).as_posix()] = path.read_bytes()
        return files
    return {}


def _feedback_payload(draft: FeedbackDraft, files: list[Path]) -> dict[str, Any]:
    return {
        "category": draft.category,
        "description": draft.description.strip() or NO_DESCRIPTION,
        "contact": draft.contact.strip(),
        "app_version": APP_VERSION,
        "ui_language": draft.ui_language,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "includes": {
            "system": draft.include_system,
            "task_logs": len(draft.task_records),
            "files": [path.name for path in files],
            "images": len(draft.images),
        },
    }


def build_package(draft: FeedbackDraft, *, settings: Any) -> tuple[bytes, dict[str, Any]]:
    """Return the ZIP bytes and the feedback.json payload inside it."""
    files = validate_draft(draft)
    payload = _feedback_payload(draft, files)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("feedback.json", _json_bytes(payload))
        if draft.include_system:
            archive.writestr("system.json", _json_bytes(_system_summary(settings)))
        for index, record in enumerate(draft.task_records, start=1):
            surface = re.sub(r"[^a-z_]", "", str(record.get("surface") or "task").lower()) or "task"
            folder = f"tasks/{index}-{surface}"
            archive.writestr(f"{folder}/task.json", _json_bytes(_task_summary(record)))
            archive.writestr(f"{folder}/log.txt", task_log_text(record).encode("utf-8"))
            for name, content in _task_diagnostic_files(str(record.get("task_id") or "")).items():
                archive.writestr(f"{folder}/diagnostic/{name}", content)
        for name, data in zip(image_names(draft.images), draft.images, strict=True):
            archive.writestr(f"images/{name}", data)
        used: set[str] = set()
        for path in files:
            archive.write(path, f"files/{_unique_name(path.name, used)}")
    data = buffer.getvalue()
    if len(data) > MAX_PACKAGE_BYTES:
        raise FeedbackError("附件总大小超过 50 MB，请只附上必要的文件。", reason="too_large")
    return data, payload


def preview(draft: FeedbackDraft, *, settings: Any) -> dict[str, Any]:
    """List what would be sent, with each task log in full so the user can read it."""
    data, payload = build_package(draft, settings=settings)
    tasks = [
        {
            "task_id": str(record.get("task_id") or ""),
            "surface": str(record.get("surface") or ""),
            "state": str(record.get("state") or ""),
            "log": task_log_text(record),
        }
        for record in draft.task_records
    ]
    files = [
        {"name": Path(raw).name, "bytes": Path(raw).expanduser().stat().st_size}
        for raw in draft.file_paths
    ]
    return {
        "feedback": payload,
        "system": _system_summary(settings) if draft.include_system else None,
        "tasks": tasks,
        "files": files,
        "images": [
            {"name": name, "bytes": len(image)}
            for name, image in zip(image_names(draft.images), draft.images, strict=True)
        ],
        "package_bytes": len(data),
        "endpoint_configured": bool(feedback_endpoint()),
    }


def package_filename(now: datetime | None = None) -> str:
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return f"translator-feedback-{stamp}.zip"


def start_send(draft: FeedbackDraft, *, settings: Any) -> dict[str, Any]:
    """Build the package, then upload it in the background; poll ``send_status``.

    Problems the user can fix (empty text, too large, task gone) and a missing
    relay are raised here, before anything is uploaded. The upload itself runs
    on a thread so the page can show how many bytes have actually gone out.
    """
    job_id = uuid.uuid4().hex
    tag = job_id[:8]
    started_at = time.monotonic()
    # 只记数量和有没有填邮箱；描述、邮箱、文件名一概不进日志。
    _log(
        logging.INFO,
        f"反馈发送开始 | 发送={tag} | 版本={APP_VERSION} | 类型={draft.category} | "
        f"任务日志={len(draft.task_records)} | 文件={len(draft.file_paths)} | 截图={len(draft.images)} | "
        f"系统信息={'是' if draft.include_system else '否'} | 邮箱={'已填' if draft.contact.strip() else '未填'}",
    )
    endpoint = feedback_endpoint()
    try:
        if not endpoint:
            raise FeedbackError("此版本尚未开通在线发送，请保存为 ZIP。", reason="not_configured")
        data, payload = build_package(draft, settings=settings)
    except FeedbackError as exc:
        _log(logging.WARNING, f"反馈发送未开始 | 发送={tag} | 阶段=打包 | 原因={exc.reason} | {_elapsed(started_at)}")
        raise
    except Exception as exc:
        _log(logging.ERROR, f"反馈发送未开始 | 发送={tag} | 阶段=打包 | 异常={_error_text(exc)} | {_elapsed(started_at)}")
        raise
    meta = {
        **payload,
        "platform": diagnostics._runtime_platform_label(),
        "package_bytes": len(data),
    }
    _log(
        logging.INFO,
        f"反馈开始上传 | 发送={tag} | 包大小={_size_text(len(data))} | 代理={_proxy_label(endpoint)} | {_elapsed(started_at)}",
    )
    job = _SendJob(total=len(data), tag=tag, started_at=started_at)
    with _jobs_lock:
        finished = [key for key, item in _jobs.items() if item.state != "uploading"]
        for key in finished[: max(0, len(finished) - _KEEP_FINISHED_JOBS)]:
            del _jobs[key]
        _jobs[job_id] = job
        started = {"job_id": job_id, **job.snapshot()}
    threading.Thread(
        target=_run_send_job,
        args=(job, endpoint, data, meta, draft.contact),
        name="feedback-send",
        daemon=True,
    ).start()
    return started


def send_status(job_id: str) -> dict[str, Any] | None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        return job.snapshot() if job else None


@dataclass
class _SendJob:
    total: int
    # 日志里串起同一次发送的几行（job_id 的前 8 位），以及从点「发送」算起的耗时。
    tag: str = ""
    started_at: float = 0.0
    sent: int = 0
    # uploading → done / error
    state: str = "uploading"
    reference: str = ""
    message: str = ""
    reason: str = ""

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "sent": self.sent,
            "total": self.total,
            "id": self.reference,
            "message": self.message,
            "reason": self.reason,
        }


_jobs: dict[str, _SendJob] = {}
_jobs_lock = threading.Lock()
_KEEP_FINISHED_JOBS = 20


def _run_send_job(job: _SendJob, endpoint: str, data: bytes, meta: dict[str, Any], contact: str = "") -> None:
    def progress(sent: int) -> None:
        with _jobs_lock:
            job.sent = sent

    def failed(detail: str) -> None:
        with _jobs_lock:
            sent = job.sent
        _log(
            logging.WARNING,
            f"反馈发送失败 | 发送={job.tag} | {detail} | 已上传={_size_text(sent)}/{_size_text(job.total)} | "
            f"{_elapsed(job.started_at)}",
        )

    try:
        reference = _upload(endpoint, data, meta, on_progress=progress, tag=job.tag)
    except FeedbackError as exc:
        failed(f"原因={exc.reason} | {exc.log_detail or '阶段=未知'}")
        with _jobs_lock:
            job.state, job.message, job.reason = "error", str(exc), exc.reason
    except Exception as exc:  # noqa: BLE001 - 线程里的意外不能让界面一直停在「正在上传」
        failed(f"原因=server | 阶段=程序内部 | 异常={_error_text(exc)}")
        with _jobs_lock:
            job.state, job.reason = "error", "server"
            job.message = "反馈服务暂时不可用，请稍后重试，或保存为 ZIP。"
    else:
        _log(
            logging.INFO,
            f"反馈发送成功 | 发送={job.tag} | 编号={_safe_token(reference, 40)} | 包大小={_size_text(job.total)} | "
            f"{_elapsed(job.started_at)}",
        )
        # 先记邮箱再报完成：界面看到「已发送」时，下一条反馈已经能带上它。
        remember_email(contact)
        with _jobs_lock:
            job.state, job.reference, job.sent = "done", reference, job.total


def saved_contact_path() -> Path:
    # 读得晚：测试会把 settings 模块的数据目录指到临时目录。
    return settings_module.APP_DATA_DIR / "feedback_contact.json"


def _saved_contact() -> dict[str, Any]:
    try:
        data = json.loads(saved_contact_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def saved_email() -> str:
    """The email the user left on their last sent feedback, or ""."""
    email = _saved_contact().get("email")
    return email if isinstance(email, str) and _EMAIL_RE.match(email) else ""


def should_ask_for_email() -> bool:
    """Whether to ask once before sending without an email.

    Only until the user has sent a feedback without one: by then they have
    made their choice and are not asked again.
    """
    return not saved_email() and not _saved_contact().get("sent_without_email")


def remember_email(contact: str) -> None:
    """Keep the address of a sent feedback on this machine.

    Only a successful send calls this, so an address that never left the
    machine (a draft, a saved ZIP) is not kept. Sending without an address
    forgets the old one and records that the user chose not to leave one, so
    they are asked only once. Failing to write just means typing it again.
    """
    email = contact.strip()
    if email and not _EMAIL_RE.match(email):
        return
    path = saved_contact_path()
    record = {"email": email} if email else {"sent_without_email": True}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    except OSError as exc:
        _log(logging.WARNING, f"反馈已发出，但没能在本机记下邮箱选择 | 异常={_error_text(exc)}")


class _CountingReader(io.BytesIO):
    """The package as a file, so httpx streams it in chunks we can count.

    httpx pulls the next chunk only after the previous one was written to the
    connection, so the position *before* each read is what has really gone out;
    nothing is estimated.
    """

    def __init__(self, data: bytes, on_progress: Callable[[int], None]) -> None:
        super().__init__(data)
        self._on_progress = on_progress

    def read(self, size: int | None = -1) -> bytes:
        self._on_progress(self.tell())
        return super().read(size)


def _upload(
    endpoint: str,
    data: bytes,
    meta: dict[str, Any],
    *,
    on_progress: Callable[[int], None] | None = None,
    tag: str = "-",
) -> str:
    """Post the package to the relay and return its reference number.

    When the system proxy cannot even be connected to (often one left behind
    by a VPN or proxy app that is no longer running), the package is sent once
    more directly. Only connection-stage errors qualify: nothing reached the
    relay yet, so it cannot be received twice.
    """
    package: bytes | _CountingReader = _CountingReader(data, on_progress) if on_progress else data
    direct = False
    try:
        response = _post(endpoint, package, meta)
    except httpx.HTTPError as exc:
        if not system_proxy.should_retry_direct(exc, endpoint):
            raise _network_error(exc, package, data) from exc
        _log(logging.INFO, f"反馈经系统代理连不上，已改直连重试 | 发送={tag} | 代理那次={_error_text(exc)}")
        if isinstance(package, _CountingReader):
            package.seek(0)
        direct = True
        try:
            response = _post(endpoint, package, meta, direct=True)
        except httpx.HTTPError as retry_exc:
            raise _network_error(retry_exc, package, data, retried=True) from retry_exc
        _log(logging.INFO, f"反馈直连重试已连上 | 发送={tag} | HTTP={response.status_code}")
    try:
        body = response.json()
    except ValueError:
        body = {}
    detail = f"阶段=服务器返回 | {_response_text(response, body)}" + (" | 已改直连重试" if direct else "")
    if response.status_code == 413:
        raise FeedbackError("附件总大小超过 50 MB，请只附上必要的文件。", reason="too_large", log_detail=detail)
    if response.status_code == 429:
        raise FeedbackError("发送过于频繁，请稍后重试，或保存为 ZIP。", reason="rate_limited", log_detail=detail)
    reference = str(body.get("id") or "") if isinstance(body, dict) else ""
    if response.status_code != 200 or not reference:
        raise FeedbackError("反馈服务暂时不可用，请稍后重试，或保存为 ZIP。", reason="server", log_detail=detail)
    return reference


def _post(endpoint: str, package: bytes | _CountingReader, meta: dict[str, Any], *, direct: bool = False) -> httpx.Response:
    with _open_client(direct=direct) as client:
        return client.post(
            f"{endpoint}/v1/feedback",
            data={"meta": json.dumps(meta, ensure_ascii=False)},
            files={"package": (package_filename(), package, "application/zip")},
            headers={"User-Agent": f"{APP_UPDATE_USER_AGENT}/{APP_VERSION}"},
        )


def _network_error(
    exc: httpx.HTTPError, package: bytes | _CountingReader, data: bytes, *, retried: bool = False
) -> FeedbackError:
    # 断在哪一步：连不上、传到一半、还是传完了等不到服务器回话。
    uploaded = package.tell() >= len(data) if isinstance(package, _CountingReader) else None
    return FeedbackError(
        "发送超时，请检查网络后重试，或保存为 ZIP。"
        if isinstance(exc, httpx.TimeoutException)
        else "无法连接反馈服务，请检查网络后重试，或保存为 ZIP。",
        reason="network",
        log_detail=f"阶段={_network_stage(exc, uploaded)} | 异常={_error_text(exc)}"
        + (" | 已改直连重试，仍失败" if retried else ""),
    )


# ── app.log：每次发送留下开始、成功或失败，只记排查用得上、又不涉及隐私的信息 ──────────


def _log(level: int, message: str) -> None:
    try:
        app_event_logger("feedback").log(level, message)
    except Exception:  # noqa: BLE001 - 写不了日志也不能挡住发送
        pass


def _elapsed(started_at: float) -> str:
    return f"耗时={time.monotonic() - started_at:.1f}s"


def _size_text(size: int) -> str:
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / 1024 / 1024:.1f} MB"


def _safe_token(value: Any, limit: int) -> str:
    """Relay-side codes (FB 编号、error 字段) are short identifiers; keep only that shape."""
    return re.sub(r"[^A-Za-z0-9_.:-]", "", str(value or ""))[:limit] or "-"


_SECRET_RE = re.compile(r"(?i)(?:bearer\s+|sk-[a-z0-9_-]{8,}|(?:api[_ -]?key|token|password)\s*[:=]\s*)[^\s,;]+")
_CREDENTIALS_IN_URL_RE = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://)[^/\s@]+@")


def _error_text(exc: BaseException) -> str:
    """Exception type, a cleaned message and where it was raised.

    httpx wraps the socket / TLS error, so the chained causes are listed too:
    「ConnectError ← SSLCertVerificationError」 says far more than either alone.
    Absolute paths, document file names and anything that looks like a
    credential are removed; the message is cut short.
    """
    names: list[str] = []
    current: BaseException | None = exc
    for _depth in range(6):
        if current is None:
            break
        # httpx 和 httpcore 的异常同名，连着一样的只记一次。
        if not names or names[-1] != type(current).__name__:
            names.append(type(current).__name__)
        current = current.__cause__ or current.__context__
    message = _CREDENTIALS_IN_URL_RE.sub(r"\1", str(exc))
    message = _SECRET_RE.sub("[redacted]", message)
    message = _FILE_NAME_RE.sub("[文件名]", redact_absolute_paths(message))
    message = " ".join(message.split())[:300]
    text = " ← ".join(names) + (f": {message}" if message else "")
    # 程序内部的意外才需要知道断在哪一行；网络错误的位置总在 httpx 里，没有信息量。
    frames = traceback.extract_tb(exc.__traceback__) if exc.__traceback__ and not isinstance(exc, httpx.HTTPError) else []
    if frames:
        text += f" @ {Path(frames[-1].filename).name}:{frames[-1].lineno}"
    return text


def _network_stage(exc: httpx.HTTPError, uploaded: bool | None = None) -> str:
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)):
        return "连接"
    if uploaded is not None:
        return "等待服务器确认" if uploaded else "上传"
    if isinstance(exc, (httpx.WriteError, httpx.WriteTimeout)):
        return "上传"
    if isinstance(exc, (httpx.ReadError, httpx.ReadTimeout, httpx.RemoteProtocolError)):
        return "等待服务器确认"
    return "网络"


def _response_text(response: httpx.Response, body: Any) -> str:
    """Status, the relay's own error code, or what kind of page answered instead."""
    parts = [f"HTTP={response.status_code}"]
    if isinstance(body, dict) and body.get("error"):
        parts.append(f"服务器错误={_safe_token(body.get('error'), 40)}")
    elif not isinstance(body, dict) or not body:
        # 不是中转站的 JSON：多半是代理、网关或拦截页答的话，记下内容类型就能看出来。
        content_type = response.headers.get("content-type", "").split(";")[0].strip()
        parts.append(f"非中转站应答={_safe_token(content_type, 60)}")
    location = response.headers.get("location")
    if location:
        parts.append(f"跳转到={_safe_token(httpx.URL(location).host, 80) if '://' in location else '相对地址'}")
    if response.headers.get("server"):
        parts.append(f"应答方={_safe_token(response.headers['server'], 40)}")
    return " | ".join(parts)


def _proxy_label(endpoint: str) -> str:
    """Whether httpx will go through a proxy (it reads the same settings), without its address."""
    try:
        url = system_proxy.configured_proxy(endpoint)
        if url and system_proxy.bypasses_proxy(endpoint):
            return "无（不走代理）"
    except Exception:  # noqa: BLE001 - 只是为了日志
        return "未知"
    if not url:
        return "无"
    scheme = url.split("://", 1)[0].lower() if "://" in url else "http"
    try:
        proxy_host = httpx.URL(url if "://" in url else f"http://{url}").host
    except Exception:  # noqa: BLE001
        proxy_host = ""
    where = "本机" if proxy_host in {"127.0.0.1", "localhost", "::1"} else "其他机器"
    return f"有（{_safe_token(scheme, 10)}，{where}）"


def _open_client(*, direct: bool = False) -> httpx.Client:
    # 默认跟随系统代理（httpx 读 HTTPS_PROXY 等环境变量和系统代理设置），与检查更新一致；
    # direct=True 是系统代理连不上时的那一次直连重试。
    return httpx.Client(timeout=_SEND_TIMEOUT, follow_redirects=False, trust_env=not direct)


def _unique_name(name: str, used: set[str]) -> str:
    candidate = name or "file"
    stem, suffix = Path(candidate).stem, Path(candidate).suffix
    counter = 2
    while candidate in used:
        candidate = f"{stem} ({counter}){suffix}"
        counter += 1
    used.add(candidate)
    return candidate


def _json_bytes(value: Any) -> bytes:
    return json.dumps(diagnostics._json_safe(value), ensure_ascii=False, indent=2).encode("utf-8")
