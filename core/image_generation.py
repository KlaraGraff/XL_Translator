"""Image-generation connectivity and page translation clients."""

from __future__ import annotations

import base64
import json
import math
import tempfile
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Protocol

import httpx
from PIL import Image

from config import PDF_MIN_READABLE_LONG_EDGE_PX, PDF_MIN_READABLE_SHORT_EDGE_PX
from core.model_roles import (
    EffectiveModelConfig,
    ROLE_IMAGE,
    image_model_signature,
    provider_supports_capability,
    record_image_model_availability,
    resolve_effective_model_config,
)
from settings import AppSettings

IMAGE_TEST_MAX_ATTEMPTS = 3
IMAGE_GENERATION_TIMEOUT_SECONDS = 900.0
GPT_IMAGE_MODEL_PREFIX = "gpt-image-"
GPT_IMAGE_2_MODEL = "gpt-image-2"
GPT_IMAGE_2_MIN_PIXELS = 655_360
GPT_IMAGE_2_MAX_PIXELS = 8_294_400
GPT_IMAGE_2_MAX_EDGE = 3840
GPT_IMAGE_2_PDF_TARGET_LONG_EDGE = 2048

PDF_IMAGE_TRANSLATION_PROMPT = (
    "Translate every readable visible text element on this PDF page into {target_language}. "
    "For Chinese, use concise Simplified Chinese that fits the original boxes and table cells. "
    "Return one complete full-page PNG preserving the original orientation, aspect ratio, margins, "
    "tables, drawings, colors, stamps, signatures, logos, font scale, line breaks, reading order, "
    "and text positions. Translate each heading, note, label, and table cell from its own source "
    "text; do not copy neighboring labels or redesign the page. Keep numbers, units, dates, "
    "reference codes, proper names, contacts, stamps, signatures, logos, and text already in "
    "{target_language} unchanged. Do not crop, rotate, summarize, add explanations, add watermarks, "
    "or add/remove rows, columns, images, or layout elements."
)


class ImageModelUnavailableError(RuntimeError):
    """Raised for explicit model-level unavailability signals."""


class ImageGenerationClient(Protocol):
    def generate_page(
        self,
        *,
        source_image_path: Path,
        target_language: str,
        model_config: EffectiveModelConfig,
        review_feedback: str | None = None,
    ) -> bytes:
        """Return PNG/JPEG/WebP bytes for one translated page."""


@dataclass(frozen=True)
class ImageConnectivityResult:
    ok: bool
    message: str
    detail: str = ""
    status: str = "ok"


class OpenAICompatibleImageGenerationClient:
    """Best-effort image-generation client for OpenAI-compatible Responses APIs."""

    def __init__(self, *, timeout_seconds: float = IMAGE_GENERATION_TIMEOUT_SECONDS):
        self.timeout_seconds = timeout_seconds

    def generate_page(
        self,
        *,
        source_image_path: Path,
        target_language: str,
        model_config: EffectiveModelConfig,
        review_feedback: str | None = None,
    ) -> bytes:
        if not provider_supports_capability(model_config.provider, "image"):
            raise ImageModelUnavailableError(
                f"当前服务商不在图像生成能力列表中：{model_config.provider}"
            )
        if not model_config.api_key:
            raise ImageModelUnavailableError("图像生成模型缺少 API Key")
        if not model_config.model:
            raise ImageModelUnavailableError("图像生成模型名称不能为空")

        base_url = _normalize_base_url(model_config)
        prompt = build_pdf_image_translation_prompt(
            target_language,
            review_feedback=review_feedback,
        )
        if _is_gpt_image_model(model_config.model):
            return _generate_page_with_images_edit(
                source_image_path=source_image_path,
                prompt=prompt,
                model_config=model_config,
                base_url=base_url,
                timeout_seconds=self.timeout_seconds,
            )
        image_data_url = _image_data_url(source_image_path)
        payload = {
            "model": model_config.model,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {"type": "input_image", "image_url": image_data_url},
                    ],
                }
            ],
            "tools": [{"type": "image_generation"}],
        }
        headers = {
            "Authorization": f"Bearer {model_config.api_key}",
            "Content-Type": "application/json",
        }

        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.post(f"{base_url}/responses", headers=headers, json=payload)
                response.raise_for_status()
                response_payload = response.json()
        except Exception as exc:  # noqa: BLE001 - converted to page/model errors by caller.
            if is_model_unavailable_error(exc):
                raise ImageModelUnavailableError(str(exc)) from exc
            raise

        image_bytes = _extract_image_bytes(response_payload)
        if not image_bytes:
            raise ValueError("图像生成接口已响应，但没有返回可用图片。")
        return image_bytes


