"""PDF 翻译方式（Beta）：设置面板要知道本机条件，才能提前说清哪些页会改用保底方式。"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import FastAPI
from loguru import logger
from pydantic import BaseModel

from settings import load_settings


def route_capabilities() -> dict[str, Any]:
    """图像翻译模型能不能用、本机能不能把译好的 Word 转回 PDF。不启动任何程序。"""
    from api.surfaces.base import TaskInputError
    from api.surfaces.pdf import _validate_image_model
    from core.pdf_component import component_state
    from core.pdf_word_route import office_pdf_engine, word_route_available

    settings = load_settings()
    try:
        _validate_image_model(settings)
        image_ready, image_message = True, ""
        # 测过、没通过的图像模型按「没有」算，和任务里一样（见 PdfImageTranslationRunner）。
        from core.model_roles import ROLE_IMAGE, resolve_effective_model_config

        if resolve_effective_model_config(settings, ROLE_IMAGE).availability_status == "unavailable":
            image_ready, image_message = False, "图像翻译模型的连接测试未通过。"
    except TaskInputError as exc:
        image_ready, image_message = False, str(exc)
    word_component = word_route_available()
    engine, office_message = office_pdf_engine() if word_component else ("", "未安装「PDF 处理组件」。")
    # 原位替换要用组件里的 PyMuPDF；装了 pdf2docx 但 PyMuPDF 加载不了时，转 Word 照常可用。
    component = component_state(refresh=True)
    from core.pdf_routes import inplace_target_supported
    from core.pdf_vector.fonts import COMPLEX_SCRIPT_TARGETS

    return {
        "image_model_ready": image_ready,
        "image_model_message": image_message,
        "word_component": word_component,
        "inplace_component": component.available,
        "inplace_component_message": component.message,
        "office_engine": engine,
        "office_message": office_message,
        "inplace_target_ok": inplace_target_supported(settings.pdf.target_lang),
        # 原位替换写不了的目标语言（界面按当前选的目标语言提示，不必等设置存好再查）。
        "inplace_unsupported_targets": sorted(COMPLEX_SCRIPT_TARGETS),
        # 段落重排（BabelDOC）装好了没有：没装时按页自动选择的文字页转 Word。
        "babeldoc_component": _babeldoc_installed(),
    }


def _babeldoc_installed() -> bool:
    from core.babeldoc_component import component_paths

    return component_paths() is not None


def page_kinds(paths: list[str]) -> dict[str, Any]:
    """所选 PDF 各有几页文字页、图纸、扫描页：PDF 页「翻译方式」下面那行「这批文件会怎样处理」
    按它写。读不了的文件记 None（界面不猜），任务里照常报错。"""
    from core.pdf_routes import count_page_kinds

    counts: dict[str, Any] = {}
    for path in paths[:_PAGE_KINDS_MAX_FILES]:
        try:
            counts[path] = asdict(count_page_kinds(path))
        except Exception as exc:  # noqa: BLE001 - unreadable here; the task reports the real error
            logger.debug(f"[PDF] 数不出页面类型：{path}：{exc!r}")
            counts[path] = None
    return {"counts": counts}


# 一次最多数这么多份；更多时界面只按已数出的写。
_PAGE_KINDS_MAX_FILES = 200


class PageKindsRequest(BaseModel):
    paths: list[str] = []


def register(app: FastAPI) -> None:
    @app.get("/api/pdf/route-capabilities")
    def pdf_route_capabilities() -> dict[str, Any]:
        return route_capabilities()

    @app.post("/api/pdf/page-kinds")
    def pdf_page_kinds(request: PageKindsRequest) -> dict[str, Any]:
        return page_kinds(list(request.paths))
