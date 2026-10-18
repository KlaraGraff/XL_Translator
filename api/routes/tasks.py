"""Source scanning, the task lifecycle, PDF page actions and the task event stream."""

from __future__ import annotations

from dataclasses import asdict, fields
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from loguru import logger

from api.surfaces import get_surface
from api.task_manager import (
    TaskOptions,
)
from settings import (
    load_settings,
)
from api.schemas import (
    BatchStartRequest,
    ClassifySourcesRequest,
    ScanRequest,
    PdfPageActionRequest,
    TaskStartRequest,
)
from api.routes._shared import (
    _require_no_active_tasks,
    _json_safe,
)


def register(app: FastAPI) -> None:
    """Register the tasks routes on ``app``."""

    @app.post("/api/sources/classify")
    def classify_sources(request: ClassifySourcesRequest) -> dict[str, Any]:
        """只数扩展名、不读内容：入口页据此把拖入的文件分到个数最多的那个类型页。"""
        from core.source_classify import classify_source_paths

        result = classify_source_paths(request.paths)
        return {
            "items": [asdict(item) for item in result.items],
            "truncated": result.truncated,
        }

    @app.post("/api/sources/scan")
    def scan_sources(request: ScanRequest) -> dict[str, Any]:
        if request.apply_saved_defaults:
            saved = app.state.task_manager.saved_scan_defaults(
                request.surface,
                source_lang=request.source_language,
                target_lang=request.target_language,
            )
            unset = {
                key: value
                for key, value in saved.items()
                if key in ScanRequest.model_fields and key not in request.model_fields_set
            }
            if unset:
                request = request.model_copy(update=unset)
        roots = [Path(item).expanduser() for item in request.paths if str(item).strip()]
        if not roots:
            roots = [Path(request.path).expanduser()]
        surface = get_surface(request.surface)
        custom = surface.scan_response(request, roots)
        if custom is not None:
            return custom
        result = _merge_scan_results([surface.scan_sources(root, request) for root in roots])
        from core.file_progress import file_progress_id

        payload = {
            "items": [{**_json_safe(item), "file_id": file_progress_id(item.path)} for item in result.items],
            "skipped": [_json_safe(item) for item in result.skipped],
            "summary": result.summary,
            "risk": result.risk,
        }
        # 续译检测只认内置页面的产物命名；运行时注册的类型没有续译。
        payload["previous_output"] = (
            _detect_previous_output_safe(request, result, roots) if surface.builtin else None
        )
        # ``result`` is a stable grouped alias for callers that consume
        # one typed scan object; top-level fields keep the Phase 1 route
        # backward compatible.
        payload["result"] = dict(payload)
        return payload

    def _detect_previous_output_safe(
        request: ScanRequest, result: Any, roots: list[Path]
    ) -> dict[str, Any] | None:
        # 检测只是给弹窗提供线索，失败绝不允许拖垮扫描本身——import 和调用
        # 都包在这里，任何异常都降级成「没有上一次的输出」。
        try:
            from core.resume_detection import detect_previous_output

            settings = load_settings()
            if request.surface == "excel":
                target_lang = settings.excel_target_lang
                source_lang: str | None = settings.excel_source_lang
                output_settings = settings.excel_output
            elif request.surface == "word":
                target_lang = settings.word_target_lang
                source_lang = settings.word_source_lang
                output_settings = settings.word_output
            else:
                target_lang = settings.pdf.target_lang
                source_lang = None
                output_settings = settings.pdf_output
            custom_output_root = (
                output_settings.custom_output_dir.strip() or None
                if output_settings.use_custom_output_dir
                else None
            )
            previous = detect_previous_output(
                surface=request.surface,
                items=result.items,
                scan_roots=roots,
                custom_output_root=custom_output_root,
                target_lang=target_lang,
                source_lang=source_lang,
                preferred_dir=request.preferred_resume_dir,
                # Excel 的补译计划按这个开关决定公式格进不进待译集合，检测层
                # 必须同一套口径，否则弹窗数字和任务实际行为对不上。
                formula_display_value_backfill=(
                    settings.excel_output.formula_display_value_backfill
                    if request.surface == "excel"
                    else True
                ),
            )
        except Exception:
            logger.exception("previous-output detection failed; scan continues without it")
            return None
        return _json_safe(previous) if previous else None

    def _task_options(request: TaskStartRequest) -> TaskOptions:
        # Every TaskOptions field has a same-named request field
        # (tests/test_surface_registry_contracts.py checks it), so a new
        # option is declared in those two places and nowhere else.
        values = {field.name: getattr(request, field.name) for field in fields(TaskOptions)}
        if request.apply_saved_defaults:
            saved = app.state.task_manager.saved_request_defaults(
                request.surface, target_lang=request.target_lang
            )
            _fill_unset_fields(values, saved, explicit=request.model_fields_set)
        return TaskOptions(**values)

    @app.post("/api/tasks/preflight")
    def preflight_task(request: TaskStartRequest) -> dict[str, Any]:
        options = _task_options(request)
        response = app.state.task_manager.preflight_task(
            surface=request.surface,
            source_path=request.source_path,
            selected_paths=request.selected_paths,
            options=options,
        )
        if request.apply_saved_defaults:
            # 命令行和 MCP 要能说出这次实际用了哪些选项（补过默认值之后的）。
            response["options"] = _json_safe(asdict(options))
        return response

    @app.post("/api/tasks", status_code=202)
    def start_task(request: TaskStartRequest) -> dict[str, Any]:
        return app.state.task_manager.start_task(
            surface=request.surface,
            source_path=request.source_path,
            selected_paths=request.selected_paths,
            options=_task_options(request),
            confirmation_token=request.confirmation_token,
        )

    @app.post("/api/tasks/batch", status_code=202)
    def start_batch(request: BatchStartRequest) -> dict[str, Any]:
        return app.state.task_manager.start_batch(
            [
                (item.surface, item.source_path, item.selected_paths, _task_options(item))
                for item in request.items
            ],
            allow_shared=request.allow_shared,
        )

    @app.get("/api/tasks")
    def list_tasks() -> dict[str, Any]:
        return app.state.task_manager.list_tasks()

    # Static task-center routes must be registered before the parameterized
    # task-id routes below; Starlette matches routes in declaration order.
    @app.get("/api/tasks/locks/current")
    def current_task_locks() -> dict[str, Any]:
        return {"reservations": app.state.task_manager.reservations()}

    @app.get("/api/tasks/resources")
    def task_resource_groups() -> dict[str, Any]:
        return {"groups": app.state.task_manager.resource_groups()}

    @app.delete("/api/tasks/history")
    def clear_task_history() -> dict[str, Any]:
        _require_no_active_tasks(app, category="task_history")
        return {
            "category": "task_history",
            "removed_count": app.state.task_manager.clear_history(),
            "outputs_affected": False,
            "restart_required": False,
        }

    @app.get("/api/tasks/{task_id}/results")
    def get_task_results(task_id: str) -> dict[str, Any]:
        return app.state.task_manager.task_results(task_id)

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: str) -> dict[str, Any]:
        return app.state.task_manager.task_status(task_id)

    @app.get("/api/tasks/{task_id}/source")
    def get_task_source(task_id: str) -> dict[str, Any]:
        # 只给本机界面重新接管任务时恢复来源栏用；任务中心和历史记录里仍然没有路径。
        return app.state.task_manager.task_source(task_id)

    @app.delete("/api/tasks/{task_id}")
    def delete_task_record(task_id: str) -> dict[str, Any]:
        # 只删任务中心的这条记录；运行中的任务由 task manager 拒绝（409）。
        return app.state.task_manager.delete_task_record(task_id)

    @app.post("/api/tasks/{task_id}/stop")
    def stop_task(task_id: str) -> dict[str, Any]:
        return app.state.task_manager.stop_task(task_id)

    @app.post("/api/tasks/{task_id}/pause")
    def pause_task(task_id: str) -> dict[str, Any]:
        return app.state.task_manager.pause_task(task_id)

    @app.post("/api/tasks/{task_id}/resume")
    def resume_task(task_id: str) -> dict[str, Any]:
        return app.state.task_manager.resume_task(task_id)

    @app.post("/api/tasks/{task_id}/end-paused")
    def end_paused_task(task_id: str) -> dict[str, Any]:
        return app.state.task_manager.end_paused_task(task_id)

    @app.get("/api/tasks/{task_id}/pdf-pages")
    def pdf_page_review(task_id: str) -> dict[str, Any]:
        return app.state.task_manager.pdf_page_review(task_id)

    @app.get("/api/tasks/{task_id}/pdf-pages/image")
    def pdf_page_image(
        task_id: str,
        file: str,
        page: int,
        kind: str = "translated",
    ) -> FileResponse:
        path = app.state.task_manager.pdf_page_image_path(
            task_id,
            relative_path=file,
            page_number=page,
            kind=kind,
        )
        if path is None:
            # 「上一版」只有做过一次单页重生成的页才有，缺它是常态而不是异常，
            # 报错要说清是哪一种缺，别让用户以为页图丢了。
            raise HTTPException(
                404,
                "此页没有上一版译文。" if kind == "previous" else "该页的图像不存在。",
            )
        return FileResponse(
            path,
            media_type=_page_image_media_type(path),
            # The page image is rewritten in place when a page is regenerated,
            # so a cached copy would show the previous run's output.
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/tasks/{task_id}/pdf-pages/regenerate")
    def regenerate_pdf_page(task_id: str, request: PdfPageActionRequest) -> dict[str, Any]:
        return app.state.task_manager.request_pdf_page_action(
            task_id,
            action="regenerate",
            relative_path=request.file,
            page_number=request.page,
        )

    @app.post("/api/tasks/{task_id}/pdf-pages/rerun")
    def rerun_pdf_page(task_id: str, request: PdfPageActionRequest) -> dict[str, Any]:
        # 终态任务专用：/regenerate 是「排队，继续翻译时生效」，这一条是「现在就重跑
        # 这一页，并把输出文件重新合成一遍」。
        return app.state.task_manager.rerun_pdf_page(
            task_id,
            relative_path=request.file,
            page_number=request.page,
        )

    @app.post("/api/tasks/{task_id}/pdf-pages/restore-previous")
    def restore_pdf_page_previous(task_id: str, request: PdfPageActionRequest) -> dict[str, Any]:
        # 终态任务专用：把这一页换回上一版译文页图（上一次重生成留下的那一版），
        # 并按新的页图重新装配输出文件。不调用模型、不计费；做完才返回。
        return app.state.task_manager.restore_pdf_page_previous(
            task_id,
            relative_path=request.file,
            page_number=request.page,
        )

    @app.post("/api/tasks/{task_id}/pdf-pages/skip")
    def skip_pdf_page(task_id: str, request: PdfPageActionRequest) -> dict[str, Any]:
        return app.state.task_manager.request_pdf_page_action(
            task_id,
            action="skip",
            relative_path=request.file,
            page_number=request.page,
        )

    @app.get("/api/tasks/{task_id}/events")
    def task_events(
        task_id: str,
        last_event_id: int = 0,
        last_event_header: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        after = last_event_id
        if last_event_header:
            try:
                after = int(last_event_header)
            except ValueError:
                pass
        return StreamingResponse(
            app.state.task_manager.iter_sse(task_id, after_event_id=after),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )


_PAGE_IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}


def _page_image_media_type(path: Path) -> str:
    return _PAGE_IMAGE_MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")


def _fill_unset_fields(values: dict[str, Any], saved: dict[str, Any], *, explicit: set[str]) -> None:
    """Take the saved value for every option the caller did not send.

    ``options`` (the switches of a page registered at run time) merges key by
    key: the switches the caller named win, the others keep their default.
    """
    for key, value in saved.items():
        if key not in values:
            continue
        if key == "options" and isinstance(value, dict):
            values[key] = {**value, **dict(values.get("options") or {})}
        elif key not in explicit:
            values[key] = value


def _merge_scan_results(results: list[Any]) -> Any:
    """Fold several single-path scans into one selectable list.

    多选文件时每个路径各扫一次，合并后 summary / risk 走同一个类的属性算，不会和
    单路径扫描算出两套口径。同一个文件被扫到两次（路径互相包含）只保留第一次。
    """
    base = results[0]
    for extra in results[1:]:
        base.items.extend(extra.items)
        base.skipped.extend(extra.skipped)
    seen: set[str] = set()
    unique_items = []
    for item in base.items:
        key = str(getattr(item, "path", "") or "")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        unique_items.append(item)
    base.items[:] = unique_items
    return base
