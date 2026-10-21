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
from loguru import logger
from PIL import Image

from config import is_siliconflow_endpoint, normalize_cloud_base_url
from core.image_generation import is_model_unavailable_error, looks_like_wrong_image_model
from core.user_facing_errors import humanize_error
from core.model_roles import (
    EffectiveModelConfig,
    ROLE_PDF_REVIEW,
    pdf_review_model_signature,
    provider_supports_capability,
    unsupported_capability_message,
    record_pdf_review_model_availability,
    resolve_effective_model_config,
)
from settings import AppSettings


# 审核跟生成用的是同一家中转站，按次计费、跟耗时无关：客户端超时不省钱，
# 只是把已经付过钱的结果扔掉再重试一次。跟 image_generation.py 里的
# IMAGE_GENERATION_TIMEOUT_SECONDS 一起从 180 调到 240，理由同源，见那边注释。
PDF_REVIEW_TIMEOUT_SECONDS = 240.0
# 「测试连接」同样不跟着涨，理由见 image_generation.py 的 IMAGE_TEST_TIMEOUT_SECONDS。
PDF_REVIEW_TEST_TIMEOUT_SECONDS = 90.0
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


# 没有 OpenAI ``/responses`` 接口、只能走 ``/chat/completions`` 的服务商。
CHAT_COMPLETIONS_ONLY_PROVIDERS = frozenset({"siliconflow"})
_ROUTE_PATHS = {"chat": "chat/completions", "responses": "responses"}


def review_routes(config: EffectiveModelConfig) -> list[str]:
    """按连接的「文本请求协议」决定审核请求走哪种接口，和文档翻译同一个设置。

    以前这里写死只发 OpenAI 的 ``/responses``，只有 chat 接口的服务商（硅基流动等）
    无论选哪个模型都失败。指定了协议就只走那一种；自动时先走 ``/responses``
    （原有行为，OpenAI 与多数中转站已验证），对方没有这个接口再换 chat。
    """
    mode = str(config.api_mode or "auto").strip()
    mode = {"codex_responses": "responses", "chat_completions": "chat"}.get(mode, mode)
    if mode in _ROUTE_PATHS:
        return [mode]
    if config.provider in CHAT_COMPLETIONS_ONLY_PROVIDERS or is_siliconflow_endpoint(
        config.provider, config.base_url
    ):
        return ["chat"]
    return ["responses", "chat"]


def _route_missing(exc: httpx.HTTPStatusError, kind: str) -> bool:
    if kind == "route":
        return True
    # 没有这个接口时，很多服务直接回一个空白或通用的 404/405，认不出具体原因。
    return kind == "request" and exc.response.status_code in {404, 405}


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
                unsupported_capability_message(
                    "PDF 与图片审核模型", model_config.provider, "vision_text"
                )
            )
        if not model_config.api_key:
            raise PdfReviewModelUnavailableError("PDF 与图片审核模型尚未填写 API 密钥")
        if not model_config.model:
            raise PdfReviewModelUnavailableError("PDF 与图片审核模型的名称不能为空")

        base_url = _normalize_base_url(model_config)
        from core.model_auto_upgrade import (
            effective_model_after_rollback,
            rollback_upgraded_model,
        )
        from core.text_transport import classify_http_error, extract_chat_text

        effective_model = effective_model_after_rollback(
            model_config.connection_id, model_config.model,
            model_config.base_url, ROLE_PDF_REVIEW,
        )
        user_text = (
            f"Target language: {target_language}. "
            "First image is the original source PDF page. "
            "Second image is the translated candidate page."
        )
        builders = {"chat": _chat_review_payload, "responses": _responses_review_payload}
        headers = {
            "Authorization": f"Bearer {model_config.api_key}",
            "Content-Type": "application/json",
        }
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                routes = review_routes(model_config)
                for index, route in enumerate(routes):
                    payload = builders[route](
                        effective_model, user_text, source_image_path, translated_image_path,
                    )
                    url = f"{base_url}/{_ROUTE_PATHS[route]}"
                    response = client.post(url, headers=headers, json=payload)
                    try:
                        response.raise_for_status()
                    except httpx.HTTPStatusError as exc:
                        kind = classify_http_error(exc)[0]
                        if index + 1 < len(routes) and _route_missing(exc, kind):
                            # 自动模式：这家没有这种接口，换另一种再发一次。
                            logger.debug(f"[PDF审核] {url} 不可用（{exc.response.status_code}），改用下一种接口。")
                            continue
                        if kind != "model":
                            raise
                        previous = rollback_upgraded_model(
                            model_config.connection_id, effective_model,
                            model_config.base_url, api_key=model_config.api_key,
                            model_role=ROLE_PDF_REVIEW,
                        )
                        if not previous:
                            raise
                        payload["model"] = previous
                        response = client.post(url, headers=headers, json=payload)
                        response.raise_for_status()
                    response_payload = response.json()
                    break
        except Exception as exc:  # noqa: BLE001 - caller classifies page/model errors.
            if is_model_unavailable_error(exc):
                logger.debug(f"[PDF审核] 审核模型不可用原始错误：{exc!r}")
                raise PdfReviewModelUnavailableError(
                    humanize_error(
                        str(exc),
                        fallback=(
                            "PDF 与图片审核模型当前不可用，请在设置中检查此连接的 API 密钥、"
                            "模型名称和账户额度。"
                        ),
                    )
                ) from exc
            raise

        if route == "chat":
            text = extract_chat_text(response_payload)
        else:
            text = _extract_response_text(response_payload)
        return parse_pdf_review_result(text)


