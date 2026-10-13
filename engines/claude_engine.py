"""
Claude (Anthropic) 翻译引擎。
使用 JSON Array 批处理格式，内置指数退避重试。
"""
import json

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from config import (
    CLAUDE_BASE_URL,
    CLOUD_REQUEST_TIMEOUT,
    RETRY_MAX_ATTEMPTS,
    RETRY_WAIT_MIN,
    RETRY_WAIT_MAX,
)
from core.prompt_cache import compose_system, record_usage, split_system
from engines.base_engine import (
    TASK_INSTRUCTION,
    TranslationEngine,
    get_source_lang_name,
    get_target_lang_name,
    is_retryable_engine_error,
    parse_response,
)


class ClaudeEngine(TranslationEngine):

    def __init__(self, api_key: str, model: str, base_url: str = ""):
        self._api_key = api_key
        self._model = model
        self._base_url = str(base_url or CLAUDE_BASE_URL).rstrip("/")
        self._plain_system = False

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

    @retry(
        stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
        wait=wait_exponential(min=RETRY_WAIT_MIN, max=RETRY_WAIT_MAX),
        retry=retry_if_exception(is_retryable_engine_error),
        reraise=True,
    )
    def _call_api(self, system: str, user_msg: str) -> str:
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        stable, volatile = split_system(system)
        payload = {
            "model": self._model,
            "max_tokens": 8096,
            "system": self._system_field(stable, volatile),
            "messages": [{"role": "user", "content": user_msg}],
        }
        with httpx.Client(timeout=CLOUD_REQUEST_TIMEOUT) as client:
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
            response.raise_for_status()
            response_payload = response.json()
        if isinstance(response_payload, dict):
            record_usage(response_payload.get("usage"))
        return _extract_claude_text(response_payload)

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
    first_block = content[0]
    if not isinstance(first_block, dict):
        raise ValueError("Claude API 返回 content 格式异常")
    text = first_block.get("text")
    return text if isinstance(text, str) else ""
