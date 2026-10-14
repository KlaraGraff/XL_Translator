"""What a format adapter for Translator imports.

A format adapter teaches the app one more file type with three functions in
``adapter.py`` next to an ``adapter.json`` manifest::

    from translator_adapter import ScanItem, TextUnit, TranslatedUnit, OutputFile

    def scan(path: str) -> ScanItem: ...            # optional: count the units
    def extract(path: str) -> list[TextUnit]: ...   # the text to translate
    def write(path: str, units: list[TranslatedUnit], mode: str,
              out_path: str, options: dict) -> OutputFile: ...

The app does everything else: the page, model calls, the translation memory,
retries, review marks, progress, the task center.  An adapter never calls a
model, reads keys or goes online.

This package uses the standard library only and never imports the app.
"""

from translator_adapter.protocol import (
    ADAPTER_API_VERSION,
    MODE_BILINGUAL,
    MODE_TRANSLATION_ONLY,
    MODES,
    STATUS_LOCKED,
    STATUS_MEMORY,
    STATUS_NEEDS_REVIEW,
    STATUS_TRANSLATED,
    STATUS_UNTRANSLATED,
    STATUSES,
    OptionSpec,
    OutputFile,
    ScanItem,
    TextUnit,
    TranslatedUnit,
)

__all__ = [
    "ADAPTER_API_VERSION",
    "MODES",
    "MODE_BILINGUAL",
    "MODE_TRANSLATION_ONLY",
    "STATUSES",
    "STATUS_LOCKED",
    "STATUS_MEMORY",
    "STATUS_NEEDS_REVIEW",
    "STATUS_TRANSLATED",
    "STATUS_UNTRANSLATED",
    "OptionSpec",
    "OutputFile",
    "ScanItem",
    "TextUnit",
    "TranslatedUnit",
]
