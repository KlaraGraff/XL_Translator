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
import os
import platform
import re
import threading
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from app_meta import APP_UPDATE_USER_AGENT, APP_VERSION, FEEDBACK_ENDPOINT
from core import diagnostics
from core.task_logger import redact_absolute_paths
import settings as settings_module

FEEDBACK_CATEGORIES = ("problem", "suggestion", "other")
MAX_DESCRIPTION_CHARS = 5_000
MAX_CONTACT_CHARS = 200
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
MAX_TASKS = 5
# 用户自己另外挑的文件最多 10 个；加上所选任务的原文件和译文，整包最多 50 个。
MAX_PICKED_FILES = 10
# 描述框里粘贴的截图。
MAX_IMAGES = 6
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_FILES = 50
# 附件（用户自己挑的文件）原始大小上限。中转站把包分段存进 Cloudflare KV（单个值
# 上限 25 MiB，免费额度共 1 GB、保留 90 天），整包上限另给 ZIP 头和日志留出余量，
# 须与 feedback-relay/src/index.js 的 MAX_PACKAGE_BYTES 一致。
MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
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
    """A request the user can fix (or route around by saving a ZIP)."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


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
    if len(images) > MAX_IMAGES:
        raise FeedbackError("最多粘贴 6 张图片。", reason="too_many_images")
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
    if not draft.description.strip():
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
    image_names(draft.images)
    total += sum(len(data) for data in draft.images)
    if total > MAX_ATTACHMENT_BYTES:
        raise FeedbackError("附件总大小超过 50 MB，请只附上必要的文件。", reason="too_large")
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
        "description": draft.description.strip(),
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
    endpoint = feedback_endpoint()
    if not endpoint:
        raise FeedbackError("此版本尚未开通在线发送，请保存为 ZIP。", reason="not_configured")
    data, payload = build_package(draft, settings=settings)
    meta = {
        **payload,
        "platform": diagnostics._runtime_platform_label(),
        "package_bytes": len(data),
    }
    job_id = uuid.uuid4().hex
    job = _SendJob(total=len(data))
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

    try:
        reference = _upload(endpoint, data, meta, on_progress=progress)
    except FeedbackError as exc:
        with _jobs_lock:
            job.state, job.message, job.reason = "error", str(exc), exc.reason
    except Exception:  # noqa: BLE001 - 线程里的意外不能让界面一直停在「正在上传」
        with _jobs_lock:
            job.state, job.reason = "error", "server"
            job.message = "反馈服务暂时不可用，请稍后重试，或保存为 ZIP。"
    else:
        # 先记邮箱再报完成：界面看到「已发送」时，下一条反馈已经能带上它。
        remember_email(contact)
        with _jobs_lock:
            job.state, job.reference, job.sent = "done", reference, job.total


def saved_contact_path() -> Path:
    # 读得晚：测试会把 settings 模块的数据目录指到临时目录。
    return settings_module.APP_DATA_DIR / "feedback_contact.json"


def saved_email() -> str:
    """The email the user left on their last sent feedback, or ""."""
    try:
        data = json.loads(saved_contact_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    email = data.get("email") if isinstance(data, dict) else None
    return email if isinstance(email, str) and _EMAIL_RE.match(email) else ""


def remember_email(contact: str) -> None:
    """Keep the address of a sent feedback on this machine; an empty one forgets it.

    Only a successful send calls this, so an address that never left the
    machine (a draft, a saved ZIP) is not kept. Failing to write just means
    typing it again next time.
    """
    email = contact.strip()
    if email and not _EMAIL_RE.match(email):
        return
    path = saved_contact_path()
    try:
        if not email:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(json.dumps({"email": email}, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        pass


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
    endpoint: str, data: bytes, meta: dict[str, Any], *, on_progress: Callable[[int], None] | None = None
) -> str:
    """Post the package to the relay and return its reference number."""
    package: bytes | _CountingReader = _CountingReader(data, on_progress) if on_progress else data
    try:
        with _open_client() as client:
            response = client.post(
                f"{endpoint}/v1/feedback",
                data={"meta": json.dumps(meta, ensure_ascii=False)},
                files={"package": (package_filename(), package, "application/zip")},
                headers={"User-Agent": f"{APP_UPDATE_USER_AGENT}/{APP_VERSION}"},
            )
    except httpx.TimeoutException as exc:
        raise FeedbackError("发送超时，请检查网络后重试，或保存为 ZIP。", reason="network") from exc
    except httpx.HTTPError as exc:
        raise FeedbackError("无法连接反馈服务，请检查网络后重试，或保存为 ZIP。", reason="network") from exc
    if response.status_code == 413:
        raise FeedbackError("附件总大小超过 50 MB，请只附上必要的文件。", reason="too_large")
    if response.status_code == 429:
        raise FeedbackError("发送过于频繁，请稍后重试，或保存为 ZIP。", reason="rate_limited")
    try:
        body = response.json()
    except ValueError:
        body = {}
    reference = str(body.get("id") or "") if isinstance(body, dict) else ""
    if response.status_code != 200 or not reference:
        raise FeedbackError("反馈服务暂时不可用，请稍后重试，或保存为 ZIP。", reason="server")
    return reference


def _open_client() -> httpx.Client:
    # 跟随系统代理（httpx 默认读 HTTPS_PROXY 等环境变量），与检查更新一致。
    return httpx.Client(timeout=_SEND_TIMEOUT, follow_redirects=False)


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
