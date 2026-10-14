"""TM cleaning: an AI pass over one language pair of the translation memory.

Not a page under 翻译 and not file-based, but it runs through the same task
center, so it has an entry here for its identity.  Its start flow (a language
pair instead of a source path) stays in the task manager.
"""

from __future__ import annotations

from api.surfaces.base import Surface


class TmCleanSurface(Surface):
    key = "tm_clean"
    label = "TM cleaning"
    page_key = "tm_clean"
    requires_source = False
    display_label = {"zh": "记忆库清洗", "en": "Memory cleanup", "fr": "Nettoyage de la mémoire"}
    # 图标集里没有专属图标，与任务中心一样用记忆库图标。
    icon = "book"
