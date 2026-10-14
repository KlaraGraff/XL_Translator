"""Type descriptors of the translation pages (``GET /api/surfaces``)."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from api.surfaces import all_surfaces


def register(app: FastAPI) -> None:
    """Register the surface descriptor route on ``app``."""

    @app.get("/api/surfaces")
    def list_surfaces() -> dict[str, Any]:
        # Registration order, which is also the order of the sidebar.
        return {"surfaces": [surface.describe() for surface in all_surfaces()]}
