"""
Claude (Anthropic) 翻译引擎。
使用 JSON Array 批处理格式，内置指数退避重试。
"""
import json

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from config import (
    CLAUDE_BASE_URL,
    CLOUD_REQUEST_TIMEOUT,
    RETRY_MAX_ATTEMPTS,
    RETRY_WAIT_MIN,
    RETRY_WAIT_MAX,
)
from core.translation_protocol import REPLACE_TRANSLATION_PREFIX
from engines.base_engine import (
    TASK_INSTRUCTION,
    TranslationEngine,
    get_source_lang_name,
    get_target_lang_name,
    parse_response,
)


class ClaudeEngine(TranslationEngine):

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-6", base_url: str = ""):
        self._api_key = api_key
        self._model = model
        self._base_url = str(base_url or CLAUDE_BASE_URL).rstrip("/")

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
            replace_prefix=REPLACE_TRANSLATION_PREFIX,
        )
        full_system = f"{system_prompt}\n\n{instruction}".strip()
        user_msg    = json.dumps(texts, ensure_ascii=False)

        raw = self._call_api(full_system, user_msg)
        return parse_response(texts, raw, "Claude")

    @retry(
        stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
        wait=wait_exponential(min=RETRY_WAIT_MIN, max=RETRY_WAIT_MAX),
        reraise=True,
    )
    def _call_api(self, system: str, user_msg: str) -> str:
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._model,
            "max_tokens": 8096,
            "system": system,
            "messages": [{"role": "user", "content": user_msg}],
        }
        with httpx.Client(timeout=CLOUD_REQUEST_TIMEOUT) as client:
            response = client.post(
                f"{self._base_url}/messages",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            response_payload = response.json()
        return _extract_claude_text(response_payload)

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
