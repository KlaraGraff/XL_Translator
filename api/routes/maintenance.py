"""Update checks, data health, maintenance (clear/reset), diagnostics bundles and shutdown."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response, StreamingResponse

from core import diagnostics, maintenance
from settings import (
    AppSettings,
    load_settings,
    save_settings,
)
from api.schemas import (
    DataHealthDismissPayload,
    UpdatePreferencesRequest,
    MaintenanceClearRequest,
    FullResetRequest,
)
from api.routes._shared import (
    _require_no_active_tasks,
    _json_error,
    _json_safe,
)


def register(app: FastAPI) -> None:
    """Register the maintenance routes on ``app``."""

    @app.get("/api/updates/state")
    def update_state() -> dict[str, Any]:
        settings = load_settings()
        return _update_state_payload(settings)

    @app.get("/api/updates/check")
    def check_updates(mode: Literal["manual", "background"] = "manual") -> dict[str, Any]:
        settings = load_settings()
        if mode == "background":
            deferred = _background_update_deferred(settings)
            if deferred:
                return {
                    "ok": True,
                    "status": "deferred",
                    "message": "本次暂不在后台检查更新。",
                    "reason": deferred,
                    **_update_state_payload(settings),
                }
        from core.update_checker import check_for_updates

        result = check_for_updates()
        if mode == "background":
            # Display only — "last checked: ..." on the About page.  Nothing
            # gates on this value any more (see _background_update_deferred).
            # The network check can take seconds; reload before stamping the
            # timestamp so settings saved meanwhile are not clobbered.
            settings = load_settings()
            settings.update.last_background_check_at = datetime.now(timezone.utc).isoformat()
            save_settings(settings)
        if result.status == "error":
            diagnostics.record_system_diagnostic(
                phase="update_check",
                error_code=result.diagnostic_code or "update_check_failed",
            )
        payload = _json_safe(result)
        if mode == "background":
            payload["notification_suppressed"] = _background_notice_suppressed(settings, payload)
        return payload

    @app.get("/api/updates/stars")
    def repo_stars() -> dict[str, Any]:
        """设置「更新与关于」页「去点 Star」按钮旁边的数字。查不到时 stars 为 null，界面不显示数字。

        关了「启动时后台检查更新」的用户说过不想让软件自己联网，这里也不去问 GitHub。
        """
        from core.repo_stars import REPO_PAGE_URL, cached_star_count

        settings = load_settings()
        stars = None if settings.update.notifications_paused else cached_star_count()
        return {"stars": stars, "repo_url": REPO_PAGE_URL}

    @app.put("/api/updates/preferences")
    def update_preferences(payload: UpdatePreferencesRequest) -> dict[str, Any]:
        settings = load_settings()
        changed = payload.model_fields_set
        if "notifications_paused" in changed:
            settings.update.notifications_paused = bool(payload.notifications_paused)
        if "ignored_release_version" in changed:
            settings.update.ignored_release_version = str(
                payload.ignored_release_version or ""
            ).strip()
        if "quick_start_completed" in changed:
            settings.onboarding.quick_start_completed = bool(payload.quick_start_completed)
        if "pdf_guide_seen" in changed:
            settings.onboarding.pdf_guide_seen = bool(payload.pdf_guide_seen)
        if "cad_guide_seen" in changed:
            settings.onboarding.cad_guide_seen = bool(payload.cad_guide_seen)
        save_settings(settings)
        return _update_state_payload(settings)

    @app.get("/api/data/health")
    def data_health() -> dict[str, Any]:
        """Tell the UI whether local data was kept, upgraded, or rebuilt."""
        return maintenance.data_health()

    @app.delete("/api/data/health/notice")
    def dismiss_data_health_notice(
        payload: DataHealthDismissPayload | None = None,
    ) -> dict[str, Any]:
        # 不带 body 维持旧语义（整份清除）；带 scopes 只清横幅真正展示过的
        # 作用域，横幅在屏期间才惰性写入的事件（典型：keys）不会被顺手抹掉。
        return maintenance.dismiss_recovery_notice(payload.scopes if payload else None)

    @app.get("/api/maintenance/overview")
    def maintenance_overview() -> dict[str, Any]:
        return maintenance.data_overview(
            active_task_count=app.state.task_manager.active_task_count(),
        )

    @app.post("/api/maintenance/clear")
    def maintenance_clear(payload: MaintenanceClearRequest) -> dict[str, Any]:
        category = payload.category
        if category in {"task_history", "logs", "diagnostics", "keys", "tm"}:
            _require_no_active_tasks(app, category=category)
        if category in {"keys", "settings", "tm", "workspaces"} and not payload.confirmation:
            raise HTTPException(422, "此操作需要明确确认。")
        if category == "task_history":
            return {
                "category": "task_history",
                "removed_count": app.state.task_manager.clear_history(),
                "outputs_affected": False,
                "restart_required": False,
            }
        if category == "logs":
            return maintenance.clear_logs().as_dict()
        if category == "diagnostics":
            return maintenance.clear_diagnostics().as_dict()
        if category == "keys":
            return maintenance.clear_keys().as_dict()
        if category == "settings":
            return maintenance.reset_settings().as_dict()
        if category == "tm":
            return maintenance.clear_tm(lang_pair=payload.lang_pair).as_dict()
        if category == "workspaces":
            return maintenance.clear_owned_workspaces().as_dict()
        raise HTTPException(422, "不支持的维护类别。")

    @app.post("/api/maintenance/reset-full")
    def reset_full_local_data(payload: FullResetRequest) -> dict[str, Any]:
        _require_no_active_tasks(app, category="reset_full")
        if not payload.confirmation or payload.phrase != "RESET":
            raise HTTPException(422, "完整重置需要勾选确认并输入 RESET。")
        return maintenance.reset_all_local_data().as_dict()

    @app.get("/api/diagnostics")
    def list_diagnostics() -> dict[str, Any]:
        return {
            "records": diagnostics.public_diagnostic_records(),
            "overview": diagnostics.diagnostic_overview(),
        }

    @app.get("/api/diagnostics/history.zip")
    def download_diagnostics_history() -> StreamingResponse:
        payload, filename, count = diagnostics.build_diagnostics_history_zip_bytes()
        return _zip_response(payload, filename, count=count)

    @app.get("/api/diagnostics/task/{task_id}.zip")
    def download_task_diagnostic(task_id: str) -> StreamingResponse:
        record = _task_diagnostic_record(task_id)
        if record is None:
            raise HTTPException(404, "该任务没有诊断记录（可能由早期版本运行，或已被自动清理），无可导出的内容。")
        return _zip_response(*_diagnostic_zip_or_404(record))

    @app.get("/api/diagnostics/task/{task_id}")
    def task_diagnostic_availability(task_id: str) -> dict[str, Any]:
        """Whether this task has a diagnostic record to export.

        Registered after the ``.zip`` route on purpose: ``{task_id}`` also
        matches ``abc.zip``, so the download has to win the first match.

        任务详情据此决定「导出诊断」能不能点。任务结束时都会写一份记录，但更早版本
        跑的任务、或超出保留上限被清掉的就没有；以前要点了按钮、收到一条红色报错
        toast 才知道。
        """
        return {"available": _task_diagnostic_record(task_id) is not None}

    @app.get("/api/diagnostics/{record_id}.zip")
    def download_diagnostic_record(record_id: str) -> StreamingResponse:
        record = diagnostics.find_diagnostic_record(record_id)
        if record is None:
            raise HTTPException(404, "找不到这条诊断记录，可能已被清除。")
        return _zip_response(*_diagnostic_zip_or_404(record))

    @app.delete("/api/diagnostics/{record_id}")
    def delete_diagnostic_record(record_id: str) -> dict[str, Any]:
        _require_no_active_tasks(app, category="diagnostics")
        return maintenance.delete_diagnostic(record_id).as_dict()

    @app.post("/api/system/shutdown", status_code=202)
    async def request_shutdown() -> Response:
        """Ask the sidecar to wind down the same way SIGTERM does on Unix.

        Windows has no graceful signal that reaches the sidecar (it is started
        with ``CREATE_NO_WINDOW``), so the desktop shell calls this before
        falling back to ``TerminateProcess``.  The hook is installed by
        ``api.launcher``; an app built any other way (tests, ``uvicorn``
        started by hand) has no server to stop and says so.

        Async on purpose: the hook flips flags that the event loop thread also
        touches from uvicorn's signal handler, so it runs on that thread too.
        """
        hook = getattr(app.state, "request_shutdown", None)
        if hook is None:
            return _json_error(
                409,
                "此后台服务不是由应用启动的，无法从此处关闭。",
                reason="shutdown_unavailable",
            )
        hook()
        return JSONResponse(status_code=202, content={"ok": True, "status": "shutting_down"})


def _diagnostic_zip_or_404(record: dict[str, Any]) -> tuple[bytes, str]:
    """Zip one diagnostic record, or say it is gone rather than crashing.

    诊断记录会按数量和总体积轮转清理，索引里的条目可能指向一个已经被删掉的目录
    （也可能是用户在下载弹窗开着的时候清理了诊断数据）。原来这里直接抛
    ``FileNotFoundError``，界面收到 500「服务器内部错误」，读起来像是应用坏了；
    实际上只是这份记录不在了，说清楚就行。
    """
    try:
        return diagnostics.build_diagnostic_zip_bytes(record["record_dir"])
    except FileNotFoundError as exc:
        raise HTTPException(
            404, "此诊断记录的文件已被清理，无可导出的内容。"
        ) from exc


def _task_diagnostic_record(task_id: str) -> dict[str, Any] | None:
    """Resolve the newest diagnostic record belonging to one task.

    Records deliberately never store the task id — only the anonymous locator
    derived from it.  Reuse ``core.diagnostics`` own derivation instead of
    copying the hashing detail, so the two can never drift apart.
    """
    key = str(task_id or "").strip()
    if not key:
        return None
    locator = diagnostics._anonymous_locator(key)
    # list_diagnostic_records() 已按创建时间倒序，取第一条即最新一次归档。
    return next(
        (
            item
            for item in diagnostics.list_diagnostic_records()
            if str(item.get("anonymous_locator") or "") == locator
        ),
        None,
    )


def _update_state_payload(settings: AppSettings) -> dict[str, Any]:
    return {
        "preferences": {
            "notifications_paused": settings.update.notifications_paused,
            "ignored_release_version": settings.update.ignored_release_version,
            "last_background_check_at": settings.update.last_background_check_at,
            "quick_start_completed": settings.onboarding.quick_start_completed,
            "pdf_guide_seen": settings.onboarding.pdf_guide_seen,
            "cad_guide_seen": settings.onboarding.cad_guide_seen,
        },
        "background_due": _background_update_deferred(settings) is None,
    }


def _background_update_deferred(settings: AppSettings) -> str | None:
    """Say why this launch must not check, or None to go ahead and check.

    There is deliberately no time-based throttle here.  There used to be one
    ("checked within 24h → skip"), and it cached the *answer* rather than
    rate-limiting the request: a check that ran shortly before a release was
    published silenced every launch for the next day, invisibly — the frontend
    shows nothing at all for a deferred check, so the failure looked like the
    updater being broken.  Translator is opened a handful of times a day and
    the check is one conditional GET, so the request was never worth saving.

    Throttling belongs on the *notice*, not on the check: `notifications_paused`
    mutes it globally and `ignored_release_version` mutes one release.  Those
    are the only two reasons a launch stays quiet about an update that exists,
    and both are things the user turned on themselves.
    """
    if not settings.onboarding.quick_start_completed:
        return "quick_start_incomplete"
    if settings.update.notifications_paused:
        return "notifications_paused"
    return None


def _background_notice_suppressed(settings: AppSettings, result: dict[str, Any]) -> bool:
    if result.get("status") != "available":
        return True
    return str(result.get("latest_version") or "") == settings.update.ignored_release_version


def _zip_response(payload: bytes, filename: str, *, count: int | None = None) -> StreamingResponse:
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    if count is not None:
        headers["X-Translator-Record-Count"] = str(count)
    return StreamingResponse(iter([payload]), media_type="application/zip", headers=headers)
