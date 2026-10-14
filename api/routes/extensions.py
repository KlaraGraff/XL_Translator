"""Extension routes: add a format adapter from a folder or zip, list and remove installed ones.

Installing always takes two calls: ``preview`` runs every check and returns
what the confirmation dialog shows; ``install`` with ``confirmed: true`` (and
the previewed ``sha256``) then installs in the background, polled through
``install-progress``.  See ``core/adapter_install.py`` and
``api/extension_install_job.py``.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from api import extension_install_job as jobs
from api.surfaces import all_surfaces
from core import adapter_install
from core.adapter_install import AdapterInstallError


def _refused(exc: AdapterInstallError, *, status_code: int = 422, reason: str = "extension_invalid") -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"detail": str(exc), "reason": reason, "problems": list(exc.problems)},
    )


def register(app: FastAPI) -> None:
    """Register the extension routes on ``app``."""

    def active_surfaces() -> frozenset[str]:
        return app.state.task_manager.active_surfaces()

    @app.get("/api/extensions/installed")
    def installed_extensions() -> dict[str, Any]:
        """Installed extensions; ``ok`` is false (with ``problem``) for one that can no longer load."""
        registered = {surface.key for surface in all_surfaces()}
        items = []
        for item in adapter_install.installed_adapters():
            data = item.to_dict()
            data["key"] = f"ext:{item.id}"
            data["registered"] = data["key"] in registered
            items.append(data)
        return {"extensions": items, "install": jobs.JOB.progress()}

    @app.post("/api/extensions/preview", response_model=None)
    def preview_extension(payload: dict[str, Any]) -> dict[str, Any] | JSONResponse:
        """Check a folder or zip and describe it for the confirmation dialog; changes nothing."""
        try:
            return adapter_install.preview(str(payload.get("path") or ""), reserved_ids=jobs.reserved_ids())
        except AdapterInstallError as exc:
            return _refused(exc)

    @app.post("/api/extensions/install", response_model=None)
    def install_extension(payload: dict[str, Any]) -> dict[str, Any] | JSONResponse:
        """Install in the background after the user confirmed; poll install-progress."""
        path = str(payload.get("path") or "")
        if payload.get("confirmed") is not True:
            return _refused(AdapterInstallError("安装扩展前需要确认来源可信"), reason="extension_unconfirmed")
        expected = str(payload.get("sha256") or "")

        def work(*, progress, cancel) -> dict[str, Any]:
            return jobs.install_and_activate(
                path,
                active_surfaces=active_surfaces,
                expected_sha256=expected,
                progress=progress,
                cancel=cancel,
            )

        try:
            return jobs.JOB.begin(work, label=path)
        except AdapterInstallError as exc:
            return _refused(exc, status_code=409, reason="extension_install_busy")

    @app.get("/api/extensions/install-progress")
    def extension_install_progress() -> dict[str, Any]:
        return jobs.JOB.progress()

    @app.post("/api/extensions/cancel-install")
    def cancel_extension_install() -> dict[str, Any]:
        return jobs.JOB.cancel()

    @app.post("/api/extensions/{adapter_id}/uninstall", response_model=None)
    def uninstall_extension(adapter_id: str) -> dict[str, Any] | JSONResponse:
        """Unregister the type and delete our copy; its saved settings are kept for a reinstall."""
        if jobs.JOB.running():
            return _refused(AdapterInstallError("正在安装扩展，请稍候"), status_code=409, reason="extension_install_busy")
        if str(adapter_id or "").strip().lower() not in adapter_install.read_registry():
            return _refused(
                AdapterInstallError("没有找到这个扩展，可能已被卸载"), status_code=404, reason="extension_not_installed"
            )
        try:
            record = jobs.uninstall_and_deactivate(adapter_id, active_surfaces=active_surfaces)
        except AdapterInstallError as exc:
            return _refused(exc, status_code=409, reason="extension_in_use")
        return {"uninstalled": record}
