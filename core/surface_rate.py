"""速率与重试：按文件类型保存的批次、并发和重试设置（v38 起）。

「翻译参数」的 Excel、Word、PDF、CAD 页签各有一组同样的五项：同时请求数、每批条数、
每批字符上限、失败重试次数、单段重试次数。存在 ``settings.rate.<类型>``，None 表示
没改过，按文档翻译模型是云端还是本地取默认值。PDF 整页图像翻译另有三项（同时生成
页数、单页重试次数、同时审核页数），存在 ``settings.pdf``。

v37 及以前，同时请求数和每批条数按模型保存（core/model_throughput.py），CAD、Word
各自又有写死的批次，结果同一个「每批段数」对一部分文件类型不起作用。现在每种文件
类型只认这里。按模型的速率只剩清洗记忆库在用。
"""

from __future__ import annotations

from dataclasses import dataclass

from config import (
    CHUNK_CLOUD_DEFAULT,
    CHUNK_CLOUD_MAX,
    CHUNK_CLOUD_MIN,
    CHUNK_LOCAL_DEFAULT,
    CHUNK_LOCAL_MAX,
    CHUNK_LOCAL_MIN,
    CONCURRENCY_CLOUD_DEFAULT,
    CONCURRENCY_LOCAL_DEFAULT,
    PDF_PAGE_CONCURRENCY_DEFAULT,
    PDF_PAGE_CONCURRENCY_SAFETY_CAP,
    PDF_PAGE_RETRY_ATTEMPTS_DEFAULT,
    PDF_PAGE_RETRY_ATTEMPTS_MAX,
    PDF_PAGE_RETRY_ATTEMPTS_MIN,
    PDF_REVIEW_CONCURRENCY_DEFAULT,
    RATE_BATCH_CHARS_CLOUD_DEFAULT,
    RATE_BATCH_CHARS_LOCAL_DEFAULT,
    RATE_BATCH_CHARS_MAX,
    RATE_BATCH_CHARS_MIN,
    RATE_ITEM_RETRY_DEFAULT,
    RATE_ITEM_RETRY_MAX,
    RATE_ITEM_RETRY_MIN,
    REQUEST_RETRY_ATTEMPTS_DEFAULT,
    REQUEST_RETRY_ATTEMPTS_MAX,
    REQUEST_RETRY_ATTEMPTS_MIN,
    WORD_BATCH_PARAGRAPHS_DEFAULT,
    WORD_BATCH_PARAGRAPHS_MAX,
    WORD_BATCH_PARAGRAPHS_MIN,
    get_concurrency_bounds,
)
from core.model_roles import (
    EffectiveModelConfig,
    ROLE_IMAGE,
    ROLE_PDF_REVIEW,
    ROLE_TRANSLATION,
    resolve_effective_model_config,
)
from settings import RATE_SURFACE_KEYS, AppSettings, SurfaceRateSettings, WordBatchSettings

RATE_SURFACES: tuple[str, ...] = RATE_SURFACE_KEYS
RATE_FIELDS: tuple[str, ...] = (
    "concurrency",
    "batch_items",
    "batch_chars",
    "request_retry",
    "item_retry",
)
PDF_IMAGE_FIELDS: tuple[str, ...] = (
    "page_generation_concurrency",
    "page_retry_attempts",
    "review_concurrency",
)
_UNLIMITED = 2**31 - 1


@dataclass(frozen=True)
class SurfaceRate:
    """一种文件类型这一次实际用的数值（默认值已代入、已按范围收敛）。"""

    surface: str
    mode: str
    concurrency: int
    batch_items: int
    batch_chars: int
    request_retry: int
    item_retry: int

    def word_batch_settings(self) -> WordBatchSettings:
        """Word 分批器的参数：段数、字符上限和单段重试都取自这里。"""
        return WordBatchSettings(
            max_paragraphs_per_batch=self.batch_items,
            max_chars_per_batch=self.batch_chars,
            split_paragraph_chars=self.batch_chars,
            strict_retry_attempts=max(1, self.item_retry),
        )


