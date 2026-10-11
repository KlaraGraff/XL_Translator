"""Registry of translation surfaces (the pages under 翻译, plus TM cleaning).

To add a page, write ``api/surfaces/<key>.py`` with a ``Surface`` subclass
and add an instance to ``_SURFACES`` below; the task manager, the request
schemas and the progress weights pick it up from here.  The full checklist,
front end included, is docs/ADDING_A_TRANSLATION_PAGE.md.
"""

from __future__ import annotations

from api.surfaces.base import RunnerRequest, Surface
from api.surfaces.cad import CadSurface
from api.surfaces.excel import ExcelSurface
from api.surfaces.pdf import PdfSurface
from api.surfaces.tm_clean import TmCleanSurface
from api.surfaces.word import WordSurface
from api.task_contracts import TaskInputError

_SURFACES: dict[str, Surface] = {
    surface.key: surface
    for surface in (
        ExcelSurface(),
        WordSurface(),
        PdfSurface(),
        CadSurface(),
        TmCleanSurface(),
    )
}

#: Every surface key, in registration order.
SURFACE_KEYS: tuple[str, ...] = tuple(_SURFACES)
#: Keys of the file-based pages (what ``/api/sources/scan`` accepts).
DOCUMENT_SURFACE_KEYS: tuple[str, ...] = tuple(
    key for key, surface in _SURFACES.items() if surface.requires_source
)


def get_surface(key: str) -> Surface:
    """The registered surface for ``key`` (case and whitespace ignored)."""
    normalized = str(key or "").strip().lower()
    surface = _SURFACES.get(normalized)
    if surface is None:
        raise TaskInputError("找不到此翻译页面，请刷新后重试。")
    return surface


def all_surfaces() -> tuple[Surface, ...]:
    return tuple(_SURFACES.values())


__all__ = [
    "DOCUMENT_SURFACE_KEYS",
    "SURFACE_KEYS",
    "RunnerRequest",
    "Surface",
    "all_surfaces",
    "get_surface",
]