def _generate_page_with_images_edit(
    *,
    source_image_path: Path,
    prompt: str,
    model_config: EffectiveModelConfig,
    base_url: str,
    timeout_seconds: float,
) -> bytes:
    data = {
        "model": model_config.model,
        "prompt": prompt,
        "n": "1",
        "size": _pdf_page_image_size_for_model(source_image_path, model_config.model),
        "quality": "medium",
        "output_format": "png",
    }
    headers = {"Authorization": f"Bearer {model_config.api_key}"}
    try:
        with source_image_path.open("rb") as image_file:
            files = {
                "image": (
                    source_image_path.name,
                    image_file,
                    "image/png",
                )
            }
            with httpx.Client(timeout=timeout_seconds) as client:
                response = client.post(
                    f"{base_url}/images/edits",
                    headers=headers,
                    data=data,
                    files=files,
                )
                response.raise_for_status()
                response_payload = response.json()
    except Exception as exc:  # noqa: BLE001 - caller classifies page/model errors.
        if is_model_unavailable_error(exc):
            raise ImageModelUnavailableError(str(exc)) from exc
        raise

    image_bytes = _extract_image_bytes(response_payload)
    if not image_bytes:
        raise ValueError("图像编辑接口已响应，但没有返回可用图片。")
    return image_bytes


def build_pdf_image_translation_prompt(
    target_language: str,
    *,
    review_feedback: str | None = None,
) -> str:
    prompt = PDF_IMAGE_TRANSLATION_PROMPT.format(target_language=target_language)
    feedback = str(review_feedback or "").strip()
    if not feedback:
        return prompt
    return (
        f"{prompt} Previous candidate review found blocking issues. Regenerate the full "
        "page from the original source image, using the original page as the source of "
        "truth. Fix only these issues and preserve every correct existing element:\n"
        f"{feedback}"
    )


def check_image_generation_connectivity(
    settings: AppSettings,
    *,
    client: ImageGenerationClient | None = None,
    max_attempts: int = IMAGE_TEST_MAX_ATTEMPTS,
) -> ImageConnectivityResult:
    config = resolve_effective_model_config(settings, ROLE_IMAGE)
    signature = image_model_signature(settings)
    if not provider_supports_capability(config.provider, "image"):
        message = f"{config.label}当前服务商不支持图像生成能力：{config.provider}"
        record_image_model_availability(
            settings,
            ok=False,
            message=message,
            signature=signature,
            checked_at=_now_iso(),
        )
        return ImageConnectivityResult(False, message, status="unsupported_provider")

    client = client or OpenAICompatibleImageGenerationClient()
    with tempfile.TemporaryDirectory() as tmp:
        test_image = Path(tmp) / "image_connectivity_test.png"
        Image.new("RGB", (64, 64), "white").save(test_image, format="PNG")
        last_error = ""
        for attempt in range(1, max(1, int(max_attempts)) + 1):
            try:
                image_bytes = client.generate_page(
                    source_image_path=test_image,
                    target_language="English",
                    model_config=config,
                )
                Image.open(BytesIO(image_bytes)).verify()
                message = f"{config.label}连接测试通过。"
                record_image_model_availability(
                    settings,
                    ok=True,
                    message=message,
                    signature=signature,
                    checked_at=_now_iso(),
                )
                return ImageConnectivityResult(True, message)
            except Exception as exc:  # noqa: BLE001 - retry and report all image errors.
                last_error = _sanitize_error(exc)

    message = f"{config.label}连接测试失败：{last_error or '未知错误'}"
    record_image_model_availability(
        settings,
        ok=False,
        message=message,
        signature=signature,
        checked_at=_now_iso(),
    )
    return ImageConnectivityResult(False, message, detail=last_error, status="failed")


def is_model_unavailable_error(exc: BaseException) -> bool:
    text = _exception_text(exc).lower()
    if not text:
        return False
    narrow_markers = (
        "invalid api key",
        "incorrect api key",
        "unauthorized",
        "authentication",
        "forbidden",
        "insufficient_quota",
        "quota exceeded",
        "billing",
        "payment",
        "balance",
        "model not found",
        "model does not exist",
        "unsupported model",
        "image generation not supported",
        "not support image",
        "图像生成能力",
        "模型不存在",
        "余额不足",
        "额度不足",
        "欠费",
        "未授权",
    )
    return any(marker in text for marker in narrow_markers)