def _responses_review_payload(
    model: str, user_text: str, source_image_path: Path, translated_image_path: Path,
) -> dict[str, Any]:
    return {
        "model": model,
        "instructions": PDF_PAGE_REVIEW_PROMPT,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": user_text},
                    {"type": "input_image", "image_url": _image_data_url(source_image_path)},
                    {"type": "input_image", "image_url": _image_data_url(translated_image_path)},
                ],
            }
        ],
        "store": False,
        "stream": False,
    }


def _chat_review_payload(
    model: str, user_text: str, source_image_path: Path, translated_image_path: Path,
) -> dict[str, Any]:
    # 只有 /chat/completions 的服务商（硅基流动）：同一组内容换成 chat 的 image_url 写法。
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": PDF_PAGE_REVIEW_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": _image_data_url(source_image_path)}},
                    {"type": "image_url", "image_url": {"url": _image_data_url(translated_image_path)}},
                ],
            },
        ],
        "stream": False,
    }


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
                    problem="审核模型的回复格式有误，无法据此判断此页。",
                    suggestion="请重新审核，或更换更稳定的图片识别模型。",
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


REVIEW_MODEL_ADVICE = "PDF 与图片审核需要能识别图片的模型，请换一个支持图片输入的模型。"


def check_pdf_review_connectivity(
    settings: AppSettings,
    *,
    client: PdfPageReviewClient | None = None,
    max_attempts: int = PDF_REVIEW_TEST_MAX_ATTEMPTS,
    connection_id: str = "",
) -> PdfReviewConnectivityResult:
    # ``connection_id`` 指定要测的连接池条目；不给就还是主连接。
    config = resolve_effective_model_config(
        settings,
        ROLE_PDF_REVIEW,
        connection_id=connection_id,
    )
    signature = pdf_review_model_signature(settings, connection_id)
    if not provider_supports_capability(config.provider, "vision_text"):
        message = "此连接的服务商不支持图片审核，请更换连接。"
        record_pdf_review_model_availability(
            settings,
            ok=False,
            message=message,
            signature=signature,
            checked_at=_now_iso(),
            connection_id=connection_id,
        )
        return PdfReviewConnectivityResult(False, message, status="unsupported_provider")

    client = client or OpenAICompatiblePdfReviewClient(
        timeout_seconds=PDF_REVIEW_TEST_TIMEOUT_SECONDS
    )
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "source.png"
        translated = Path(tmp) / "translated.png"
        Image.new("RGB", (64, 64), "white").save(source, format="PNG")
        Image.new("RGB", (64, 64), "white").save(translated, format="PNG")
        last_error = ""
        last_exc: BaseException | None = None
        for _attempt in range(1, max(1, int(max_attempts)) + 1):
            try:
                result = client.review_page(
                    source_image_path=source,
                    translated_image_path=translated,
                    target_language="English",
                    model_config=config,
                )
                message = "连接测试通过。"
                record_pdf_review_model_availability(
                    settings,
                    ok=True,
                    message=message,
                    signature=signature,
                    checked_at=_now_iso(),
                    connection_id=connection_id,
                )
                detail = result.summary or "审核模型已正常返回判断结果。"
                return PdfReviewConnectivityResult(True, message, detail=detail)
            except Exception as exc:  # noqa: BLE001 - retry and report all review errors.
                last_error = _sanitize_error(exc)
                last_exc = exc

    message = last_error or "审核模型未正常响应，请检查此连接的接口地址、API 密钥和模型名称。"
    if last_exc is not None and looks_like_wrong_image_model(last_exc):
        # 整句换掉：原来那句「请检查接口地址、API 密钥和模型名称」会把人引去查密钥。
        message = f"{config.label}连接测试失败：{REVIEW_MODEL_ADVICE}"
    record_pdf_review_model_availability(
        settings,
        ok=False,
        message=message,
        signature=signature,
        checked_at=_now_iso(),
        connection_id=connection_id,
    )
    return PdfReviewConnectivityResult(False, message, detail=last_error, status="failed")


def _normalize_base_url(config: EffectiveModelConfig) -> str:
    base_url = normalize_cloud_base_url(config.provider, config.base_url)
    if not base_url:
        raise PdfReviewModelUnavailableError("PDF 与图片审核模型尚未填写接口地址")
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
        # Follow only the best payload key and stop: also walking the other
        # values would yield the same text a second time plus id/role/type
        # metadata, and the doubled JSON never parses again.  Container keys
        # ("output", "choices") must outrank "text" because the Responses API
        # top level also has a "text" field that only holds format options.
        for key in ("output_text", "output", "choices", "message", "content", "text"):
            if key in value:
                yield from _walk_values(value[key])
                return
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
    """Full diagnostic text — for logs only; it carries the upstream JSON body."""
    parts = [str(exc), exc.__class__.__name__]
    response = getattr(exc, "response", None)
    if response is not None:
        for attr in ("status_code", "text"):
            value = getattr(response, attr, None)
            if value:
                parts.append(str(value))
    return "\n".join(part for part in parts if part)


def _sanitize_error(exc: BaseException) -> str:
    """Return the one sentence the connection-test panel shows the user.

    The diagnostic text — exception class, HTTP status, the provider's JSON error
    body — goes to the debug log.  Handing it to the panel put a request URL and
    a raw envelope in front of someone who only wanted to know whether the
    connection works.
    """
    logger.debug(f"[PDF审核] 连接测试失败原始错误：{_exception_text(exc)}")
    return humanize_error(
        str(exc),
        fallback="审核模型未正常响应，请检查此连接的接口地址、API 密钥和模型名称。",
    )


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")
