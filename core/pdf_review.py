"""Multimodal review helpers for PDF image-layout translation."""

from __future__ import annotations

import base64
import json
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import httpx
from PIL import Image

from config import normalize_cloud_base_url
from core.image_generation import is_model_unavailable_error
from core.model_roles import (
    EffectiveModelConfig,
    ROLE_PDF_REVIEW,
    pdf_review_model_signature,
    provider_supports_capability,
    record_pdf_review_model_availability,
    resolve_effective_model_config,
)
from settings import AppSettings


PDF_REVIEW_TIMEOUT_SECONDS = 180.0
PDF_REVIEW_TEST_MAX_ATTEMPTS = 3

PDF_PAGE_REVIEW_PROMPT = (
    "You are auditing a PDF page image translation. Compare the source page image and "
    "the translated page image. Decide only whether the translated page can be used. "
    "Blocking issues include missing translated text, wrong translation, copied neighboring "
    "cell labels, changed numbers, dates, units, reference codes, proper names, contacts, "
    "stamps or signatures, table/grid changes, added or removed rows/columns, crop, rotation, "
    "watermarks, summaries, or layout redesign. Minor style suggestions may be reported, but "
    "they must not change the pass/fail decision. Return strict JSON only with keys: pass "
    "(boolean), blocking_issues (array), minor_suggestions (array of strings), summary (string)."
)


class PdfReviewModelUnavailableError(RuntimeError):
    """Raised for explicit PDF review model unavailability signals."""


@dataclass(frozen=True)
class PdfReviewIssue:
    type: str = "review_issue"
    location: str = ""
    problem: str = ""
    suggestion: str = ""


@dataclass(frozen=True)
class PdfPageReviewResult:
    passed: bool
    blocking_issues: list[PdfReviewIssue] = field(default_factory=list)
    minor_suggestions: list[str] = field(default_factory=list)
    summary: str = ""
    raw_text: str = ""

    def to_manifest(self) -> dict[str, Any]:
        return {
            "pass": self.passed,
            "blocking_issues": [issue.__dict__ for issue in self.blocking_issues],
            "minor_suggestions": list(self.minor_suggestions),
            "summary": self.summary,
            "raw_text": self.raw_text,
        }


@dataclass(frozen=True)
class PdfReviewConnectivityResult:
    ok: bool
    message: str
    detail: str = ""
    status: str = "ok"


class PdfPageReviewClient(Protocol):
    def review_page(
        self,
        *,
        source_image_path: Path,
        translated_image_path: Path,
        target_language: str,
        model_config: EffectiveModelConfig,
    ) -> PdfPageReviewResult:
        """Return a pass/fail review for one translated page candidate."""


class OpenAICompatiblePdfReviewClient:
    """Multimodal page-review client for OpenAI-compatible Responses APIs."""

    def __init__(self, *, timeout_seconds: float = PDF_REVIEW_TIMEOUT_SECONDS):
        self.timeout_seconds = timeout_seconds

    def review_page(
        self,
        *,
        source_image_path: Path,
        translated_image_path: Path,
        target_language: str,
        model_config: EffectiveModelConfig,
    ) -> PdfPageReviewResult:
        if not provider_supports_capability(model_config.provider, "vision_text"):
            raise PdfReviewModelUnavailableError(
                f"当前服务商不在图像理解审核能力列表中：{model_config.provider}"
            )
        if not model_config.api_key:
            raise PdfReviewModelUnavailableError("PDF 翻译审核模型缺少 API Key")
        if not model_config.model:
            raise PdfReviewModelUnavailableError("PDF 翻译审核模型名称不能为空")

        base_url = _normalize_base_url(model_config)
        payload = {
            "model": model_config.model,
            "instructions": PDF_PAGE_REVIEW_PROMPT,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                f"Target language: {target_language}. "
                                "First image is the original source PDF page. "
                                "Second image is the translated candidate page."
                            ),
                        },
                        {
                            "type": "input_image",
                            "image_url": _image_data_url(source_image_path),
                        },
                        {
                            "type": "input_image",
                            "image_url": _image_data_url(translated_image_path),
                        },
                    ],
                }
            ],
            "store": False,
            "stream": False,
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
        except Exception as exc:  # noqa: BLE001 - caller classifies page/model errors.
            if is_model_unavailable_error(exc):
                raise PdfReviewModelUnavailableError(str(exc)) from exc
            raise

        text = _extract_response_text(response_payload)
        return parse_pdf_review_result(text)