def _normalize_base_url(config: EffectiveModelConfig) -> str:
    if config.provider == "openai":
        return "https://api.openai.com/v1"
    base_url = str(config.base_url or "").strip().rstrip("/")
    if not base_url:
        raise ImageModelUnavailableError("图像生成模型缺少 Base URL")
    return base_url


def _is_gpt_image_model(model: str) -> bool:
    return str(model or "").strip().lower().startswith(GPT_IMAGE_MODEL_PREFIX)


def _pdf_page_image_size_for_model(source_image_path: Path, model: str) -> str:
    with Image.open(source_image_path) as image:
        width, height = image.size

    if model != GPT_IMAGE_2_MODEL:
        if width > height:
            return "1536x1024"
        if height > width:
            return "1024x1536"
        return "1024x1024"

    if max(width, height) <= 512:
        return "1024x1024"

    ratio = max(width, 1) / max(height, 1)
    if ratio >= 1:
        target_width = GPT_IMAGE_2_PDF_TARGET_LONG_EDGE
        target_height = target_width / ratio
    else:
        target_height = GPT_IMAGE_2_PDF_TARGET_LONG_EDGE
        target_width = target_height * ratio

    target_width, target_height = _fit_gpt_image_2_size(target_width, target_height)
    return f"{target_width}x{target_height}"


def _fit_gpt_image_2_size(width: float, height: float) -> tuple[int, int]:
    target_width = _floor_multiple(width, 16)
    target_height = _floor_multiple(height, 16)
    while target_width * target_height > GPT_IMAGE_2_MAX_PIXELS:
        if target_width >= target_height:
            target_width = _floor_multiple(target_width - 16, 16)
        else:
            target_height = _floor_multiple(target_height - 16, 16)

    short_edge = min(target_width, target_height)
    long_edge = max(target_width, target_height)
    min_scale = max(
        PDF_MIN_READABLE_SHORT_EDGE_PX / max(short_edge, 1),
        PDF_MIN_READABLE_LONG_EDGE_PX / max(long_edge, 1),
        math.sqrt(GPT_IMAGE_2_MIN_PIXELS / max(target_width * target_height, 1)),
    )
    if min_scale > 1:
        target_width = _ceil_multiple(target_width * min_scale, 16)
        target_height = _ceil_multiple(target_height * min_scale, 16)

    target_width = min(GPT_IMAGE_2_MAX_EDGE, target_width)
    target_height = min(GPT_IMAGE_2_MAX_EDGE, target_height)
    return max(16, target_width), max(16, target_height)


def _floor_multiple(value: float, multiple: int) -> int:
    return max(multiple, int(value) // multiple * multiple)


def _ceil_multiple(value: float, multiple: int) -> int:
    return max(multiple, math.ceil(value / multiple) * multiple)


def _image_data_url(path: Path) -> str:
    data = path.read_bytes()
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def _extract_image_bytes(payload: Any) -> bytes:
    for value in _walk_values(payload):
        if not isinstance(value, str) or len(value) < 32:
            continue
        if value.startswith("data:image/"):
            _, _, encoded = value.partition(",")
            return base64.b64decode(encoded)
        if _looks_like_base64_image(value):
            try:
                return base64.b64decode(value)
            except Exception:
                continue
    return b""


def _walk_values(value: Any):
    if isinstance(value, dict):
        preferred_keys = ("result", "b64_json", "image", "image_base64", "data")
        for key in preferred_keys:
            if key in value:
                yield value[key]
        for item in value.values():
            yield from _walk_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_values(item)
    else:
        yield value


def _looks_like_base64_image(value: str) -> bool:
    stripped = value.strip()
    if len(stripped) < 32:
        return False
    if stripped[:8].startswith("iVBOR") or stripped.startswith("/9j/"):
        return True
    return False


def _exception_text(exc: BaseException) -> str:
    parts = [str(exc), exc.__class__.__name__]
    response = getattr(exc, "response", None)
    if response is not None:
        for attr in ("status_code", "text"):
            value = getattr(response, attr, None)
            if value:
                parts.append(str(value))
    body = getattr(exc, "body", None)
    if body:
        try:
            parts.append(json.dumps(body, ensure_ascii=False))
        except TypeError:
            parts.append(str(body))
    return "\n".join(part for part in parts if part)


def _sanitize_error(exc: BaseException) -> str:
    text = _exception_text(exc).strip() or exc.__class__.__name__
    if len(text) > 500:
        return text[:497] + "..."
    return text


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")
