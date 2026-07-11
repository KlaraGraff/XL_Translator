"""
OpenAI 兼容翻译引擎。
覆盖：OpenAI / 硅基流动 / 自定义 OpenAI 兼容接口。
"""
import json
from urllib.parse import urlparse

import httpx

from tenacity import retry, stop_after_attempt, wait_exponential

from config import CLOUD_REQUEST_TIMEOUT, RETRY_MAX_ATTEMPTS, RETRY_WAIT_MIN, RETRY_WAIT_MAX
from core.translation_protocol import REPLACE_TRANSLATION_PREFIX
from engines.base_engine import (
    TASK_INSTRUCTION,
    TranslationEngine,
    get_source_lang_name,
    get_target_lang_name,
    parse_response,
)


def _supports_asxs_responses_route(base_url: str) -> bool:
    normalized = str(base_url or "").strip()
    if not normalized:
        return False
    parsed = urlparse(normalized)
    return parsed.netloc.lower() == "api.asxs.top"


def _extract_text_from_responses_events(lines) -> str:
    deltas: list[str] = []
    done_texts: list[str] = []

    for raw_line in lines:
        if not raw_line:
            continue

        line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else str(raw_line)
        if not line.startswith("data: "):
            continue

        payload = line[6:]
        if payload == "[DONE]":
            continue

        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            continue

        if isinstance(data.get("delta"), str):
            deltas.append(data["delta"])
            continue

        if isinstance(data.get("text"), str):
            done_texts.append(data["text"])
            continue

        item = data.get("item")
        if isinstance(item, dict):
            for content in item.get("content") or []:
                if not isinstance(content, dict):
                    continue
                if isinstance(content.get("text"), str):
                    done_texts.append(content["text"])

    if deltas:
        return "".join(deltas)
    if done_texts:
        return done_texts[-1]
    return ""


class OpenAIEngine(TranslationEngine):

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o",
        base_url: str = "",
        api_mode: str = "",
        engine_name_prefix: str = "openai",
    ):
        import openai
        kwargs: dict = {
            "api_key": api_key,
            "timeout": CLOUD_REQUEST_TIMEOUT,
            "max_retries": 0,
        }
        if base_url:
            kwargs["base_url"] = base_url
        self._client = openai.OpenAI(**kwargs)
        self._model = model
        self._api_key = api_key
        self._base_url = str(base_url or "").rstrip("/")
        self._api_mode = str(api_mode or "").strip()
        self._engine_name_prefix = str(engine_name_prefix or "openai").strip()

    @property
    def engine_name(self) -> str:
        return f"{self._engine_name_prefix}/{self._model}"

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
        return parse_response(texts, raw, "OpenAI")

    def _use_responses_api(self) -> bool:
        if self._api_mode == "codex_responses":
            return True
        if self._api_mode and self._api_mode != "codex_responses":
            return False
        return _supports_asxs_responses_route(self._base_url)

    @retry(
        stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
        wait=wait_exponential(min=RETRY_WAIT_MIN, max=RETRY_WAIT_MAX),
        reraise=True,
    )
    def _call_api(self, system: str, user_msg: str) -> str:
        if self._use_responses_api():
            return self._call_responses_api(system, user_msg)
        return self._call_chat_completions_api(system, user_msg)

    def _call_chat_completions_api(self, system: str, user_msg: str) -> str:
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": user_msg},
            ],
        )
        return response.choices[0].message.content or ""

    def _call_responses_api(self, system: str, user_msg: str) -> str:
        if not self._base_url:
            raise ValueError("Responses API 调用缺少 base_url 配置")

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        payload = {
            "model": self._model,
            "instructions": system,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": user_msg,
                        }
                    ],
                }
            ],
            "store": False,
            "stream": True,
            "include": ["reasoning.encrypted_content"],
        }

        with httpx.Client(timeout=CLOUD_REQUEST_TIMEOUT) as client:
            with client.stream(
                "POST",
                f"{self._base_url}/responses",
                headers=headers,
                json=payload,
            ) as response:
                response.raise_for_status()
                text = _extract_text_from_responses_events(response.iter_lines())

        if not text.strip():
            raise ValueError("Responses API 返回成功但未包含可解析正文")
        return text

    def chat(self, system: str, user: str) -> str:
        return self._call_api(system, user)
