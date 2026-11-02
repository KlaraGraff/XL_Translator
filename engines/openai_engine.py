"""OpenAI-compatible translation through the shared text transport."""

from config import OPENAI_BASE_URL
from core.text_transport import (
    request_text,
)
from engines.base_engine import BatchTranslateMixin, TranslationEngine


class OpenAIEngine(BatchTranslateMixin, TranslationEngine):

    def __init__(
        self,
        api_key: str,
        # 没有默认模型：模型 ID 只能来自用户选择（实时从 /models 拉取）。
        # 写死一个默认值会随时间过期，而且永远轮不到它生效——调度层一律显式传入。
        model: str,
        base_url: str = "",
        api_mode: str = "auto",
        connection_id: str = "",
        model_role: str = "translation",
        engine_name_prefix: str = "openai",
        response_label: str = "OpenAI",
        chat_extra: dict | None = None,
    ):
        self._connection_id = connection_id
        self._model_role = model_role
        self._model = model
        self._api_key = api_key
        self._base_url = str(base_url or OPENAI_BASE_URL).rstrip("/")
        self._api_mode = str(api_mode or "").strip()
        self._engine_name_prefix = str(engine_name_prefix or "openai").strip()
        self._response_label = str(response_label or "OpenAI").strip()
        self._chat_extra = dict(chat_extra or {})

    @property
    def engine_name(self) -> str:
        return f"{self._engine_name_prefix}/{self._model}"

    def _call_api(self, system: str, user_msg: str) -> str:
        text, self.last_route = request_text(
            base_url=self._base_url, api_key=self._api_key, model=self._model,
            system=system, user=user_msg, api_mode=self._api_mode or "auto",
            connection_id=self._connection_id,
            model_role=self._model_role,
            chat_extra=self._chat_extra,
        )
        return text

    def _call_responses_api(self, system: str, user_msg: str) -> str:
        return request_text(
            base_url=self._base_url, api_key=self._api_key, model=self._model,
            system=system, user=user_msg, api_mode="responses",
            connection_id=self._connection_id,
            model_role=self._model_role,
        )[0]

    def chat(self, system: str, user: str) -> str:
        return self._call_api(system, user)
