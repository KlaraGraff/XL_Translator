"""重启后，从运行结束时写下的清单重建逐页记录。

逐页记录平时只活在 runner 实例里：sidecar 重启（或终态实例被回收）之后，
``/api/tasks/{id}/pdf-pages`` 就没有 runner 可问，界面只能写一句「此历史任务的逐页
记录已不可用」。运行结束时写的 ``pdf_translation_manifest.json`` 里本来就有
``files[].pages[]``，这里把清单读回成 ``PdfFileRecord`` / ``PdfPageRecord``，再用和
运行中同一套投影（``pdf_page_snapshot_entry``）交出去，两边给出的字段不会各说各话。

只读：不写盘，也不改任何任务状态。
"""

from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path
from typing import Any

from loguru import logger

from core.pdf_image_translation import (
    PdfPageActionError,
    page_image_name,
    pdf_file_snapshot_entry,
    pdf_page_snapshot_entry,
)
from core.pdf_models import (
    PDF_MANIFEST_FILENAME,
    PDF_PAGES_ROOT,
    SOURCE_PAGES_DIRNAME,
    SOURCE_TYPE_PDF,
    TRANSLATED_PAGES_DIRNAME,
    PdfFileRecord,
    PdfPageRecord,
)
from core.pdf_task_report import _page_archive_stem

# 清单里的键可能比这个版本多、也可能比它少（用户可写的数据、前后版本都会写）：
# 认识的键照收，不认识的忽略，缺的用记录自己的默认值，绝不因为多一个字段就丢页。
_PAGE_RECORD_FIELDS = frozenset(field.name for field in fields(PdfPageRecord))
_FILE_RECORD_FIELDS = frozenset(field.name for field in fields(PdfFileRecord))


class _RestoredFile:
    """清单里的一个文件：记录本身、按页码索引的页，以及它自己的页图存档目录。"""

    def __init__(self, entry: dict[str, Any], pages: dict[int, PdfPageRecord], page_count: int) -> None:
        known = {key: value for key, value in entry.items() if key in _FILE_RECORD_FIELDS}
        known.pop("pages", None)
        known.setdefault("name", "")
        known.setdefault("source_path", "")
        known.setdefault("relative_path", "")
        known.setdefault("source_type", SOURCE_TYPE_PDF)
        self.record = PdfFileRecord(**known)
        # 清单里的 page_count 和 pages[] 理论上一致，缺一个的时候以另一个为准：页表要按
        # 真实存在的页给，不能因为少一个计数就把整份文件的页全丢掉。
        self.page_count = max(page_count, max(pages, default=0))
        self.record.page_count = self.page_count
        self.record.pages = sorted(pages.values(), key=lambda page: page.page_number)
        self.pages = pages


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _page_record(payload: Any) -> PdfPageRecord | None:
    """清单里的一页，或没有可用页码时的 ``None``。"""
    if not isinstance(payload, dict):
        return None
    page_number = _int(payload.get("page_number"))
    if page_number < 1:
        return None
    known = {key: value for key, value in payload.items() if key in _PAGE_RECORD_FIELDS}
    known["page_number"] = page_number
    # source_image_path 是记录的唯一必填字段；更早的清单可能没写全，缺了就当没记。
    known.setdefault("source_image_path", "")
    return PdfPageRecord(**known)


def manifest_file_records(manifest: dict[str, Any] | None) -> list[_RestoredFile] | None:
    """清单里的 ``files[]``，或没有可用内容时的 ``None``。

    一页都没记下来的文件也照样返回：它自己的 status/error 已经写清了原因（打不开、
    没轮到），整行丢掉反而让那份文件从表里消失。
    """
    if not isinstance(manifest, dict):
        return None
    entries = [entry for entry in manifest.get("files") or [] if isinstance(entry, dict)]
    if not entries:
        return None
    restored: list[_RestoredFile] = []
    for entry in entries:
        pages: dict[int, PdfPageRecord] = {}
        for item in entry.get("pages") or []:
            page = _page_record(item)
            if page is not None and page.page_number not in pages:
                pages[page.page_number] = page
        restored.append(_RestoredFile(entry, pages, max(0, _int(entry.get("page_count")))))
    return restored


def _read_manifest_file(path: Path) -> dict[str, Any] | None:
    if path.name != PDF_MANIFEST_FILENAME or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - 读不出来的清单只是「没有逐页记录」。
        logger.debug(f"[PDF] 读取任务清单失败原始错误：{path}：{exc!r}")
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("files"), list):
        return None
    payload["_resume_process_dir"] = str(path.parent)
    return payload


def read_task_pdf_manifest(record: dict[str, Any] | None) -> dict[str, Any] | None:
    """这个历史任务自己那份清单，或 ``None``。

    先认任务结果里记下的清单路径——同一批任务共用一个输出文件夹，只有这条路径指得准，
    按「文件夹里最新的一份」去找会把别的任务的页表端出来，那比没有页表更糟。记不下来
    时退回输出文件夹直下的清单（旧版产物把清单写在交付目录里）。
    """
    if not isinstance(record, dict):
        return None
    result = record.get("result") if isinstance(record.get("result"), dict) else {}
    recorded = _text(result.get("manifest_path")).strip()
    if recorded:
        payload = _read_manifest_file(Path(recorded))
        if payload is not None:
            return payload
    output_dir = _text(result.get("output_dir")).strip()
    if not output_dir:
        return None
    return _read_manifest_file(Path(output_dir) / PDF_MANIFEST_FILENAME)


