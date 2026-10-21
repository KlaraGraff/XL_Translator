"""Vector-preserving in-place PDF translation (Beta)."""
from .fonts import find_cjk_font, target_supported
from .runner import InplaceHooks, InplaceResult, ocr_languages, translate_page_inplace

__all__ = ["InplaceHooks", "InplaceResult", "find_cjk_font", "ocr_languages", "target_supported",
           "translate_page_inplace"]
