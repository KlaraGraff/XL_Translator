"""
智谱 GLM 翻译引擎。
"""
import json

from tenacity import retry, stop_after_attempt, wait_exponential

from config import RETRY_MAX_ATTEMPTS, RETRY_WAIT_MIN, RETRY_WAIT_MAX
from core.translation_protocol import REPLACE_TRANSLATION_PREFIX
from engines.base_engine import (
    TASK_INSTRUCTION,
    TranslationEngine,
    get_source_lang_name,
    get_target_lang_name,
    parse_response,
)


class ZhipuEngine(TranslationEngine):

    def __init__(self, api_key: str, model: str = "glm-4"):
        from zhipuai import ZhipuAI
        self._client = ZhipuAI(api_key=api_key)
        self._model  = model

    @property
    def engine_name(self) -> str:
        return f"zhipu/{self._model}"

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
        return parse_response(texts, raw, "智谱")

    @retry(
        stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
        wait=wait_exponential(min=RETRY_WAIT_MIN, max=RETRY_WAIT_MAX),
        reraise=True,
    )
    def _call_api(self, system: str, user_msg: str) -> str:
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": user_msg},
            ],
        )
        return response.choices[0].message.content or ""

    def chat(self, system: str, user: str) -> str:
        return self._call_api(system, user)