def _manifest_dir(manifest: dict[str, Any] | None) -> Path | None:
    raw = _text((manifest or {}).get("_resume_process_dir")).strip()
    return Path(raw) if raw else None


def _page_archive_dir(
    manifest_dir: Path,
    relative_path: str,
    *,
    source: bool,
) -> Path | None:
    """这一份文件的页图存档目录；不会跑出清单所在的那次运行目录。"""
    relative = _text(relative_path).strip().replace("\\", "/").lstrip("/")
    parts = relative.split("/")
    if not relative or any(part in {"..", ""} for part in parts):
        return None
    dirname = SOURCE_PAGES_DIRNAME if source else TRANSLATED_PAGES_DIRNAME
    archive = manifest_dir / PDF_PAGES_ROOT / dirname / _page_archive_stem(Path(relative))
    try:
        resolved = archive.resolve(strict=False)
        base = manifest_dir.resolve(strict=False)
    except OSError:
        return None
    return archive if resolved != base and base in resolved.parents else None


def _page_image_file(
    restored: _RestoredFile,
    archive: Path | None,
    *,
    page_number: int,
    source: bool,
) -> Path | None:
    """和运行中的 ``_page_image_file`` 同一套候选：先认页记录指的那份，再按页图命名找。"""
    if archive is None:
        return None
    page = restored.pages.get(page_number)
    page_count = max(1, restored.page_count)
    candidates: list[Path] = []
    if page is not None:
        recorded = page.source_image_path if source else page.translated_image_path
        if recorded:
            candidates.append(Path(recorded))
    candidates.append(archive / page_image_name(page_number, page_count))
    if not source:
        candidates.append(archive / page_image_name(page_number, page_count, failed=True))
        stem = page_image_name(page_number, page_count).removesuffix(".png")
        candidates.extend(sorted(archive.glob(f"{stem}.*")) if archive.is_dir() else [])
    # 清单里记的是绝对路径，搬过文件夹之后可能指到别处；只认存档目录里的那份。
    try:
        resolved_archive = archive.resolve(strict=False)
    except OSError:
        return None
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=False)
        except OSError:
            continue
        if resolved != resolved_archive and resolved_archive not in resolved.parents:
            continue
        if candidate.is_file():
            return candidate
    return None


def _restored_files(record: dict[str, Any] | None) -> tuple[list[_RestoredFile], dict[str, Any], Path] | None:
    if not isinstance(record, dict) or _text(record.get("surface")) != "pdf":
        return None
    manifest = read_task_pdf_manifest(record)
    files = manifest_file_records(manifest)
    manifest_dir = _manifest_dir(manifest)
    if files is None or manifest is None or manifest_dir is None:
        return None
    return files, manifest, manifest_dir


def restored_page_image_path(
    record: dict[str, Any] | None,
    *,
    relative_path: str,
    page_number: int,
    kind: str,
) -> Path | None:
    """历史任务的一页原图/译图，或 ``None``。

    「上一版」一律没有：换回上一版要重装输出文件，那是 runner 的事；没有 runner 的
    任务只能看，不能换。
    """
    if kind not in {"source", "translated", "previous"}:
        raise PdfPageActionError("页图类型只能是 source、translated 或 previous。")
    if kind == "previous":
        return None
    restored = _restored_files(record)
    if restored is None:
        return None
    files, _manifest, manifest_dir = restored
    wanted = _text(relative_path).strip().replace("\\", "/")
    try:
        number = int(page_number)
    except (TypeError, ValueError):
        return None
    for item in files:
        if str(item.record.relative_path).replace("\\", "/") != wanted:
            continue
        if number < 1 or (item.page_count and number > item.page_count):
            return None
        archive = _page_archive_dir(manifest_dir, item.record.relative_path, source=kind == "source")
        return _page_image_file(item, archive, page_number=number, source=kind == "source")
    return None


def restored_pdf_page_review(record: dict[str, Any] | None) -> dict[str, Any] | None:
    """一个重启前就结束了的历史任务的逐页快照，或 ``None``。

    形状和 ``TranslationTaskManager.pdf_page_review`` 的返回值一致。``actionable`` 与
    ``rerun_actionable`` 都是假：没有 runner 就没有可重新生成某一页的页记录和装订
    流程，这里只回答「每一页当时是什么结果」。
    """
    restored = _restored_files(record)
    if restored is None:
        return None
    files, manifest, manifest_dir = restored
    payload_files: list[dict[str, Any]] = []
    for item in files:
        source_dir = _page_archive_dir(manifest_dir, item.record.relative_path, source=True)
        translated_dir = _page_archive_dir(manifest_dir, item.record.relative_path, source=False)
        pages: list[dict[str, Any]] = []
        for number in range(1, item.page_count + 1):
            pages.append(
                pdf_page_snapshot_entry(
                    number,
                    item.pages.get(number),
                    has_source_image=_page_image_file(
                        item, source_dir, page_number=number, source=True
                    )
                    is not None,
                    has_translated_image=_page_image_file(
                        item, translated_dir, page_number=number, source=False
                    )
                    is not None,
                )
            )
        payload_files.append(pdf_file_snapshot_entry(item.record, pages))
    return {
        "task_id": _text(record.get("task_id")),
        "state": _text(record.get("state")),
        "terminal": bool(record.get("terminal", True)),
        # 没有 runner：既不能排队页操作，也不能单独重跑某一页。
        "actionable": False,
        "review_enabled": bool(manifest.get("review_enabled")),
        "rerun_actionable": False,
        "rerun": {"active": False, "relative_path": "", "page_number": 0, "error": ""},
        "files": payload_files,
    }
