"""OpenAI-compatible translation through the shared text transport."""
import json
import httpx  # noqa: F401 - compatibility patch target for legacy integrations

from config import OPENAI_BASE_URL
from core.text_transport import (
    request_text,
)
from engines.base_engine import (
    TASK_INSTRUCTION, TranslationEngine, get_source_lang_name,
    get_target_lang_name, parse_response,
)

class OpenAIEngine(TranslationEngine):

    def __init__(
        self,
        api_key: str,
        # 没有默认模型：模型 ID 只能来自用户选择（实时从 /models 拉取）。
        # 写死一个默认值会随时间过期，而且永远轮不到它生效——调度层一律显式传入。
        model: str,
        base_url: str = "",
        api_mode: str = "auto",
        connection_id: str = "",
        engine_name_prefix: str = "openai",
        response_label: str = "OpenAI",
    ):
        self._connection_id = connection_id
        self._model = model
        self._api_key = api_key
        self._base_url = str(base_url or OPENAI_BASE_URL).rstrip("/")
        self._api_mode = str(api_mode or "").strip()
        self._engine_name_prefix = str(engine_name_prefix or "openai").strip()
        self._response_label = str(response_label or "OpenAI").strip()

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
        )
        full_system = f"{system_prompt}\n\n{instruction}".strip()
        user_msg    = json.dumps(texts, ensure_ascii=False)

        raw = self._call_api(full_system, user_msg)
        return parse_response(texts, raw, self._response_label)

    def _call_api(self, system: str, user_msg: str) -> str:
        text, self.last_route = request_text(
            base_url=self._base_url, api_key=self._api_key, model=self._model,
            system=system, user=user_msg, api_mode=self._api_mode or "auto",
            connection_id=self._connection_id,
        )
        return text

    def _call_responses_api(self, system: str, user_msg: str) -> str:
        return request_text(
            base_url=self._base_url, api_key=self._api_key, model=self._model,
            system=system, user=user_msg, api_mode="responses",
            connection_id=self._connection_id,
        )[0]

    def chat(self, system: str, user: str) -> str:
        return self._call_api(system, user)
