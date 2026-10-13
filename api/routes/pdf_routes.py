"""PDF 翻译方式（Beta）：设置面板要知道本机条件，才能提前说清哪些页会改用保底方式。"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

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
    except TaskInputError as exc:
        image_ready, image_message = False, str(exc)
    word_component = word_route_available()
    engine, office_message = office_pdf_engine() if word_component else ("", "未安装「PDF 处理组件」。")
    # 原位替换要用组件里的 PyMuPDF；装了 pdf2docx 但 PyMuPDF 加载不了时，转 Word 照常可用。
    component = component_state(refresh=True)
    target = str(settings.pdf.target_lang or "").lower()
    return {
        "image_model_ready": image_ready,
        "image_model_message": image_message,
        "word_component": word_component,
        "inplace_component": component.available,
        "inplace_component_message": component.message,
        "office_engine": engine,
        "office_message": office_message,
        "inplace_target_ok": target.startswith("zh"),
    }


def register(app: FastAPI) -> None:
    @app.get("/api/pdf/route-capabilities")
    def pdf_route_capabilities() -> dict[str, Any]:
        return route_capabilities()
