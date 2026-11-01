"""
Claude (Anthropic) 翻译引擎。
使用 JSON Array 批处理格式，内置指数退避重试。
"""
import json
import re

import httpx
from loguru import logger
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from config import (
    CLAUDE_BASE_URL,
    CLOUD_REQUEST_TIMEOUT,
    RETRY_MAX_ATTEMPTS,
    RETRY_WAIT_MIN,
    RETRY_WAIT_MAX,
)
from core import system_proxy
from core.prompt_cache import compose_system, record_usage, split_system
from engines.base_engine import (
    TASK_INSTRUCTION,
    TranslationEngine,
    get_source_lang_name,
    get_target_lang_name,
    is_retryable_engine_error,
    parse_response,
)


# ── 输出上限 ──────────────────────────────────────────────

# Claude 各代型号的单次输出上限不一样：3 系（opus/sonnet/haiku）只有 4096，
# 3.5 起至少 8192，4 代更高，不少第三方中转网关还会再压低一档。以前不分型号
# 一律发 8096：连通性测试只发 8 个 token，必然全绿；真实翻译却每批被 400
# invalid_request_error 拒掉，而 400 在批次重试里算「不可重试」，整批原样退回
# （CODE_AUDIT_2026-08-31 中-10）。
# 现在按型号名给上限，认不出的型号走所有 Claude 型号都接受的保守值；万一实际
# 上限比这里的还低，_call_api 会照报错里的数字自动降一档并记住，不再永久失败。
DEFAULT_MAX_OUTPUT_TOKENS = 4096
# 认得的新型号沿用改动前的输出预算（没有按型号继续往上加，免得改变现有行为）。
KNOWN_MAX_OUTPUT_TOKENS = 8096
_HIGH_OUTPUT_MODEL_RE = re.compile(
    r"claude-(?:3[.-][57]|(?:opus|sonnet|haiku)-[4-9])",
    re.IGNORECASE,
)
# 报错里认不出具体上限时，退到本次请求的一半，但不低于这个数：再小的话一批
# JSON 数组必然被截断，等于把 400 换成「响应解析失败」。
_MIN_MAX_OUTPUT_TOKENS = 512
_MAX_OUTPUT_TOKEN_DOWNGRADES = 3

# 「max_tokens 超过上限」的几种说法：Anthropic 的「max_tokens: 8096 > 4096,
# which is the maximum allowed number of tokens」，以及中转站常见的
# 「max_tokens is too large: 8096 (maximum 4096)」「max_tokens must be less
# than or equal to 2048」。别把「max_tokens 必须是正整数」这类同字段的报错也算进来。
_MAX_TOKENS_LIMIT_WORDS_RE = re.compile(
    r"max_tokens[^.\n]{0,80}"
    r"(?:too (?:large|long)|exceed|maximum|less than|at most|上限|超出|超过)",
    re.IGNORECASE,
)
_MAX_TOKENS_COMPARISON_RE = re.compile(
    r"max_tokens\D{0,40}?(\d+)\s*>\s*(\d+)", re.IGNORECASE
)


def model_max_output_tokens(model: str) -> int:
    """这个型号单次最多能输出多少 token（认不出的型号给保守值）。"""
    if _HIGH_OUTPUT_MODEL_RE.search(str(model or "")):
        return KNOWN_MAX_OUTPUT_TOKENS
    return DEFAULT_MAX_OUTPUT_TOKENS


