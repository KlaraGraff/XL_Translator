"""Registry of translation surfaces (the pages under 翻译, plus TM cleaning).

To add a page, write ``api/surfaces/<key>.py`` with a ``Surface`` subclass
and add an instance to ``_SURFACES`` below; the task manager, the request
schemas and the progress weights pick it up from here.  The full checklist,
front end included, is docs/ADDING_A_TRANSLATION_PAGE.md.

A page that does not ship with the app (``builtin = False``, key ``ext:<id>``)
is added at run time with ``register_surface``.  The front end has no
compiled table for it and draws it from its descriptor (``GET /api/surfaces``).
"""

from __future__ import annotations

from api.surfaces.base import RunnerRequest, Surface
from api.surfaces.cad import CadSurface
from api.surfaces.excel import ExcelSurface
from api.surfaces.pdf import PdfSurface
from api.surfaces.tm_clean import TmCleanSurface
from api.surfaces.word import WordSurface
from api.task_contracts import TaskInputError
from core.surface_settings import extension_id

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

#: Every built-in surface key, in registration order.
SURFACE_KEYS: tuple[str, ...] = tuple(_SURFACES)
#: Keys of the built-in file-based pages.
DOCUMENT_SURFACE_KEYS: tuple[str, ...] = tuple(
    key for key, surface in _SURFACES.items() if surface.requires_source
)


def register_surface(surface: Surface) -> Surface:
    """Add a page that does not ship with the app.

    Only ``ext:<id>`` keys with ``builtin = False`` are accepted, so nothing
    registered at run time can shadow or replace a built-in page.  Its
    settings live in ``settings.extensions[<id>]`` (``core.surface_settings``).
    """
    key = str(getattr(surface, "key", "") or "")
    if extension_id(key) is None or key != key.strip().lower():
        raise ValueError(f"a run-time surface key must look like 'ext:<id>', got {key!r}")
    if getattr(surface, "builtin", True):
        raise ValueError(f"{key}: a run-time surface must set builtin = False")
    if key in _SURFACES:
        raise ValueError(f"{key}: already registered")
    _SURFACES[key] = surface
    return surface


def unregister_surface(key: str) -> None:
    """Remove a page added with ``register_surface``; built-in pages stay."""
    surface = _SURFACES.get(str(key or "").strip().lower())
    if surface is not None and not surface.builtin:
        del _SURFACES[surface.key]


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
    "register_surface",
    "unregister_surface",
]