def _mode(value: object) -> str:
    return "local" if str(value or "").strip() == "local" else "cloud"


def role_mode(settings: AppSettings, role: str = ROLE_TRANSLATION) -> str:
    """一个模型用途当前是云端还是本地（默认值和范围都按这个分档）。"""
    try:
        return _mode(resolve_effective_model_config(settings, role).mode)
    except Exception:  # noqa: BLE001 - a half-configured role still has a mode
        return _mode(getattr(settings.engine, "mode", "cloud"))


def rate_defaults(surface: str, mode: str) -> dict[str, int]:
    local = _mode(mode) == "local"
    if surface == "word":
        batch_items = WORD_BATCH_PARAGRAPHS_DEFAULT
    else:
        batch_items = CHUNK_LOCAL_DEFAULT if local else CHUNK_CLOUD_DEFAULT
    return {
        "concurrency": CONCURRENCY_LOCAL_DEFAULT if local else CONCURRENCY_CLOUD_DEFAULT,
        "batch_items": batch_items,
        "batch_chars": RATE_BATCH_CHARS_LOCAL_DEFAULT if local else RATE_BATCH_CHARS_CLOUD_DEFAULT,
        "request_retry": REQUEST_RETRY_ATTEMPTS_DEFAULT,
        "item_retry": RATE_ITEM_RETRY_DEFAULT,
    }


def rate_bounds(surface: str, mode: str, *, unlocked: bool = False) -> dict[str, tuple[int, int]]:
    """各项的可填范围。解锁后并发、每批条数和字符上限不设上限（Word 每批最多 8 段除外）。"""
    local = _mode(mode) == "local"
    if surface == "word":
        batch_items = (WORD_BATCH_PARAGRAPHS_MIN, WORD_BATCH_PARAGRAPHS_MAX)
    elif unlocked:
        batch_items = (1, _UNLIMITED)
    else:
        batch_items = (CHUNK_LOCAL_MIN, CHUNK_LOCAL_MAX) if local else (CHUNK_CLOUD_MIN, CHUNK_CLOUD_MAX)
    return {
        "concurrency": get_concurrency_bounds(_mode(mode), unlocked),
        "batch_items": batch_items,
        "batch_chars": (1, _UNLIMITED) if unlocked else (RATE_BATCH_CHARS_MIN, RATE_BATCH_CHARS_MAX),
        "request_retry": (REQUEST_RETRY_ATTEMPTS_MIN, REQUEST_RETRY_ATTEMPTS_MAX),
        # Word 的单段重试是逐段恢复的轮数，至少一轮才能把空译文、残缺译文找出来重发。
        "item_retry": (max(1, RATE_ITEM_RETRY_MIN) if surface == "word" else RATE_ITEM_RETRY_MIN, RATE_ITEM_RETRY_MAX),
    }


def _clamp(value: int, bounds: tuple[int, int]) -> int:
    return max(bounds[0], min(bounds[1], int(value)))


def _stored_rate(settings: AppSettings, surface: str) -> SurfaceRateSettings:
    if surface not in RATE_SURFACES:
        raise ValueError(f"未知的文件类型：{surface}")
    stored = getattr(getattr(settings, "rate", None), surface, None)
    return stored if isinstance(stored, SurfaceRateSettings) else SurfaceRateSettings()


def resolve_surface_rate(
    settings: AppSettings,
    surface: str,
    *,
    mode: str | None = None,
) -> SurfaceRate:
    """这种文件类型这一次用的五项数值。

    ``mode`` 是任务实际用的文档翻译模型连接的模式；不给就按当前设置的主用连接取。
    """
    resolved_mode = _mode(mode) if mode else role_mode(settings, ROLE_TRANSLATION)
    stored = _stored_rate(settings, surface)
    unlocked = bool(getattr(settings.engine, "concurrency_unlocked", False))
    defaults = rate_defaults(surface, resolved_mode)
    bounds = rate_bounds(surface, resolved_mode, unlocked=unlocked)
    values: dict[str, int] = {}
    for field in RATE_FIELDS:
        raw = getattr(stored, field)
        values[field] = defaults[field] if raw is None else _clamp(raw, bounds[field])
    return SurfaceRate(surface=surface, mode=resolved_mode, **values)