def rejected_max_tokens(response: httpx.Response, requested: int) -> int | None:
    """400/422 是在嫌 max_tokens 太大时返回它说的上限，其余情况返回 None。"""
    if response.status_code not in {400, 422}:
        return None
    message = response.text or ""
    if "max_tokens" not in message.casefold():
        return None
    limit_words = _MAX_TOKENS_LIMIT_WORDS_RE.search(message)
    if limit_words is None:
        return None
    # 只看 max_tokens 后面这一小段：报文前面的错误码、请求地址里的数字都不是上限。
    window = message[limit_words.start():limit_words.start() + 120]
    comparison = _MAX_TOKENS_COMPARISON_RE.search(window)
    if comparison:
        # 第一个数是本次请求的值，第二个才是型号上限。
        limit = int(comparison.group(2))
    else:
        # 「max_tokens is too large: 8096」只说了一遍请求值：它不能当上限用。
        numbers = [
            int(value) for value in re.findall(r"\d{2,}", window) if int(value) < requested
        ]
        # 报文里给了上限就照它办；一个字都没给时只能减半，但不低于 _MIN_MAX_OUTPUT_TOKENS
        # ——再小的一批 JSON 数组必然被截断，等于把 400 换成「响应解析失败」。
        limit = min(numbers) if numbers else max(requested // 2, _MIN_MAX_OUTPUT_TOKENS)
    return limit if limit < requested else None


class ClaudeEngine(TranslationEngine):

    def __init__(self, api_key: str, model: str, base_url: str = ""):
        self._api_key = api_key
        self._model = model
        self._base_url = str(base_url or CLAUDE_BASE_URL).rstrip("/")
        self._plain_system = False
        self._max_output_tokens = model_max_output_tokens(model)

    @property
    def engine_name(self) -> str:
        return f"claude/{self._model}"

    def translate_batch(
        self,
        texts: list[str],
        target_lang: str,
        system_prompt: str,
        source_lang: str = "zh",
    ) -> dict[str, str]:
        if not texts:
            return {}

        source_lang_name = get_source_lang_name(source_lang)
        target_lang_name = get_target_lang_name(target_lang)
        instruction = TASK_INSTRUCTION.format(
            source_lang_name=source_lang_name,
            target_lang_name=target_lang_name,
        )
        full_system = compose_system(system_prompt, instruction)
        user_msg    = json.dumps(texts, ensure_ascii=False)

        raw = self._call_api(full_system, user_msg)
        return parse_response(texts, raw, "Claude")

    def _call_api(self, system: str, user_msg: str) -> str:
        # 正文提取放在重试之外：响应里没有 text 块是这一批内容本身的问题，
        # 原样重发只会再计费一次，不可能变好（审计 中-11）。
        return _extract_claude_text(self._request_messages(system, user_msg))

    @retry(
        stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
        wait=wait_exponential(min=RETRY_WAIT_MIN, max=RETRY_WAIT_MAX),
        retry=retry_if_exception(is_retryable_engine_error),
        reraise=True,
    )
    def _request_messages(self, system: str, user_msg: str) -> object:
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        stable, volatile = split_system(system)
        payload = {
            "model": self._model,
            "max_tokens": self._max_output_tokens,
            "system": self._system_field(stable, volatile),
            "messages": [{"role": "user", "content": user_msg}],
        }
        with system_proxy.http_client(timeout=CLOUD_REQUEST_TIMEOUT) as client:
            response = client.post(
                f"{self._base_url}/messages",
                headers=headers,
                json=payload,
            )
            if (
                response.status_code in {400, 422}
                and not self._plain_system
                and any(word in response.text.casefold() for word in ("cache", "system"))
            ):
                # 转发 Claude 的平台不认缓存标记：去掉标记重发一次，之后这个引擎
                # 都不再带（maple 2026-10-13：第三方也按官方做法加，被退回再去掉）。
                self._plain_system = True
                payload["system"] = self._system_field(stable, volatile)
                response = client.post(
                    f"{self._base_url}/messages",
                    headers=headers,
                    json=payload,
                )
            reduced_max_tokens = rejected_max_tokens(response, payload["max_tokens"])
            for _ in range(_MAX_OUTPUT_TOKEN_DOWNGRADES):
                if reduced_max_tokens is None:
                    break
                # 型号认不准、实际输出上限比请求值低：照报错说的上限降一档重发，
                # 并记住这个值，后面每一批都不再撞。400 本身不可重试，不在这里
                # 消化掉就会让整批退回原文（审计中-10）。
                logger.warning(
                    f"[Claude] {self._model} 的输出上限低于 {payload['max_tokens']}，"
                    f"按 {reduced_max_tokens} 重发本批"
                )
                self._max_output_tokens = reduced_max_tokens
                payload["max_tokens"] = reduced_max_tokens
                response = client.post(
                    f"{self._base_url}/messages",
                    headers=headers,
                    json=payload,
                )
                reduced_max_tokens = rejected_max_tokens(response, reduced_max_tokens)
            response.raise_for_status()
            response_payload = response.json()
        if isinstance(response_payload, dict):
            record_usage(response_payload.get("usage"))
        return response_payload

    def _system_field(self, stable: str, volatile: str):
        """固定段末尾打上缓存标记（Claude 只缓存标记之前的部分），本批段放在标记后面。"""
        if self._plain_system:
            return "\n\n".join(part for part in (stable, volatile) if part)
        blocks: list[dict] = []
        if stable:
            blocks.append({"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}})
        if volatile:
            blocks.append({"type": "text", "text": volatile})
        return blocks

    def chat(self, system: str, user: str) -> str:
        return self._call_api(system, user)


def _extract_claude_text(payload: object) -> str:
    if not isinstance(payload, dict):
        raise ValueError("Claude API 返回格式异常")
    content = payload.get("content")
    if not isinstance(content, list) or not content:
        raise ValueError("Claude API 返回未包含 content")
    # 首块不一定是正文：网关替用户打开思考模式时，content[0] 是 thinking /
    # redacted_thinking，真正的译文在后面的 text 块里。只看 content[0] 会让
    # 这些 200 的响应被当成解析失败，再被二分重发一遍，每次照常计费（审计 中-11）。
    parts = [
        block["text"]
        for block in content
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
    ]
    if not parts:
        first_block = content[0]
        # 把真因写进异常文案：日志里看到的是「首块是 thinking」而不是笼统的格式错。
        first_type = first_block.get("type") if isinstance(first_block, dict) else type(first_block).__name__
        raise ValueError(
            f"Claude API 返回未包含文本块（stop_reason={payload.get('stop_reason')!r}，"
            f"首块 type={first_type!r}）"
        )
    text = "".join(parts)
    if payload.get("stop_reason") == "max_tokens":
        # 截断的 JSON 数组解析不过去，会伪装成「格式错」，日志里查不出真因；
        # 也不该二分重发——半截的输出换一半的量重新请求，还是会被同样的上限截断。
        raise ValueError("Claude API 输出被截断（stop_reason=max_tokens），本批译文不完整")
    return text