def parse_pdf_review_result(text: str) -> PdfPageReviewResult:
    raw_text = str(text or "").strip()
    payload = _extract_json_object(raw_text)
    if payload is None:
        normalized = raw_text.strip().upper()
        if normalized in {"Y", "YES", "PASS", "TRUE"}:
            return PdfPageReviewResult(True, summary="审核通过。", raw_text=raw_text)
        if normalized in {"N", "NO", "FAIL", "FALSE"}:
            return PdfPageReviewResult(
                False,
                blocking_issues=[
                    PdfReviewIssue(
                        type="review_failed",
                        problem="审核模型判定未通过，但未返回具体问题。",
                    )
                ],
                summary="审核未通过。",
                raw_text=raw_text,
            )
        return PdfPageReviewResult(
            False,
            blocking_issues=[
                PdfReviewIssue(
                    type="invalid_review_response",
                    problem="审核模型未返回可解析的 JSON 判断。",
                    suggestion="请重新审核或改用更稳定的多模态模型。",
                )
            ],
            summary="审核结果无法解析。",
            raw_text=raw_text,
        )

    blocking_issues = [
        _normalize_issue(item)
        for item in payload.get("blocking_issues") or payload.get("issues") or []
        if isinstance(item, dict)
    ]
    minor_suggestions = [
        str(item).strip()
        for item in payload.get("minor_suggestions") or []
        if str(item).strip()
    ]
    passed = bool(payload.get("pass") is True or str(payload.get("pass")).upper() == "Y")
    if not passed and not blocking_issues:
        blocking_issues = [
            PdfReviewIssue(
                type="review_failed",
                problem=str(payload.get("summary") or "审核判定未通过。"),
            )
        ]
    return PdfPageReviewResult(
        passed=passed,
        blocking_issues=blocking_issues,
        minor_suggestions=minor_suggestions,
        summary=str(payload.get("summary") or "").strip(),
        raw_text=raw_text,
    )


def check_pdf_review_connectivity(
    settings: AppSettings,
    *,
    client: PdfPageReviewClient | None = None,
    max_attempts: int = PDF_REVIEW_TEST_MAX_ATTEMPTS,
) -> PdfReviewConnectivityResult:
    config = resolve_effective_model_config(settings, ROLE_PDF_REVIEW)
    signature = pdf_review_model_signature(settings)
    if not provider_supports_capability(config.provider, "vision_text"):
        message = f"{config.label}当前服务商不支持图像理解审核能力：{config.provider}"
        record_pdf_review_model_availability(
            settings,
            ok=False,
            message=message,
            signature=signature,
            checked_at=_now_iso(),
        )
        return PdfReviewConnectivityResult(False, message, status="unsupported_provider")

    client = client or OpenAICompatiblePdfReviewClient()
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "source.png"
        translated = Path(tmp) / "translated.png"
        Image.new("RGB", (64, 64), "white").save(source, format="PNG")
        Image.new("RGB", (64, 64), "white").save(translated, format="PNG")
        last_error = ""
        for _attempt in range(1, max(1, int(max_attempts)) + 1):
            try:
                result = client.review_page(
                    source_image_path=source,
                    translated_image_path=translated,
                    target_language="English",
                    model_config=config,
                )
                message = f"{config.label}连接测试通过。"
                record_pdf_review_model_availability(
                    settings,
                    ok=True,
                    message=message,
                    signature=signature,
                    checked_at=_now_iso(),
                )
                detail = result.summary or "审核模型已返回结构化判断。"
                return PdfReviewConnectivityResult(True, message, detail=detail)
            except Exception as exc:  # noqa: BLE001 - retry and report all review errors.
                last_error = _sanitize_error(exc)

    message = f"{config.label}连接测试失败：{last_error or '未知错误'}"
    record_pdf_review_model_availability(
        settings,
        ok=False,
        message=message,
        signature=signature,
        checked_at=_now_iso(),
    )
    return PdfReviewConnectivityResult(False, message, detail=last_error, status="failed")


def _normalize_base_url(config: EffectiveModelConfig) -> str:
    base_url = normalize_cloud_base_url(config.provider, config.base_url)
    if not base_url:
        raise PdfReviewModelUnavailableError("PDF 翻译审核模型缺少 Base URL")
    return base_url


def _image_data_url(path: Path) -> str:
    data = path.read_bytes()
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def _extract_response_text(payload: Any) -> str:
    texts: list[str] = []
    for value in _walk_values(payload):
        if isinstance(value, str) and value.strip():
            texts.append(value.strip())
    return "\n".join(texts).strip()


def _walk_values(value: Any):
    if isinstance(value, dict):
        for key in ("output_text", "text", "content"):
            if key in value:
                yield value[key]
        for item in value.values():
            yield from _walk_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_values(item)
    else:
        yield value


def _extract_json_object(text: str) -> dict[str, Any] | None:
    stripped = str(text or "").strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?", "", stripped, flags=re.IGNORECASE).strip()
        stripped = re.sub(r"```$", "", stripped).strip()
    candidates = [stripped]
    start = stripped.find("{")
    end = stripped.rfind("}")
    if 0 <= start < end:
        candidates.append(stripped[start : end + 1])
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except Exception:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _normalize_issue(item: dict[str, Any]) -> PdfReviewIssue:
    return PdfReviewIssue(
        type=str(item.get("type") or "review_issue").strip() or "review_issue",
        location=str(item.get("location") or "").strip(),
        problem=str(item.get("problem") or item.get("message") or "").strip(),
        suggestion=str(item.get("suggestion") or "").strip(),
    )


def _exception_text(exc: BaseException) -> str:
    parts = [str(exc), exc.__class__.__name__]
    response = getattr(exc, "response", None)
    if response is not None:
        for attr in ("status_code", "text"):
            value = getattr(response, attr, None)
            if value:
                parts.append(str(value))
    return "\n".join(part for part in parts if part)


def _sanitize_error(exc: BaseException) -> str:
    text = _exception_text(exc).strip() or exc.__class__.__name__
    if len(text) > 500:
        return text[:497] + "..."
    return text


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")