def pdf_text_as_word(settings: AppSettings) -> SurfaceRateSettings:
    """PDF「转 Word 翻译」交给 Word 流程时用的速率：PDF 文字那一组。

    没改过的项仍是 None，到 Word 流程里按 Word 的默认值（每批 8 段）走；改过的每批
    条数超过 8 时按 Word 的上限收敛。
    """
    return _stored_rate(settings, "pdf_text").model_copy(deep=True)


# ---------------------------------------------------------------------------
# PDF 整页图像翻译
# ---------------------------------------------------------------------------


def pdf_image_defaults(mode: str) -> dict[str, int]:
    local = _mode(mode) == "local"
    return {
        "page_generation_concurrency": CONCURRENCY_LOCAL_DEFAULT if local else PDF_PAGE_CONCURRENCY_DEFAULT,
        "page_retry_attempts": PDF_PAGE_RETRY_ATTEMPTS_DEFAULT,
        "review_concurrency": CONCURRENCY_LOCAL_DEFAULT if local else PDF_REVIEW_CONCURRENCY_DEFAULT,
    }


def pdf_image_bounds(*, unlocked: bool = False) -> dict[str, tuple[int, int]]:
    pages = (1, _UNLIMITED) if unlocked else (1, PDF_PAGE_CONCURRENCY_SAFETY_CAP)
    return {
        "page_generation_concurrency": pages,
        "page_retry_attempts": (PDF_PAGE_RETRY_ATTEMPTS_MIN, PDF_PAGE_RETRY_ATTEMPTS_MAX),
        "review_concurrency": pages,
    }


def _pdf_concurrency(settings: AppSettings, key: str, role: str, mode: str | None) -> int:
    resolved_mode = _mode(mode) if mode else role_mode(settings, role)
    bounds = pdf_image_bounds(unlocked=bool(getattr(settings.engine, "concurrency_unlocked", False)))[key]
    raw = getattr(settings.pdf, key, None)
    if raw is None:
        return _clamp(pdf_image_defaults(resolved_mode)[key], bounds)
    try:
        return _clamp(int(raw), bounds)
    except (TypeError, ValueError):
        return _clamp(pdf_image_defaults(resolved_mode)[key], bounds)


def resolve_pdf_page_concurrency(settings: AppSettings, *, mode: str | None = None) -> int:
    """整页图像翻译同时生成的页数；``mode`` 是 PDF 与图片翻译模型的模式。"""
    return _pdf_concurrency(settings, "page_generation_concurrency", ROLE_IMAGE, mode)


def resolve_pdf_review_concurrency(settings: AppSettings, *, mode: str | None = None) -> int:
    """开启逐页审核时同时审核的页数；``mode`` 是审核模型的模式。"""
    return _pdf_concurrency(settings, "review_concurrency", ROLE_PDF_REVIEW, mode)


# ---------------------------------------------------------------------------
# 任务占用的并发（调度器按连接汇总）
# ---------------------------------------------------------------------------

PAGE_TEXT_SURFACES = {
    "excel_translate": "excel",
    "word_translate": "word",
    "cad_translate": "cad",
    "pdf_translate": "pdf_text",
}


