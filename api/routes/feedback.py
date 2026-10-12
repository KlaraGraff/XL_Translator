"""In-app feedback: list attachable tasks, preview, save as ZIP, send to the relay."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Response

from core import feedback
from settings import load_settings
from api.schemas import FeedbackFilesRequest, FeedbackRequest
from api.task_contracts import TaskNotFoundError
from api.routes._shared import _json_error


def register(app: FastAPI) -> None:
    """Register the feedback routes on ``app``."""

    @app.get("/api/feedback/context")
    def feedback_context() -> dict[str, Any]:
        recent = app.state.task_manager.list_tasks().get("recent") or []
        return {
            "endpoint_configured": bool(feedback.feedback_endpoint()),
            "tasks": feedback.recent_tasks(recent),
            "max_attachment_bytes": feedback.MAX_ATTACHMENT_BYTES,
            "max_files": feedback.MAX_FILES,
            "max_tasks": feedback.MAX_TASKS,
        }

    @app.post("/api/feedback/files")
    def feedback_files(request: FeedbackFilesRequest) -> dict[str, Any]:
        # 界面挑完文件后用来显示名字和大小；不读内容。
        return {"files": [feedback.file_info(path) for path in request.paths]}

    @app.get("/api/feedback/tasks/{task_id}/files", response_model=None)
    def feedback_task_files(task_id: str) -> dict[str, Any] | Response:
        # 勾选某次任务的日志后，列出这次任务的原文件和译文，供用户逐个勾选；默认都不勾。
        manager = app.state.task_manager
        try:
            record = manager.task_status(task_id)
        except TaskNotFoundError:
            return _json_error(404, "所选任务的记录已不存在，请重新选择。", reason="task_missing")
        try:
            live = manager.task_source(task_id)
        except TaskNotFoundError:
            live = {}
        sources = {str(item.get("file_id") or ""): str(item.get("path") or "") for item in live.get("files") or []}
        return {"files": feedback.task_files(record, sources)}

    @app.post("/api/feedback/preview", response_model=None)
    def feedback_preview(request: FeedbackRequest) -> dict[str, Any] | Response:
        try:
            return feedback.preview(_draft(app, request), settings=load_settings())
        except feedback.FeedbackError as exc:
            return _json_error(400, str(exc), reason=exc.reason)

    @app.post("/api/feedback/package", response_model=None)
    def feedback_package(request: FeedbackRequest) -> Response:
        try:
            data, _payload = feedback.build_package(_draft(app, request), settings=load_settings())
        except feedback.FeedbackError as exc:
            return _json_error(400, str(exc), reason=exc.reason)
        return Response(
            content=data,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{feedback.package_filename()}"'},
        )

    @app.post("/api/feedback/send", response_model=None)
    def feedback_send(request: FeedbackRequest) -> dict[str, Any] | Response:
        try:
            return feedback.send(_draft(app, request), settings=load_settings())
        except feedback.FeedbackError as exc:
            return _json_error(_SEND_STATUS.get(exc.reason, 400), str(exc), reason=exc.reason)


# 发不出去（服务未开通、网络、服务端）和用户能改的输入错误分开，界面据此决定要不要
# 把「保存为 ZIP」推到前面。
_SEND_STATUS = {"not_configured": 503, "network": 502, "server": 502, "rate_limited": 429}


def _draft(app: FastAPI, request: FeedbackRequest) -> feedback.FeedbackDraft:
    records: list[dict[str, Any]] = []
    for task_id in dict.fromkeys(item.strip() for item in request.task_ids if item.strip()):
        try:
            records.append(app.state.task_manager.task_status(task_id))
        except TaskNotFoundError as exc:
            raise feedback.FeedbackError(
                "所选任务的记录已不存在，请重新选择。", reason="task_missing"
            ) from exc
    return feedback.FeedbackDraft(
        category=request.category,
        description=request.description,
        contact=request.contact,
        include_system=request.include_system,
        ui_language=request.ui_language,
        task_records=records,
        file_paths=list(request.file_paths),
    )
