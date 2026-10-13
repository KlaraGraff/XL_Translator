"""Vector-preserving in-place PDF translation (Beta)."""
from .runner import InplaceHooks, InplaceResult, find_cjk_font, ocr_languages, translate_page_inplace

__all__ = ["InplaceHooks", "InplaceResult", "find_cjk_font", "ocr_languages", "translate_page_inplace"]
