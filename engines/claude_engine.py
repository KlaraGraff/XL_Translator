"""
Claude (Anthropic) 翻译引擎。
使用 JSON Array 批处理格式，内置指数退避重试。
"""
import json

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
            "max_tokens": 8096,
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