def task_role_rate(
    settings: AppSettings,
    page_key: str,
    config: EffectiveModelConfig,
) -> tuple[int, int | None]:
    """一个任务里一个模型用途的（同时请求数, 每批条数）；每批条数对图像类为 None。

    ``config`` 是任务给这个用途分到的连接。清洗记忆库不在「翻译参数」里，仍按模型
    速率（core/model_throughput.py）取。
    """
    role = config.role
    if page_key == "pdf_translate" and role == ROLE_IMAGE:
        return resolve_pdf_page_concurrency(settings, mode=config.mode), None
    if page_key == "pdf_translate" and role == ROLE_PDF_REVIEW:
        return resolve_pdf_review_concurrency(settings, mode=config.mode), None
    surface = PAGE_TEXT_SURFACES.get(str(page_key or "").strip())
    if surface is not None and role == ROLE_TRANSLATION:
        rate = resolve_surface_rate(settings, surface, mode=config.mode)
        return rate.concurrency, rate.batch_items
    from core.model_throughput import get_model_throughput

    throughput = get_model_throughput(settings, config)
    return int(throughput.concurrency), throughput.batch_size


def engine_settings_for_rate(settings: AppSettings, rate: SurfaceRate) -> AppSettings:
    """建引擎用的设置副本：本地 Ollama 一批内同时发几路，按这里的同时请求数。

    文字翻译用本地模型时逐批发送，一批由 Ollama 引擎拆成几路同时请求（以前固定按
    engine.ollama_concurrency 的 3 路，「同时请求数」设了也不起作用）。云端不受影响。
    """
    if rate.mode != "local":
        return settings
    copy = settings.model_copy(deep=True)
    copy.engine.ollama_concurrency = max(1, int(rate.concurrency))
    return copy


def rate_overview(settings: AppSettings) -> dict[str, object]:
    """「翻译参数」页的速率与重试：每种文件类型的已存值、默认值、范围和当前用的值。"""
    unlocked = bool(getattr(settings.engine, "concurrency_unlocked", False))
    text_mode = role_mode(settings, ROLE_TRANSLATION)
    surfaces: dict[str, object] = {}
    for surface in RATE_SURFACES:
        stored = _stored_rate(settings, surface)
        effective = resolve_surface_rate(settings, surface, mode=text_mode)
        surfaces[surface] = {
            "stored": {field: getattr(stored, field) for field in RATE_FIELDS},
            "defaults": rate_defaults(surface, text_mode),
            "bounds": {key: list(value) for key, value in rate_bounds(surface, text_mode, unlocked=unlocked).items()},
            "effective": {field: getattr(effective, field) for field in RATE_FIELDS},
        }
    image_mode = role_mode(settings, ROLE_IMAGE)
    review_mode = role_mode(settings, ROLE_PDF_REVIEW)
    image_defaults = pdf_image_defaults(image_mode)
    image_defaults["review_concurrency"] = pdf_image_defaults(review_mode)["review_concurrency"]
    surfaces["pdf_image"] = {
        "stored": {
            "page_generation_concurrency": settings.pdf.page_generation_concurrency,
            "page_retry_attempts": settings.pdf.page_retry_attempts,
            "review_concurrency": settings.pdf.review_concurrency,
        },
        "defaults": image_defaults,
        "bounds": {key: list(value) for key, value in pdf_image_bounds(unlocked=unlocked).items()},
        "effective": {
            "page_generation_concurrency": resolve_pdf_page_concurrency(settings, mode=image_mode),
            "page_retry_attempts": settings.pdf.page_retry_attempts,
            "review_concurrency": resolve_pdf_review_concurrency(settings, mode=review_mode),
        },
    }
    return {
        "mode": text_mode,
        "image_mode": image_mode,
        "unlocked": unlocked,
        "surfaces": surfaces,
    }


__all__ = [
    "PAGE_TEXT_SURFACES",
    "PDF_IMAGE_FIELDS",
    "RATE_FIELDS",
    "RATE_SURFACES",
    "SurfaceRate",
    "engine_settings_for_rate",
    "pdf_image_bounds",
    "pdf_image_defaults",
    "pdf_text_as_word",
    "rate_bounds",
    "rate_defaults",
    "rate_overview",
    "resolve_pdf_page_concurrency",
    "resolve_pdf_review_concurrency",
    "resolve_surface_rate",
    "role_mode",
    "task_role_rate",
]
